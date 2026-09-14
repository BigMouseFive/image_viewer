from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
import warnings
from typing import Any, Callable

from PIL import Image, ImageStat

from ..db import comments_hash
from ..inventory import (
    apply_product_exceptions,
    DIMENSION_REPAIR_REVIEW_STATUSES,
    dimension_repair_matches,
    is_ai_repairable_asset,
    load_manifest,
    reconcile,
)
from ..iopaint import IOPaintError, backup_asset, inspect_editable_image, resolve_editable_asset
from ..locks import asset_lock

EXPECTED_SIZE = (970, 600)
MIN_IMAGE_BYTES = 10_000
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_CANDIDATE_PIXELS = 10_000_000


class AIIntegrationError(RuntimeError):
    """An actionable, stable validation/conflict error for an external agent."""

    def __init__(self, message: str, *, status_code: int = 422, code: str = "invalid_result") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stale(message: str) -> AIIntegrationError:
    return AIIntegrationError(message, status_code=409, code="stale_task")


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_candidate(source: Path, destination: Path) -> None:
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except FileExistsError as error:
        raise AIIntegrationError("临时候选文件冲突，请重试", status_code=503, code="temporary_conflict") from error
    except OSError as error:
        raise AIIntegrationError(f"无法准备候选图片：{error}", status_code=503, code="file_io") from error


