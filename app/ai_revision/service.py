from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from ..iopaint import backup_asset, resolve_editable_asset
from ..locks import asset_lock
from ..scanner import refresh_asset
from .cursor_acp import CursorACPClient

ACTIVE_STATUSES = {"queued", "preparing", "running", "validating", "applying"}


class RevisionJobError(RuntimeError):
    pass


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def instructions_hash(comments: str) -> str:
    return hashlib.sha256(comments.strip().encode("utf-8")).hexdigest()


def build_prompt(task: dict[str, Any], target: Path, reference: Path, candidate: Path, report: Path) -> str:
    instructions = "\n".join(f"{index}. {line}" for index, line in enumerate(task["instructions"], 1))
    return f"""你是 Amazon A+ 图片修改代理。请直接完成任务，不要询问用户，不要修改 input 文件或工作目录外的文件。

SKU：{task['sku']}
模块：{task['module']}
待修改图片：{target}
产品主图：{reference}
人工评审意见：
{instructions}

规则：
1. 产品主图是产品外形、颜色、材质、结构、数量、款式和配件的事实依据。
2. 人工意见是必须解决的验收问题；自主分析问题区域和最小必要修改方法。
3. 保留未涉及且合格的构图、产品和视觉内容。
4. 使用 Cursor 自带图片生成/编辑能力生成候选图。
5. 输出必须是 970×600 PNG，写入：{candidate}
6. 不添加价格、折扣、评分、Amazon/Prime、联系方式、网址或未经支持的功能宣称。
7. 输出前检查产品一致性、数量、结构、文字完整性和画布尺寸。
8. 最多调用一次图片生成；必要时只允许再做一次局部修正。完成校验后立即写报告并结束，不要反复重新生成或追求无关优化。
9. 将报告写入：{report}
报告格式：{{"status":"completed","summary":"...","resolved_instructions":["..."],"changes":["..."],"uncertainties":[],"output_file":"{candidate}"}}
如信息不足，不要猜测，报告 status 使用 needs_human_input，列出 uncertainties，且不要伪造候选图。
"""


def validate_candidate(candidate: Path, source_sha256: str) -> dict[str, Any]:
    if not candidate.is_file():
        raise RevisionJobError("Cursor 未生成候选图片")
    size_bytes = candidate.stat().st_size
    if size_bytes < 10_000 or size_bytes > 30 * 1024 * 1024:
        raise RevisionJobError(f"候选图片文件大小异常：{size_bytes} bytes")
    try:
        with Image.open(candidate) as image:
            image.load()
            image_format = image.format
            size = image.size
            grayscale = image.convert("L")
            variance = ImageStat.Stat(grayscale).var[0]
    except (OSError, ValueError) as error:
        raise RevisionJobError(f"候选图片无法解码：{error}") from error
    if image_format != "PNG":
        raise RevisionJobError(f"候选图片必须为 PNG，实际为 {image_format}")
    if size != (970, 600):
        raise RevisionJobError(f"候选图片必须为 970×600，实际为 {size[0]}×{size[1]}")
    if variance < 2:
        raise RevisionJobError("候选图片接近纯色，拒绝自动覆盖")
    digest = file_digest(candidate)
    if digest == source_sha256:
        raise RevisionJobError("候选图片与原图内容相同")
    return {"sha256": digest, "bytes": size_bytes, "width": size[0], "height": size[1], "variance": variance}