def validate_uploaded_image(path: Path, source_sha256: str) -> dict[str, Any]:
    """Validate an uploaded candidate without touching the production target."""
    if not path.is_file():
        raise AIIntegrationError("未收到图片文件", code="missing_image")
    try:
        size_bytes = path.stat().st_size
    except OSError as error:
        raise AIIntegrationError(f"无法读取上传图片：{error}", code="unreadable_image") from error
    if size_bytes < MIN_IMAGE_BYTES or size_bytes > MAX_IMAGE_BYTES:
        raise AIIntegrationError(
            f"图片文件大小异常：{size_bytes} bytes（应在 {MIN_IMAGE_BYTES} 到 {MAX_IMAGE_BYTES} bytes 之间）",
            code="invalid_size",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                if width * height > MAX_CANDIDATE_PIXELS:
                    raise AIIntegrationError("图片解压后的像素数过大", code="too_many_pixels")
                image.load()
                variance = float(ImageStat.Stat(image.convert("L")).var[0])
    except AIIntegrationError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise AIIntegrationError(f"图片无法解码：{error}", code="decode_failed") from error
    if image_format != "PNG":
        raise AIIntegrationError(f"图片必须为 PNG，实际为 {image_format}", code="invalid_format")
    if (width, height) != EXPECTED_SIZE:
        raise AIIntegrationError(
            f"图片必须为 {EXPECTED_SIZE[0]}×{EXPECTED_SIZE[1]}，实际为 {width}×{height}",
            code="invalid_dimensions",
        )
    if variance < 2:
        raise AIIntegrationError("图片接近纯色，拒绝覆盖", code="near_flat_image")
    try:
        digest = file_digest(path)
    except OSError as error:
        raise AIIntegrationError(f"无法校验上传图片：{error}", code="unreadable_image") from error
    if digest == source_sha256:
        raise AIIntegrationError("提交图片与原图内容相同", code="unchanged_image")
    return {
        "sha256": digest,
        "format": image_format,
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "variance": variance,
    }


def validate_result_metadata(result: dict[str, Any]) -> tuple[list[str], list[str]]:
    if result.get("result_status") != "completed":
        raise AIIntegrationError("只有 result_status=completed 可以自动应用图片", code="invalid_status")
    summary = str(result.get("summary") or "").strip()
    if not summary:
        raise AIIntegrationError("必须提供 summary", code="missing_summary")
    changes = [str(value).strip() for value in result.get("changes", []) if str(value).strip()]
    uncertainties = [str(value).strip() for value in result.get("uncertainties", []) if str(value).strip()]
    if uncertainties:
        raise AIIntegrationError("存在 uncertainties 时不可自动应用图片", code="unresolved_uncertainty")
    return changes, uncertainties


def _backup_root(db) -> Path:
    # The production database lives in data/, while tests can use an isolated
    # temporary database. Keeping backups beside the database preserves both
    # the existing layout and test isolation.
    return Path(db.path).resolve().parent / "backups"


def _restore_backup(target: Path, backup: Path, expected_sha256: str) -> None:
    restore_tmp = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
    try:
        if not backup.is_file() or file_digest(backup) != expected_sha256:
            raise AIIntegrationError("原图备份校验失败，拒绝自动恢复", status_code=503, code="backup_invalid")
        _copy_candidate(backup, restore_tmp)
        os.replace(restore_tmp, target)
        _fsync_parent(target)
        if file_digest(target) != expected_sha256:
            raise AIIntegrationError("恢复后的原图校验失败", status_code=503, code="restore_failed")
    finally:
        restore_tmp.unlink(missing_ok=True)


def _validate_reference_snapshot(root: Path, task: dict[str, Any]) -> None:
    reference_path = task.get("reference_path")
    reference_sha256 = str(task.get("reference_sha256") or "")
    if not reference_path or not reference_sha256:
        raise AIIntegrationError("缺少冻结的产品主图，不能自动覆盖图片", code="missing_reference")
    try:
        reference = resolve_editable_asset(root, str(reference_path))
        if file_digest(reference) != reference_sha256:
            raise _stale("产品主图已变化，任务已过期")
    except AIIntegrationError:
        raise
    except (OSError, ValueError, FileNotFoundError) as error:
        raise _stale("产品主图不可访问，任务已过期") from error


def _task_dimension_repair(task: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(task.get("dimension_repair_json") or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_repairable_inventory(item: dict[str, Any], task: dict[str, Any], root: Path | None = None) -> None:
    if not is_ai_repairable_asset(item):
        raise AIIntegrationError("该图片已不再是可由 AI 修复的清单图片", status_code=409, code="inventory_changed")
    repair_snapshot = _task_dimension_repair(task)
    if item.get("inventory_status") == "invalid_dimensions":
        if not dimension_repair_matches(item, repair_snapshot):
            raise _stale("尺寸异常信息已变化，任务已过期")
    elif repair_snapshot:
        raise _stale("图片尺寸状态已变化，任务已过期")
    if root is not None:
        try:
            _, metadata = inspect_editable_image(root, str(item.get("relative_path") or ""))
        except (IOPaintError, OSError, ValueError, FileNotFoundError) as error:
            raise AIIntegrationError("源文件不是可解码图片，不能自动 AI 修复", status_code=422, code="invalid_source_image") from error
        if metadata["format"] != "PNG" or (metadata["width"], metadata["height"]) != (item.get("width"), item.get("height")):
            raise _stale("源图片格式或尺寸已在扫描后变化，任务已过期")


def _application_metadata(target: Path, validation: dict[str, Any]) -> dict[str, Any]:
    stat = target.stat()
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "sha256": validation["sha256"],
        "width": validation["width"],
        "height": validation["height"],
    }


def _application_audit(task: dict[str, Any], result: dict[str, Any], changes: list[str], uncertainties: list[str]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "result_status": "completed",
        "summary": str(result.get("summary") or "").strip(),
        "changes": changes,
        "uncertainties": uncertainties,
        "provider": str(result.get("provider") or "").strip(),
        "model": str(result.get("model") or "").strip(),
    }


def _mark_application_safely(db, task_id: str, state: str, error_message: str = "") -> None:
    try:
        db.mark_external_application(task_id, state, error_message)
    except (KeyError, RuntimeError, OSError):
        # The journal remains prepared if SQLite is unavailable. Startup recovery
        # can still determine its safe state from source/candidate digests.
        pass


def _rollback_after_failure(
    db,
    task_id: str,
    target: Path,
    backup: Path,
    source_sha256: str,
    *,
    candidate_sha256: str,
    replaced: bool,
    cause: str,
) -> AIIntegrationError | None:
    """Restore only the known candidate; never overwrite a third-party write."""
    if not replaced:
        _mark_application_safely(db, task_id, "rolled_back", cause)
        return None
    try:
        current_sha256 = file_digest(target)
    except OSError as error:
        _mark_application_safely(db, task_id, "recovery_required", f"{cause}；无法读取当前目标：{error}")
        return AIIntegrationError(
            f"{cause}，且无法确认当前目标图片，已保留等待人工恢复：{error}",
            status_code=503,
            code="recovery_required",
        )
    if current_sha256 == source_sha256:
        _mark_application_safely(db, task_id, "rolled_back", f"{cause}；目标已是原图")
        return None
    if current_sha256 != candidate_sha256:
        _mark_application_safely(
            db,
            task_id,
            "recovery_required",
            f"{cause}；目标在 AI 应用期间被另一写入方改变，未覆盖该版本",
        )
        return AIIntegrationError(
            f"{cause}，但目标已被其他写入方改变；为避免丢失新版本，未自动恢复原图",
            status_code=503,
            code="concurrent_file_change",
        )
    try:
        _restore_backup(target, backup, source_sha256)
    except (AIIntegrationError, OSError) as error:
        _mark_application_safely(db, task_id, "recovery_required", f"{cause}；自动恢复失败：{error}")
        return AIIntegrationError(
            f"{cause}，且自动恢复原图失败：{error}",
            status_code=503,
            code="database_and_restore_failed",
        )
    _mark_application_safely(db, task_id, "rolled_back", cause)
    return None


def _prepared_application_error(application: dict[str, Any] | None) -> AIIntegrationError | None:
    if not application:
        return None
    if application.get("state") == "recovery_required":
        return AIIntegrationError(
            "该 AI 任务存在未恢复的图片应用记录，请先检查备份和目标图片",
            status_code=503,
            code="recovery_required",
        )
    if application.get("state") == "prepared":
        return AIIntegrationError(
            "该 AI 任务正在等待恢复，请稍后重试",
            status_code=503,
            code="recovery_pending",
        )
    return None


def _committed_application_result(db, task_id: str, source_id: int, asset_id: int, candidate_sha256: str) -> tuple[dict[str, Any] | None, bool]:
    """Return a committed application result, or whether the DB could be checked.

    This protects the rare case where SQLite commits successfully but an error
    occurs while the caller is returning/closing the connection. Restoring the
    source blindly in that case would create a DB/file mismatch.
    """
    try:
        task = db.external_ai_task(task_id, source_id)
        asset = db.asset(asset_id, source_id)
        result = db.external_ai_result_for_task(task_id, applied_only=True)
    except Exception:
        return None, False
    if (
        task
        and task.get("status") == "applied"
        and asset
        and asset.get("sha256") == candidate_sha256
        and result
        and result.get("result_sha256") == candidate_sha256
    ):
        _mark_application_safely(db, task_id, "applied", "已确认此前事务完成")
        return {
            "asset": asset,
            "result": result,
            "idempotent": True,
            "backup_path": result.get("backup_path", ""),
            "validation": None,
        }, True
    return None, True


def apply_external_result(
    db,
    source: dict,
    root: Path,
    task: dict,
    asset: dict,
    uploaded: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Safely apply one external AI candidate to the task's production target.

    A durable application journal is committed before the filesystem rename.
    The asset row, result audit, task state and journal completion are committed
    together afterwards. If the process dies in between, startup recovery can
    compare the target SHA-256 with the stored source/candidate fingerprints.
    """
    changes, uncertainties = validate_result_metadata(result)
    with asset_lock(source["id"], asset["id"]):
        persisted_task = db.external_ai_task(task["task_id"], source["id"])
        if persisted_task is None:
            persisted_task = task
        if persisted_task.get("status") == "applied" and persisted_task.get("result_id"):
            existing_asset = db.asset(asset["id"], source["id"])
            existing_result = db.external_ai_result_for_task(persisted_task["task_id"], applied_only=True)
            if not existing_result:
                raise AIIntegrationError("已应用任务缺少审计结果", status_code=503, code="missing_applied_audit")
            try:
                uploaded_digest = file_digest(uploaded)
            except OSError as error:
                raise AIIntegrationError("无法读取重复提交的候选图片", code="unreadable_image") from error
            if uploaded_digest != existing_result.get("result_sha256"):
                raise _stale("该任务已由另一份候选图完成，当前候选图不能覆盖已应用结果")
            return {
                "asset": existing_asset,
                "result": existing_result,
                "idempotent": True,
                "backup_path": existing_result.get("backup_path", ""),
                "validation": None,
            }

        pending_error = _prepared_application_error(db.external_ai_application(persisted_task["task_id"]))
        if pending_error:
            raise pending_error

        current = db.asset(asset["id"], source["id"])
        if not current or current.get("missing"):
            raise _stale("原图片已删除")
        if current["revision"] != persisted_task["source_revision"] or current["sha256"] != persisted_task["source_sha256"]:
            raise _stale("图片已被更新，任务已过期")
        if comments_hash(str(current.get("comments") or "")) != persisted_task["instructions_hash"]:
            raise _stale("人工修改意见已变化，任务已过期")
        repair_snapshot = _task_dimension_repair(persisted_task)
        allowed_statuses = {"needs_revision"}
        if repair_snapshot:
            allowed_statuses.update(DIMENSION_REPAIR_REVIEW_STATUSES)
        if current["status"] not in allowed_statuses:
            raise AIIntegrationError(
                f"当前图片状态不允许应用 AI 结果：{current['status']}",
                status_code=409 if current["status"] == "modified_pending_review" else 422,
                code="invalid_asset_status",
            )
        current_inventory = apply_product_exceptions(
            reconcile(db.assets(source["id"]), load_manifest(root)),
            db.product_exceptions(source["id"]),
        )
        eligible = next((item for item in current_inventory if item.get("id") == current["id"]), None)
        if not eligible:
            raise AIIntegrationError("该图片已不再是可由 AI 修复的清单图片", status_code=409, code="inventory_changed")
        _validate_repairable_inventory(eligible, persisted_task, root)

        try:
            target = resolve_editable_asset(root, current["relative_path"])
            target_digest = file_digest(target)
        except (OSError, ValueError, FileNotFoundError) as error:
            raise _stale("原图片不可访问，任务已过期") from error
        if target_digest != persisted_task["source_sha256"]:
            raise _stale("磁盘上的原图片已变化，任务已过期")
        _validate_reference_snapshot(root, persisted_task)
        validation = validate_uploaded_image(uploaded, persisted_task["source_sha256"])

        try:
            backup = backup_asset(target, _backup_root(db), source["id"], current)
            if file_digest(backup) != persisted_task["source_sha256"]:
                raise AIIntegrationError("原图备份校验失败，拒绝覆盖", status_code=503, code="backup_invalid")
            _fsync_file(backup)
            _fsync_parent(backup)
        except AIIntegrationError:
            raise
        except OSError as error:
            raise AIIntegrationError(f"无法创建原图备份：{error}", status_code=503, code="backup_failed") from error

        audit = _application_audit(persisted_task, result, changes, uncertainties)
        try:
            prepared = db.prepare_external_application(
                persisted_task["task_id"],
                source["id"],
                current["id"],
                persisted_task["source_revision"],
                persisted_task["source_sha256"],
                validation["sha256"],
                validation,
                audit,
                str(backup),
            )
        except RuntimeError as error:
            raise AIIntegrationError(f"无法登记图片应用：{error}", status_code=503, code="application_journal_failed") from error
        if prepared.get("idempotent"):
            existing_result = db.external_ai_result_for_task(persisted_task["task_id"], applied_only=True)
            existing_asset = db.asset(current["id"], source["id"])
            if not existing_result or existing_result.get("result_sha256") != validation["sha256"]:
                raise _stale("该任务已由另一份候选图完成，当前候选图不能覆盖已应用结果")
            return {
                "asset": existing_asset,
                "result": existing_result,
                "idempotent": True,
                "backup_path": existing_result.get("backup_path", ""),
                "validation": None,
            }

        temporary = target.with_name(f".{target.name}.external-ai-{os.getpid()}-{uuid.uuid4().hex}.tmp")
        replaced = False
        database_applied = False
        preserve_for_recovery = False
        try:
            # A non-cooperating process can still replace a file while we hold
            # the reviewer lock. Recheck just before the final rename so we
            # never apply a candidate based on a silently changed target,
            # reference, or inventory state. Keeping this inside the rollback
            # block also finalizes the journal if the check rejects the task.
            try:
                if file_digest(target) != persisted_task["source_sha256"]:
                    raise _stale("原图片在应用前已变化，任务已过期")
                _validate_reference_snapshot(root, persisted_task)
                latest_inventory = apply_product_exceptions(
                    reconcile(db.assets(source["id"]), load_manifest(root)),
                    db.product_exceptions(source["id"]),
                )
                latest_eligible = next((item for item in latest_inventory if item.get("id") == current["id"]), None)
                if not latest_eligible:
                    raise AIIntegrationError("图片清单状态已变化，不能继续覆盖", status_code=409, code="inventory_changed")
                _validate_repairable_inventory(latest_eligible, persisted_task, root)
            except OSError as error:
                raise _stale("原图片在应用前不可访问，任务已过期") from error
            _copy_candidate(uploaded, temporary)
            # Candidate preparation may take noticeable time for a 30 MiB PNG.
            # Recheck once more immediately before rename so a concurrent
            # direct editor/generator cannot silently lose a new source version.
            try:
                if file_digest(target) != persisted_task["source_sha256"]:
                    raise _stale("原图片在覆盖前已变化，任务已过期")
                _validate_reference_snapshot(root, persisted_task)
                final_inventory = apply_product_exceptions(
                    reconcile(db.assets(source["id"]), load_manifest(root)),
                    db.product_exceptions(source["id"]),
                )
                final_eligible = next((item for item in final_inventory if item.get("id") == current["id"]), None)
                if not final_eligible:
                    raise AIIntegrationError("图片清单状态已变化，不能继续覆盖", status_code=409, code="inventory_changed")
                _validate_repairable_inventory(final_eligible, persisted_task, root)
            except OSError as error:
                raise _stale("原图片在覆盖前不可访问，任务已过期") from error
            os.replace(temporary, target)
            _fsync_parent(target)
            replaced = True
            if file_digest(target) != validation["sha256"]:
                raise AIIntegrationError("覆盖后的图片 SHA-256 与候选图片不一致", status_code=503, code="replace_failed")
            metadata = _application_metadata(target, validation)
            try:
                applied = db.apply_external_revision(
                    persisted_task["task_id"],
                    source["id"],
                    current["id"],
                    persisted_task["source_revision"],
                    persisted_task["source_sha256"],
                    persisted_task["instructions_hash"],
                    metadata,
                    audit,
                    str(backup),
                    application_candidate_sha256=validation["sha256"],
                )
                database_applied = True
            except Exception as error:
                committed, db_checked = _committed_application_result(
                    db,
                    persisted_task["task_id"],
                    source["id"],
                    current["id"],
                    validation["sha256"],
                )
                if committed:
                    applied = committed
                    database_applied = True
                elif not db_checked:
                    # We cannot prove whether SQLite committed. Keep the
                    # candidate and prepared journal intact for deterministic
                    # startup recovery rather than risking a blind restore.
                    preserve_for_recovery = True
                    raise AIIntegrationError(
                        f"数据库应用结果无法确认，已保留候选图等待启动恢复：{error}",
                        status_code=503,
                        code="database_outcome_unknown",
                    ) from error
                else:
                    raise AIIntegrationError(
                        f"数据库更新失败：{error}",
                        status_code=503,
                        code="database_update_failed",
                    ) from error

            applied_result = applied.get("result")
            applied_asset = applied.get("asset")
            if not applied_asset or not applied_result:
                # The transaction may already have committed; query its source
                # of truth rather than restoring a now-recorded candidate.
                recovered_result = db.external_ai_result_for_task(persisted_task["task_id"], applied_only=True)
                recovered_asset = db.asset(current["id"], source["id"])
                if recovered_result and recovered_asset:
                    applied_result, applied_asset = recovered_result, recovered_asset
                    applied = {"asset": applied_asset, "result": applied_result, "idempotent": True}
                else:
                    raise AIIntegrationError("应用结果记录不完整", status_code=503, code="incomplete_application")
            return {
                "validation": {**validation, "sha256": applied_result["result_sha256"]},
                "backup_path": str(backup),
                **applied,
            }
        except AIIntegrationError as error:
            if not database_applied and not preserve_for_recovery:
                rollback_error = _rollback_after_failure(
                    db,
                    persisted_task["task_id"],
                    target,
                    backup,
                    persisted_task["source_sha256"],
                    candidate_sha256=validation["sha256"],
                    replaced=replaced,
                    cause=str(error),
                )
                if rollback_error:
                    raise rollback_error from error
            raise
        except Exception as error:
            if database_applied:
                raise AIIntegrationError(f"应用完成后读取结果失败：{error}", status_code=503, code="post_apply_error") from error
            if preserve_for_recovery:
                raise AIIntegrationError(
                    f"图片应用结果无法确认，已保留候选图等待启动恢复：{error}",
                    status_code=503,
                    code="database_outcome_unknown",
                ) from error
            rollback_error = _rollback_after_failure(
                db,
                persisted_task["task_id"],
                target,
                backup,
                persisted_task["source_sha256"],
                candidate_sha256=validation["sha256"],
                replaced=replaced,
                cause=f"数据库更新失败：{error}",
            )
            if rollback_error:
                raise rollback_error from error
            raise AIIntegrationError(
                f"数据库更新失败，已恢复原图：{error}",
                status_code=503,
                code="database_update_failed",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)


def _decode_application_payload(application: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        validation = json.loads(application["validation_json"])
        audit = json.loads(application["audit_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AIIntegrationError("图片应用日志内容损坏", status_code=503, code="invalid_application_journal") from error
    if not isinstance(validation, dict) or not isinstance(audit, dict):
        raise AIIntegrationError("图片应用日志内容无效", status_code=503, code="invalid_application_journal")
    return validation, audit


def recover_external_application(db, source: dict[str, Any], root: Path, application: dict[str, Any]) -> dict[str, Any]:
    """Resolve one durable journal left by an interrupted external application."""
    task_id = str(application["task_id"])
    with asset_lock(application["source_id"], application["asset_id"]):
        task = db.external_ai_task(task_id, application["source_id"])
        asset = db.asset(application["asset_id"], application["source_id"])
        if not task or not asset:
            return db.mark_external_application(task_id, "recovery_required", "任务或图片记录不存在")
        try:
            target = resolve_editable_asset(root, asset["relative_path"])
            disk_sha256 = file_digest(target)
        except (OSError, ValueError, FileNotFoundError) as error:
            return db.mark_external_application(task_id, "recovery_required", f"目标图片不可访问：{error}")

        if disk_sha256 == application["source_sha256"]:
            if task.get("status") == "applied":
                return db.mark_external_application(
                    task_id,
                    "recovery_required",
                    "任务已应用但磁盘目标已回到原图，存在数据库/文件不一致",
                )
            return db.mark_external_application(task_id, "rolled_back", "启动恢复：目标仍是原图，已标记回滚")
        if disk_sha256 != application["candidate_sha256"]:
            return db.mark_external_application(
                task_id,
                "recovery_required",
                "启动恢复：目标图片既不是任务原图也不是已登记候选图",
            )
        if task.get("status") == "applied":
            result = db.external_ai_result_for_task(task_id, applied_only=True)
            if result and result.get("result_sha256") == disk_sha256:
                return db.mark_external_application(task_id, "applied", "启动恢复：数据库已完成应用")
            return db.mark_external_application(task_id, "recovery_required", "任务已应用但审计记录不匹配")
        # Do not auto-apply an interrupted candidate if a human changed the
        # source decision/context while the service was down.
        if (
            task.get("source_revision") != application["source_revision"]
            or task.get("source_sha256") != application["source_sha256"]
            or asset.get("revision") != application["source_revision"]
            or asset.get("sha256") != application["source_sha256"]
            or asset.get("status") not in (DIMENSION_REPAIR_REVIEW_STATUSES if _task_dimension_repair(task) else {"needs_revision"})
            or comments_hash(str(asset.get("comments") or "")) != task.get("instructions_hash")
        ):
            return db.mark_external_application(task_id, "recovery_required", "启动恢复：图片版本、人工意见或评审状态已变化")
        inventory = apply_product_exceptions(
            reconcile(db.assets(application["source_id"]), load_manifest(root)),
            db.product_exceptions(application["source_id"]),
        )
        eligible = next((item for item in inventory if item.get("id") == asset["id"]), None)
        if not eligible:
            return db.mark_external_application(task_id, "recovery_required", "启动恢复：清单或产品异常状态已变化")
        try:
            _validate_repairable_inventory(eligible, task, root)
        except AIIntegrationError as error:
            return db.mark_external_application(task_id, "recovery_required", f"启动恢复：{error}")
        try:
            _validate_reference_snapshot(root, task)
            _, audit = _decode_application_payload(application)
            fresh_validation = validate_uploaded_image(target, application["source_sha256"])
            if fresh_validation["sha256"] != application["candidate_sha256"]:
                raise AIIntegrationError("候选图 SHA-256 与应用日志不匹配", code="invalid_application_journal")
            backup = Path(application["backup_path"])
            if not backup.is_file() or file_digest(backup) != application["source_sha256"]:
                raise AIIntegrationError("原图备份不存在或不匹配", code="backup_invalid")
            # Retain the originally recorded human-facing report, but use fresh
            # decode/size metadata from the on-disk candidate.
            metadata = _application_metadata(target, fresh_validation)
            applied = db.apply_external_revision(
                task_id,
                application["source_id"],
                application["asset_id"],
                application["source_revision"],
                application["source_sha256"],
                task["instructions_hash"],
                metadata,
                audit,
                str(backup),
                application_candidate_sha256=application["candidate_sha256"],
            )
            return {"state": "applied", "task_id": task_id, "result": applied.get("result")}
        except (AIIntegrationError, OSError, RuntimeError, KeyError) as error:
            return db.mark_external_application(task_id, "recovery_required", f"启动恢复失败：{error}")


def _record_recovery_failure(db, task_id: str, message: str) -> dict[str, Any]:
    try:
        return db.mark_external_application(task_id, "recovery_required", message)
    except Exception as error:  # Startup must not be blocked by a secondary DB failure.
        return {
            "task_id": task_id,
            "state": "recovery_required",
            "error_message": f"{message}；无法保存恢复状态：{error}",
        }


def recover_pending_external_applications(
    db,
    source_root: Callable[[dict[str, Any]], Path],
) -> list[dict[str, Any]]:
    """Recover every journal that was prepared before a process interruption."""
    recovered = []
    try:
        pending = db.pending_external_ai_applications()
    except Exception as error:
        return [{"state": "recovery_required", "error_message": f"无法读取待恢复应用日志：{error}"}]
    for application in pending:
        task_id = str(application["task_id"])
        source = db.source(application["source_id"])
        if not source:
            recovered.append(_record_recovery_failure(db, task_id, "图片目录不存在"))
            continue
        try:
            root = source_root(source)
            recovered.append(recover_external_application(db, source, root, application))
        except Exception as error:
            recovered.append(_record_recovery_failure(db, task_id, f"启动恢复失败：{error}"))
    return recovered