def prepare_workspace(job: dict[str, Any], root: Path, workspace_root: Path) -> tuple[Path, Path, Path]:
    workspace = workspace_root / str(job["id"])
    if workspace.exists():
        shutil.rmtree(workspace)
    input_dir = workspace / "input"
    output_dir = workspace / "output"
    log_dir = workspace / "logs"
    input_dir.mkdir(parents=True)
    output_dir.mkdir()
    log_dir.mkdir()
    source_target = resolve_editable_asset(root, job["relative_path"])
    source_reference = resolve_editable_asset(root, job["reference_path"])
    target = input_dir / "target.png"
    reference = input_dir / f"product-reference{source_reference.suffix.lower()}"
    shutil.copy2(source_target, target)
    shutil.copy2(source_reference, reference)
    task = {
        "job_id": job["id"], "source_id": job["source_id"], "asset_id": job["asset_id"],
        "sku": job["sku"], "module": job["module"], "source_revision": job["source_revision"],
        "source_sha256": job["source_sha256"], "instructions": json.loads(job["instructions_json"]),
        "target_size": {"width": 970, "height": 600},
    }
    (workspace / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return workspace, target, reference


async def generate_candidate(job: dict[str, Any], workspace: Path, target: Path, reference: Path, timeout: float, agent_command: str = "agent") -> dict[str, Any]:
    candidate = workspace / "output" / "candidate.png"
    report_path = workspace / "output" / "result.json"
    task = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
    async with CursorACPClient(workspace, workspace / "logs" / "cursor-agent.log", timeout, agent_command) as client:
        session_data = await client.start_session()
        session = session_data["session"]
        model = session.get("models", {}).get("currentModelId", "")
        if not model.startswith("auto-"):
            raise RevisionJobError(f"Cursor 当前不是 Auto 模型：{model or '未知'}")
        result = await client.prompt(session["sessionId"], build_prompt(task, target, reference, candidate, report_path))
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        if report.get("status") != "completed":
            uncertainties = report.get("uncertainties") or []
            raise RevisionJobError("Cursor 无法可靠完成：" + ("；".join(uncertainties) or "缺少完成报告"))
        if report.get("uncertainties"):
            raise RevisionJobError("Cursor 报告存在不确定项，不自动覆盖")
        return {
            "session_id": session["sessionId"], "resolved_model": model, "prompt_result": result,
            "image_events": client.image_events, "report": report,
            "candidate": candidate,
        }


def apply_candidate(db, job: dict[str, Any], root: Path, workspace_root: Path, generated: dict[str, Any]) -> dict[str, Any]:
    candidate: Path = generated["candidate"]
    validation = validate_candidate(candidate, job["source_sha256"])
    # The optional legacy worker and external API share this lock. It keeps a
    # manual re-enable from racing a Skill-based result on the same asset.
    with asset_lock(job["source_id"], job["asset_id"]):
        current = db.asset(job["asset_id"], job["source_id"])
        if not current or current["missing"]:
            raise RevisionJobError("原图片已删除")
        if current["revision"] != job["source_revision"] or current["sha256"] != job["source_sha256"]:
            raise RevisionJobError("原图片已被其他操作修改，任务已过期")
        if current["status"] != "needs_revision":
            raise RevisionJobError(f"图片评审状态已变化：{current['status']}")
        if instructions_hash(current["comments"]) != job["instructions_hash"]:
            raise RevisionJobError("人工修改意见已变化，任务已过期")
        target = resolve_editable_asset(root, current["relative_path"])
        backup = backup_asset(target, PROJECT_BACKUP_ROOT(workspace_root), job["source_id"], current)
        temporary = target.with_name(f".{target.name}.ai-{job['id']}.tmp")
        try:
            shutil.copy2(candidate, temporary)
            os.replace(temporary, target)
            updated = refresh_asset(root, db, job["source_id"], job["asset_id"])
        finally:
            temporary.unlink(missing_ok=True)
        if not updated or updated["revision"] != current["revision"] + 1 or updated["status"] != "modified_pending_review":
            raise RevisionJobError("候选图已写入，但刷新后的版本状态不符合预期")
        return {"validation": validation, "backup": str(backup), "updated_asset": updated}


def PROJECT_BACKUP_ROOT(workspace_root: Path) -> Path:
    return workspace_root.parent / "backups"


async def run_job(db, job: dict[str, Any], root: Path, workspace_root: Path, timeout: float, agent_command: str = "agent") -> dict[str, Any]:
    workspace, target, reference = prepare_workspace(job, root, workspace_root)
    db.update_ai_job(job["id"], "running", workspace_path=str(workspace))
    generated = await generate_candidate(job, workspace, target, reference, timeout, agent_command)
    db.update_ai_job(
        job["id"], "validating", cursor_session_id=generated["session_id"],
        resolved_model=generated["resolved_model"], result_json=json.dumps(generated["report"], ensure_ascii=False),
    )
    db.update_ai_job(job["id"], "applying")
    applied = apply_candidate(db, job, root, workspace_root, generated)
    db.update_ai_job(
        job["id"], "completed", candidate_path=str(generated["candidate"]),
        candidate_sha256=applied["validation"]["sha256"], result_summary=generated["report"].get("summary", ""),
        completed=True,
    )
    return applied
