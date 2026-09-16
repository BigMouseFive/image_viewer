import asyncio
import csv
import hashlib
import json
import mimetypes
import re
import uuid
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
import sqlite3

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from urllib.parse import unquote
import uvicorn

from .config import BASE_DIR, load_config
from .db import Database, STATUSES, comments_hash, external_task_instructions, now
from .scanner import EXTENSIONS, refresh_asset, scan
from .inventory import (
    apply_product_exceptions,
    attach_ai_repair_metadata as attach_ai_repair_metadata_base,
    attach_references,
    dimension_repair_context,
    dimension_repair_matches,
    DIMENSION_REPAIR_REVIEW_STATUSES,
    filter_inventory,
    is_ai_repairable_asset,
    is_dimension_repair,
    is_dimension_repair_queueable,
    load_manifest,
    manifest_error,
    reconcile,
    reference_images,
)
from .iopaint import IOPaintError, backup_asset, inspect_editable_image, resolve_editable_asset, set_iopaint_input
from .locks import asset_lock

from .ai_integration.service import (
    AIIntegrationError,
    apply_external_result,
    file_digest,
    recover_pending_external_applications,
)

config = load_config()
db = Database(BASE_DIR / "data" / "reviews.db")
allowed_root = (BASE_DIR / config["images"].get("allowed_root_dir", "..")).resolve()
scan_interval = max(0, int(config["images"].get("scan_interval_seconds", 5)))
iopaint_config = config.get("iopaint", {})
iopaint_enabled = bool(iopaint_config.get("enabled", False))
iopaint_url = str(iopaint_config.get("url", "http://127.0.0.1:5055")).rstrip("/")
iopaint_timeout = max(0.5, float(iopaint_config.get("request_timeout_seconds", 5)))
product_csv_config = config.get("product_info", {}).get("csv", "../ai-relay/products_amazon_info_202609021610.csv")
product_source_csv = (BASE_DIR / product_csv_config).resolve() if not Path(product_csv_config).expanduser().is_absolute() else Path(product_csv_config).expanduser().resolve()
prompt_csv_config = config.get("aplus_prompts", {}).get("csv", "../ai-relay/outputs/aplus_prompts_20260903.csv")
prompt_source_csv = (BASE_DIR / prompt_csv_config).resolve() if not Path(prompt_csv_config).expanduser().is_absolute() else Path(prompt_csv_config).expanduser().resolve()
_product_info_cache = {"path": None, "mtime_ns": None, "items": {}}
_prompt_cache = {"path": None, "mtime_ns": None, "items": {}}


def product_info_by_sku():
    global _product_info_cache
    try:
        mtime_ns = product_source_csv.stat().st_mtime_ns
    except OSError:
        return {}
    if _product_info_cache["path"] == product_source_csv and _product_info_cache["mtime_ns"] == mtime_ns:
        return _product_info_cache["items"]
    result = {}
    try:
        with product_source_csv.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                sku = (row.get("SKU") or "").strip()
                title = (row.get("Amazon 标题") or "").strip()
                bullets = (row.get("Amazon 五点描述") or "").strip()
                asin = (row.get("ASIN") or "").strip().upper()
                if sku and (title or bullets):
                    result[sku] = {"title": title, "bullets": bullets, "asin": asin}
    except (OSError, csv.Error):
        result = {}
    _product_info_cache = {"path": product_source_csv, "mtime_ns": mtime_ns, "items": result}
    return result


def aplus_prompts_by_sku():
    global _prompt_cache
    try:
        mtime_ns = prompt_source_csv.stat().st_mtime_ns
    except OSError:
        return {}
    if _prompt_cache["path"] == prompt_source_csv and _prompt_cache["mtime_ns"] == mtime_ns:
        return _prompt_cache["items"]
    result = {}
    try:
        with prompt_source_csv.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                sku = (row.get("SKU") or "").strip()
                if sku:
                    result[sku] = {
                        module: {
                            "prompt": (row.get(f"{module}_prompt") or "").strip(),
                            "headline": (row.get(f"{module}_headline") or "").strip(),
                            "body": (row.get(f"{module}_body") or "").strip(),
                        }
                        for module in (f"A+L{index:02d}" for index in range(1, 6))
                    }
    except (OSError, csv.Error):
        result = {}
    _prompt_cache = {"path": prompt_source_csv, "mtime_ns": mtime_ns, "items": result}
    return result


def attach_product_info(items):
    info = product_info_by_sku()
    return [dict(item, product_info=info.get(item.get("sku"))) for item in items]



def attach_delivery_eligibility(items, source_id=None):
    """Attach current delivery readiness and change state to each SKU asset.

    ``sku_deliverable`` answers the original technical gate (all five current
    slots can be frozen). ``delivery_required`` is narrower: it is true only
    when that ready SKU has never been synced, or differs from its last synced
    immutable snapshot. Keeping those concepts separate prevents hundreds of
    unchanged historical deliveries from appearing in the pending-delivery view.
    """
    profile = APLUS_DELIVERY_PROFILES[0]
    asset_ids = [item["id"] for item in items if item.get("id") is not None]
    alt_texts = db.alt_texts(asset_ids)
    if source_id is None:
        source_ids = {item.get("source_id") for item in items if item.get("source_id") is not None}
        source_id = next(iter(source_ids)) if len(source_ids) == 1 else None
    snapshots = db.latest_synced_delivery_snapshots(source_id, profile["id"]) if source_id is not None else {}

    grouped = {}
    for item in items:
        if item.get("asset_role") != "deliverable" or not item.get("sku"):
            continue
        # Reconciliation detects duplicates and blocks the delivery gate. Keep
        # one representative here only for a useful pending-state explanation.
        grouped.setdefault(item["sku"], {}).setdefault(reviewer_module(item), item)

    summaries = {}
    for sku, assets_by_module in grouped.items():
        _, errors = delivery_slots_and_errors(profile, assets_by_module, alt_texts)
        ready = not errors
        snapshot = snapshots.get(sku)
        snapshot_slots = {
            reviewer_module(module): slot
            for module, slot in (snapshot or {}).get("slots", {}).items()
        }
        image_changed_modules = []
        metadata_changed_modules = []
        image_change_times = []
        metadata_change_times = []

        if snapshot:
            for definition in profile["slots"]:
                module = definition["module"]
                current = assets_by_module.get(module)
                previous = snapshot_slots.get(module)
                if not current or not previous:
                    image_changed_modules.append(module)
                    if current and current.get("content_updated_at"):
                        image_change_times.append(current["content_updated_at"])
                    continue
                if (
                    previous.get("asset_id") != current.get("id")
                    or previous.get("revision") != current.get("revision")
                    or previous.get("sha256") != current.get("sha256")
                ):
                    image_changed_modules.append(module)
                    if current.get("content_updated_at"):
                        image_change_times.append(current["content_updated_at"])
                    continue
                current_alt_text = str((alt_texts.get(current.get("id")) or {}).get("alt_text") or "")
                if previous.get("alt_text", "") != current_alt_text:
                    metadata_changed_modules.append(module)
                    updated_at = (alt_texts.get(current.get("id")) or {}).get("updated_at")
                    if updated_at:
                        metadata_change_times.append(updated_at)
        else:
            # Initial delivery has no historical slot to compare. Sort it by
            # its most recently created/changed image, but do not label it as a
            # revision of a prior delivery.
            image_change_times.extend(
                item.get("content_updated_at")
                for item in assets_by_module.values()
                if item.get("content_updated_at")
            )

        if not snapshot:
            delivery_state = "initial_delivery"
        elif image_changed_modules:
            delivery_state = "image_updated"
        elif metadata_changed_modules:
            delivery_state = "metadata_updated"
        else:
            delivery_state = "delivered_current"

        latest_snapshot = None
        if snapshot:
            latest_snapshot = {
                "id": snapshot["id"],
                "version": snapshot["version"],
                "created_at": snapshot["created_at"],
            }
        latest_image_updated_at = max(image_change_times, default=None)
        latest_metadata_updated_at = max(metadata_change_times, default=None)
        summaries[sku] = {
            "state": delivery_state,
            "ready": ready,
            "required": ready and delivery_state != "delivered_current",
            "blocking_reasons": errors,
            "image_changed_modules": image_changed_modules,
            "metadata_changed_modules": metadata_changed_modules,
            "latest_image_updated_at": latest_image_updated_at,
            "latest_change_at": max(
                (value for value in (latest_image_updated_at, latest_metadata_updated_at) if value),
                default=None,
            ),
            "latest_synced_delivery": latest_snapshot,
        }

    result = []
    for item in items:
        summary = summaries.get(item.get("sku")) if item.get("asset_role") == "deliverable" else None
        result.append(dict(
            item,
            sku_deliverable=bool(summary and summary["ready"]),
            delivery=summary,
        ))
    return result


def delivery_counts(items):
    """Count SKU-level delivery states, never individual image cards."""
    summaries = {}
    for item in items:
        if item.get("asset_role") != "deliverable" or not item.get("sku"):
            continue
        summary = item.get("delivery")
        if summary:
            summaries.setdefault(item["sku"], summary)
    values = list(summaries.values())
    return {
        # ``deliverable`` is the strict pending-delivery count. The change
        # buckets intentionally include not-yet-ready SKUs so the delivery
        # screen can still locate recently edited products for review.
        "deliverable": sum(summary.get("required") for summary in values),
        "image_updated": sum(summary.get("state") == "image_updated" for summary in values),
        "image_updated_ready": sum(summary.get("required") and summary.get("state") == "image_updated" for summary in values),
        "image_updated_pending_review": sum(summary.get("state") == "image_updated" and not summary.get("ready") for summary in values),
        "metadata_updated": sum(summary.get("state") == "metadata_updated" for summary in values),
        "metadata_updated_ready": sum(summary.get("required") and summary.get("state") == "metadata_updated" for summary in values),
        "initial_delivery": sum(summary.get("required") and summary.get("state") == "initial_delivery" for summary in values),
        "delivered": sum(summary.get("state") == "delivered_current" for summary in values),
    }


def delivery_filter_matches(item, delivery_status, delivery_focus="all"):
    """Return whether an item belongs in a delivery-management view.

    The explicit image/metadata focus is a locating tool, not a promise that
    the SKU can be delivered immediately. This distinction lets reviewers find
    a changed image that still needs human confirmation without weakening the
    five-slot delivery gate used by the default ``可交付`` view.
    """
    summary = item.get("delivery") or {}
    state = summary.get("state")
    if delivery_status == "delivered":
        return state == "delivered_current"
    if delivery_status != "deliverable":
        return True
    if delivery_focus in {"image_updated", "metadata_updated"}:
        return state == delivery_focus
    if delivery_focus == "initial_delivery":
        return bool(summary.get("required") and state == delivery_focus)
    return bool(summary.get("required"))


def _delivery_sort_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def delivery_sort_key(item, delivery_status):
    """Put recent image revisions above initial/metadata work and historic SKUs."""
    summary = item.get("delivery") or {}
    state = str(summary.get("state") or "")
    if delivery_status == "deliverable":
        priority = {"image_updated": 0, "metadata_updated": 1, "initial_delivery": 2}.get(state, 3)
        # Image-adjusted products are explicitly sorted by image-content time,
        # not by a later Alt Text edit on the same SKU.
        changed_at = (
            summary.get("latest_image_updated_at")
            if state == "image_updated"
            else summary.get("latest_change_at")
        )
    else:
        priority = 0
        changed_at = (summary.get("latest_synced_delivery") or {}).get("created_at")
    return (
        priority,
        -_delivery_sort_timestamp(changed_at),
        str(item.get("sku") or ""),
        reviewer_module(item),
        str(item.get("relative_path") or ""),
    )


def reviewer_module(item):
    """Return the canonical A+ module from an asset mapping or module string."""
    value = item.get("module", "") if isinstance(item, dict) else str(item or "")
    match = re.search(r"(A\+L\d+)$", value)
    return match.group(1) if match else value


def delivery_slots_and_errors(profile, assets_by_module, alt_texts):
    """Build a delivery snapshot only when every fixed slot passes every gate."""
    slots = []
    errors = []
    for definition in profile["slots"]:
        module = definition["module"]
        asset = assets_by_module.get(module)
        if not asset:
            errors.append(f"{module} 缺失")
            continue

        slot_errors = []
        if asset.get("inventory_status") != "present":
            slot_errors.append(f'{module} 状态为 {asset.get("inventory_status")}')
        # A previously synced asset is still eligible for a later immutable
        # delivery while its bytes/revision remain exactly the reviewed version.
        # A changed asset is moved back to pending review by the scanner and
        # must be confirmed again before it can enter a new delivery.
        review_complete = asset.get("status") in {"approved", "ignored"}
        delivered_unchanged = (
            asset.get("status") == "delivered"
            and asset.get("reviewed_revision") == asset.get("revision")
        )
        if not (review_complete or delivered_unchanged):
            slot_errors.append(f"{module} 尚未完成处理（请设为已确认或忽略）")
        elif asset.get("reviewed_revision") != asset.get("revision"):
            slot_errors.append(f"{module} 当前版本尚未完成处理（请重新确认或忽略）")
        if asset.get("width") != definition["width"] or asset.get("height") != definition["height"]:
            slot_errors.append(f'{module} 尺寸应为 {definition["width"]}×{definition["height"]}')
        alt_text = alt_texts.get(asset.get("id"))
        if not alt_text or not alt_text["alt_text"].strip():
            slot_errors.append(f"{module} 缺少 Alt Text")

        if slot_errors:
            errors.extend(slot_errors)
            continue
        slots.append({
            "slot_key": definition["slot_key"], "sequence": definition["sequence"], "module": module,
            "asset_id": asset["id"], "revision": asset["revision"], "sha256": asset["sha256"],
            "width": asset["width"], "height": asset["height"], "alt_text": alt_text["alt_text"],
        })
    if len(slots) != len(profile["slots"]):
        # Defensive guard: no partial five-slot snapshot may ever be persisted.
        errors.append("必须五个固定模块全部通过校验后才能创建交付版本")
    return ([], errors) if errors else (slots, errors)


def attach_alt_text(items):
    alt_texts = db.alt_texts([item["id"] for item in items if item.get("id") is not None])
    result = []
    for item in items:
        alt_text = alt_texts.get(item.get("id"))
        result.append(dict(item, alt_text=alt_text))
    return result


def external_ai_base_url():
    server = config.get("server", {})
    return str(server.get("public_url") or f"http://127.0.0.1:{server.get('port', 8700)}").rstrip("/")



def safe_dir(path):
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir() or (candidate != allowed_root and allowed_root not in candidate.parents):
        raise HTTPException(400, "目录不存在或不在允许范围内")
    return candidate


def _recovery_source_root(source):
    try:
        return safe_dir(source["path"])
    except HTTPException as error:
        raise OSError(str(error.detail)) from error


def active():
    source = db.active_source()
    if not source:
        raise HTTPException(400, "尚未选择图片目录")
    return source, safe_dir(source["path"])


async def periodic_scan():
    while True:
        await asyncio.sleep(max(1, scan_interval))
        source = db.active_source()
        if not source:
            continue
        try:
            path = safe_dir(source["path"])
            await asyncio.to_thread(scan, path, db, source["id"])
        except (OSError, HTTPException, sqlite3.Error):
            continue


APLUS_DELIVERY_PROFILES = [{
    "id": "uae-aplus-five-image-v1",
    "name": "UAE A+ 固定五图",
    "slots": [
        {"slot_key": "hero_banner", "sequence": 1, "module": "A+L01", "width": 970, "height": 600},
        *[{"slot_key": f"feature_{index}", "sequence": index + 1, "module": f"A+L{index + 1:02d}", "width": 970, "height": 600} for index in range(1, 5)],
    ],
}]


DEFAULTS = [
    ("文字错误", "请检查图片中的英文和阿拉伯文，修正拼写、语法和排版错误。"),
    ("产品外观不一致", "请确保产品外观、结构和参考图一致。"),
    ("产品数量不对", "请检查画面中的产品和配件数量。"),
    ("构图需要调整", "请重新调整主体位置、比例和留白。"),
    ("背景不符合要求", "请调整背景，使其符合当前模块和品牌要求。"),
    ("裁切不合理", "请确保产品完整，避免主体被裁切。"),
    ("删除多余物体", "请删除画面中不需要的物体或装饰。"),
]


@asynccontextmanager
async def lifespan(app):
    with db.connect() as con:
        if not db.sources():
            default = (BASE_DIR / config["images"].get("default_source", {}).get("path", "../ai-relay/outputs/aplus_images_uae")).resolve()
            if default.is_dir() and (default == allowed_root or allowed_root in default.parents):
                con.execute(
                    "INSERT INTO image_sources(name,path,active,created_at) VALUES(?,?,1,?)",
                    (config["images"].get("default_source", {}).get("name", "默认图片目录"), str(default), now()),
                )
        if con.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 0:
            con.executemany("INSERT INTO suggestions(title,content) VALUES(?,?)", DEFAULTS)
    source = db.active_source()
    if source:
        db.migrate_legacy(source["id"])
    # Recover any image replacement interrupted after its durable intent was
    # recorded but before the SQLite asset/task transaction completed. Run this
    # before scanning so the scanner cannot obscure the original task snapshot.
    await asyncio.to_thread(recover_pending_external_applications, db, _recovery_source_root)
    if source:
        try:
            await asyncio.to_thread(scan, safe_dir(source["path"]), db, source["id"])
        except (OSError, sqlite3.Error):
            # Keep the reviewer available if another short-lived SQLite writer
            # is active; the periodic scan retries after startup.
            pass
    scan_task = asyncio.create_task(periodic_scan())
    try:
        yield
    finally:
        scan_task.cancel()
        with suppress(asyncio.CancelledError):
            await scan_task


app = FastAPI(title="A+ Image Reviewer", lifespan=lifespan)


@app.middleware("http")
async def external_ai_upload_size_limit(request: Request, call_next):
    """Reject obviously oversized multipart requests before FastAPI parses them."""
    if request.url.path == "/api/ai/revision-results":
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                received = int(content_length)
            except ValueError:
                return JSONResponse({"detail": "Content-Length 非法"}, status_code=400)
            # Form fields and multipart framing add only a small bounded amount
            # beyond the candidate's 30 MiB business limit.
            if received > 30 * 1024 * 1024 + 128 * 1024:
                return JSONResponse({"detail": "上传请求超过 30MB 图片限制"}, status_code=413)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
Status = Literal["unreviewed", "needs_revision", "modified_pending_review", "approved", "ignored", "delivered"]


class Review(BaseModel):
    status: Status
    comments: str = Field(default="", max_length=20_000)
    revision: int = Field(ge=0)


class Suggestion(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2_000)


class SuggestionMove(BaseModel):
    direction: Literal["up", "down"]


class Source(BaseModel):
    name: str = Field(max_length=100)
    path: str = Field(min_length=1, max_length=4_096)


class AltTextUpdate(BaseModel):
    alt_text: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)



class DimensionRepairQueue(BaseModel):
    # Explicitly queue valid size exceptions; it changes review state only,
    # never overwrites an image or confirms a delivery.
    all_eligible: bool = False
    asset_ids: list[int] = Field(default_factory=list, max_length=200)


class DeliveryCreate(BaseModel):
    asins: list[str] = Field(default_factory=list, max_length=20)


class ProductExceptionUpdate(BaseModel):
    enabled: bool


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/iopaint/config")
def get_iopaint_config():
    return {"enabled": iopaint_enabled, "editor_url": iopaint_url if iopaint_enabled else None}


@app.get("/api/sources")
def get_sources():
    result = []
    for source in db.sources():
        path = Path(source["path"])
        source["exists"] = path.is_dir()
        with db.connect() as con:
            source["image_count"] = con.execute(
                "SELECT COUNT(*) FROM assets WHERE source_id=? AND missing=0", (source["id"],)
            ).fetchone()[0]
        result.append(source)
    return result


@app.get("/api/directories")
def directories(path: str = ""):
    current = allowed_root if not path else safe_dir(path)
    parent = current.parent if current != allowed_root else None
    children = []
    try:
        for item in sorted(current.iterdir()):
            if not item.name.startswith(".") and not item.is_symlink() and item.is_dir():
                children.append({"name": item.name, "path": str(item)})
    except OSError as error:
        raise HTTPException(400, f"目录不可访问：{error}") from error
    return {"path": str(current), "parent": str(parent) if parent else None, "directories": children}


@app.post("/api/sources")
def add_source(body: Source):
    path = safe_dir(body.path)
    try:
        source_id = db.add_source(body.name.strip() or path.name, str(path))
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "该目录已经添加") from error
    return {"id": source_id}


@app.post("/api/sources/{source_id}/activate")
def activate(source_id: int):
    source = db.source(source_id)
    if not source:
        raise HTTPException(404, "图片目录不存在")
    path = safe_dir(source["path"])
    db.activate_source(source_id)
    scan(path, db, source_id)
    return {"ok": True}


@app.post("/api/scan")
def scan_now():
    source, path = active()
    try:
        return {"count": scan(path, db, source["id"])}
    except sqlite3.Error as error:
        raise HTTPException(503, "扫描数据库暂时不可用，请稍后重试") from error


@app.post("/api/assets/{asset_id}/refresh")
def refresh_single_asset(asset_id: int):
    source, root = active()
    with db.connect() as con:
        current = con.execute(
            "SELECT relative_path FROM assets WHERE id=? AND source_id=?", (asset_id, source["id"])
        ).fetchone()
    if not current:
        raise HTTPException(404, "图片不存在")
    relative_path = current["relative_path"]
    try:
        refresh_asset(root, db, source["id"], asset_id)
    except KeyError as error:
        raise HTTPException(404, "图片不存在") from error
    except OSError as error:
        raise HTTPException(409, "图片正在变化，请稍后再次刷新") from error
    except sqlite3.Error as error:
        raise HTTPException(503, "刷新数据库暂时不可用，请稍后重试") from error

    all_items = attach_delivery_eligibility(apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    ), source["id"])
    references = reference_images(all_items)
    item = next(
        (candidate for candidate in all_items if candidate.get("id") == asset_id),
        None,
    ) or next(
        (candidate for candidate in all_items if candidate.get("relative_path") == relative_path),
        None,
    )
    if not item:
        return {"removed": True, "asset_id": asset_id}
    if item.get("asset_role") != "reference":
        item = attach_references([item], references)[0]
    item = attach_ai_repair_metadata([item], root)[0]
    item = attach_product_info(attach_alt_text([item]))[0]
    return {"removed": False, "asset": item}


def prepare_iopaint_asset(asset_id: int):
    if not iopaint_enabled:
        raise HTTPException(503, "IOPaint 集成未启用")
    source, root = active()
    with asset_lock(source["id"], asset_id):
        asset = db.asset(asset_id, source["id"])
        if not asset or asset.get("missing"):
            raise HTTPException(404, "图片不存在")
        # Reconcile the complete source, not a one-item subset: a duplicate
        # module elsewhere in the SKU must block direct editor access too.
        reconciled = apply_product_exceptions(
            reconcile(db.assets(source["id"]), load_manifest(root)),
            db.product_exceptions(source["id"]),
        )
        candidate = next((item for item in reconciled if item.get("id") == asset_id), None)
        if not candidate or candidate.get("asset_role") != "deliverable" or candidate.get("inventory_status") != "present":
            raise HTTPException(403, "只有清单内且文件正常的生成图片可以进入消除编辑")
        try:
            target = resolve_editable_asset(root, asset["relative_path"])
            backup = None
            if iopaint_config.get("backup_before_edit", True):
                backup = backup_asset(target, BASE_DIR / "data" / "backups", source["id"], asset)
            set_iopaint_input(iopaint_url, target, iopaint_timeout)
        except FileNotFoundError as error:
            raise HTTPException(410, "图片文件已经删除") from error
        except ValueError as error:
            raise HTTPException(403, str(error)) from error
        except IOPaintError as error:
            raise HTTPException(502, str(error)) from error
    return asset, backup


@app.post("/api/assets/{asset_id}/open-in-iopaint")
def open_in_iopaint(asset_id: int):
    asset, backup = prepare_iopaint_asset(asset_id)
    return {
        "ok": True,
        "editor_url": iopaint_url,
        "asset_id": asset_id,
        "revision": asset["revision"],
        "sha256": asset["sha256"],
        "backup_created": bool(backup),
    }


@app.get("/api/assets/{asset_id}/edit-in-iopaint")
def edit_in_iopaint(asset_id: int):
    asset, _ = prepare_iopaint_asset(asset_id)
    return RedirectResponse(
        f"{iopaint_url}/?image_reviewer={asset_id}&revision={asset['revision']}",
        status_code=303,
    )


@app.put("/api/products/{sku}/exception")
def update_product_exception(sku: str, body: ProductExceptionUpdate):
    source, _ = active()
    normalized_sku = sku.strip()
    if not normalized_sku:
        raise HTTPException(422, "SKU 不能为空")
    db.set_product_exception(source["id"], normalized_sku, body.enabled)
    return {"sku": normalized_sku, "product_exception": body.enabled}


@app.post("/api/assets/{asset_id}/review")
def review(asset_id: int, body: Review):
    source, _ = active()
    try:
        with db.connect() as con:
            row = con.execute("SELECT relative_path FROM assets WHERE id=? AND source_id=? AND missing=0", (asset_id, source["id"])).fetchone()
        if not row:
            raise KeyError(asset_id)
        manifest = load_manifest(safe_dir(source["path"]))
        assets = attach_ai_repair_metadata(apply_product_exceptions(
            reconcile(db.assets(source["id"]), manifest),
            db.product_exceptions(source["id"]),
        ), safe_dir(source["path"]))
        asset = next((item for item in assets if item["id"] == asset_id), None)
        if not asset:
            raise KeyError(asset_id)
        if asset["inventory_status"] == "invalid_dimensions":
            if not (asset.get("dimension_repair") or {}).get("required"):
                raise PermissionError("该尺寸异常不是可自动修复的 PNG 970×600 画布问题，请人工处理")
            if body.status != "needs_revision":
                raise PermissionError("尺寸异常图片只能加入尺寸修复队列，修复为 970×600 后才能确认或忽略")
        elif asset["inventory_status"] != "present":
            raise PermissionError("非正常清单、产品异常或尺寸异常图片不可评审")
        with asset_lock(source["id"], asset_id):
            db.update(asset_id, body.status, body.comments, body.revision, source["id"])
    except KeyError as error:
        raise HTTPException(404, "图片不存在") from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(410, "图片文件已经删除") from error
    except RuntimeError as error:
        raise HTTPException(409, "图片已被更新，请刷新后重新评审") from error
    return {"ok": True}


REVIEW_STATUSES = {"all", "unreviewed", "needs_revision", "modified_pending_review", "approved", "ignored", "delivered"}
INVENTORY_STATUSES = {
    "all", "present", "missing", "blocked", "extra", "invalid_dimensions",
    "invalid_manifest", "wrong_path", "duplicate_module", "product_exception",
}


def parse_filter_statuses(value: str, allowed: set[str], label: str) -> set[str]:
    statuses = {item for item in unquote(value).split(",") if item}
    if not statuses:
        return {"all"}
    invalid = statuses - allowed
    if invalid:
        raise HTTPException(422, f"未知{label}：{', '.join(sorted(invalid))}")
    return {"all"} if "all" in statuses else statuses


@app.get("/api/assets")
def get_assets(
    status: str = "all",
    inventory_status: str = "all",
    delivery_status: str = "all",
    delivery_focus: str = "all",
    q: str = "",
    limit: int = 60,
    offset: int = 0,
):
    review_statuses = parse_filter_statuses(status, REVIEW_STATUSES, "评审状态")
    inventory_statuses = parse_filter_statuses(inventory_status, INVENTORY_STATUSES, "清单状态")
    source, root = active()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    all_items = attach_ai_repair_metadata(apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    ), root)
    references = reference_images(all_items)
    items = attach_delivery_eligibility(all_items, source["id"])
    counts = delivery_counts(items)
    if delivery_status not in {"all", "deliverable", "delivered"}:
        raise HTTPException(422, "未知交付状态")
    if delivery_focus not in {"all", "image_updated", "metadata_updated", "initial_delivery"}:
        raise HTTPException(422, "未知交付聚焦条件")
    items = [
        item for item in items
        if delivery_filter_matches(item, delivery_status, delivery_focus)
    ]
    # A focus such as “图片已调整” is a locating tool. It deliberately keeps
    # changed SKUs whose five-image gate is not complete yet (for example,
    # modified_pending_review), so do not let stale review/inventory filters
    # hide the very product the user is trying to find.
    locating_focus = delivery_status == "deliverable" and delivery_focus in {
        "image_updated", "metadata_updated",
    }
    if "all" not in inventory_statuses and "extra" not in inventory_statuses and not locating_focus:
        items = [item for item in items if item.get("asset_role") != "reference"]
    if "all" not in review_statuses and not locating_focus:
        items = [item for item in items if item["status"] in review_statuses]
    items = filter_inventory(items, {"all"} if locating_focus else inventory_statuses, q)
    if delivery_status in {"deliverable", "delivered"}:
        items.sort(key=lambda item: delivery_sort_key(item, delivery_status))
    items = attach_product_info(attach_alt_text(attach_references(items, references)))
    return {
        "items": items[offset:offset + limit],
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(items),
        "delivery_counts": counts,
    }


AI_RESULT_STATUSES = {"completed", "needs_human_input", "failed"}
MAX_EXTERNAL_AI_UPLOAD_BYTES = 30 * 1024 * 1024


def _safe_asset_path(root, relative_path):
    if not relative_path:
        return None
    try:
        return resolve_editable_asset(root, str(relative_path))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _source_png_dimensions(root, relative_path):
    """Return dimensions only for a fully decodable source PNG."""
    try:
        _, metadata = inspect_editable_image(root, str(relative_path or ""))
    except (FileNotFoundError, ValueError, OSError, IOPaintError):
        return None
    if metadata["format"] != "PNG":
        return None
    return metadata["width"], metadata["height"]


def _source_matches_scanned_png(root, item, *, verify_bytes: bool = False):
    if item.get("image_format") != "PNG":
        return False
    if not verify_bytes:
        return True
    return _source_png_dimensions(root, item.get("relative_path")) == (item.get("width"), item.get("height"))


def attach_ai_repair_metadata(items, root):
    """Attach UI/task metadata after verifying a source is truly a PNG."""
    result = []
    for item in attach_ai_repair_metadata_base(items):
        basic_repairable = bool(item.get("ai_repairable"))
        repair = item.get("dimension_repair")
        # Scanner metadata is sufficient for normal 970×600 assets; fully
        # decode only the rare size-exception candidates shown as repairable.
        source_valid = None
        if repair:
            source_dimensions = _source_png_dimensions(root, item.get("relative_path"))
            source_valid = source_dimensions == (item.get("width"), item.get("height"))
            repair = dict(repair, source_valid=source_valid)
            if not source_valid:
                repair.update({
                    "required": False,
                    "reason": "源文件不是可解码的 PNG，或内容已在扫描后变化，不能自动尺寸修复。",
                })
        result.append(dict(
            item,
            dimension_repair=repair,
            source_image_valid=source_valid,
            ai_repairable=basic_repairable and (source_valid is not False),
        ))
    return result


def _prompt_entry(prompts, sku, module):
    entry = (prompts.get(str(sku)) or {}).get(module)
    return entry if isinstance(entry, dict) else {}


def _external_task_snapshot(source, root, asset, references, prompts, product_info):
    """Freeze every human/product input that an external image agent sees."""
    sku = str(asset["sku"])
    module = reviewer_module(asset)
    comments = str(asset.get("comments") or "").strip()
    entry = _prompt_entry(prompts, sku, module)
    reference = next((item for item in references.get(sku, []) if item.get("id") and item.get("relative_path")), None)
    original_copy = {"headline": entry.get("headline") or None, "body": entry.get("body") or None}
    task, _ = db.create_or_get_external_task(
        source["id"],
        asset,
        reference,
        comments_hash(comments),
        entry.get("prompt") or None,
        original_copy,
        product_info.get(sku) or {},
        dimension_repair=dimension_repair_context(asset),
    )
    return task, reference


def _task_dimension_repair(task):
    try:
        value = json.loads(task.get("dimension_repair_json") or "null")
    except (TypeError, ValueError):
        value = None
    return value if isinstance(value, dict) else None


def _task_reference_from_items(task, references):
    reference_id = task.get("reference_asset_id")
    if reference_id is not None:
        return next((item for values in references.values() for item in values if item.get("id") == reference_id), None)
    path = task.get("reference_path")
    return next((item for values in references.values() for item in values if item.get("relative_path") == path), None)


def external_ai_task_item(source, root, asset, task, reference=None):
    """Build the stable, self-contained context consumed by another AI agent."""
    sku = str(task.get("sku") or asset["sku"])
    module = reviewer_module(str(task.get("module") or asset["module"]))
    comments = str(task.get("comments") or "")
    prompt = task.get("original_prompt") or None
    copy = {"headline": task.get("original_headline"), "body": task.get("original_body")}
    target_relative = str(task.get("relative_path") or asset.get("relative_path"))
    reference_path = task.get("reference_path")
    target = _safe_asset_path(root, target_relative)
    reference_target = _safe_asset_path(root, reference_path)
    base_url = external_ai_base_url()
    task_value = task["task_id"]
    task_is_active = task.get("status") != "applied"
    target_info = {
        "relative_path": target_relative,
        "absolute_path": str(target) if target else None,
        "url": f"{base_url}/api/ai/revision-tasks/{task_value}/target" if task_is_active else None,
    }
    reference_info = {
        "relative_path": reference_path,
        "absolute_path": str(reference_target) if reference_target else None,
        "url": f"{base_url}/api/ai/revision-tasks/{task_value}/reference" if task_is_active and reference_path else None,
        "asset_id": task.get("reference_asset_id"),
        "revision": task.get("reference_revision"),
        "sha256": task.get("reference_sha256") or None,
    }
    try:
        stored_product_info = json.loads(task.get("product_info_json") or "{}")
    except (TypeError, ValueError):
        stored_product_info = {}
    if not isinstance(stored_product_info, dict):
        stored_product_info = {}

    dimension_repair = _task_dimension_repair(task)
    instructions = external_task_instructions(task)
    expected_dimensions = (dimension_repair or {}).get("expected_dimensions") or {}
    expected_output = {
        "format": "PNG",
        "width": int(expected_dimensions.get("width") or 970),
        "height": int(expected_dimensions.get("height") or 600),
    }
    return {
        "task_id": task_value,
        "source_id": source["id"],
        "asset_id": asset["id"],
        "sku": sku,
        "module": module,
        "task_status": task.get("status", "open"),
        "task_kind": "dimension_repair" if dimension_repair else "review_revision",
        "asset_status": asset.get("status"),
        "comments": comments,
        "instructions": instructions,
        "instructions_hash": task.get("instructions_hash") or comments_hash(comments),
        "dimension_repair": dimension_repair,
        "revision": task["source_revision"],
        "sha256": task["source_sha256"],
        "target_image": target_info,
        "main_image": reference_info,
        "target_image_path": target_info["absolute_path"],
        "reference_image_path": reference_info["absolute_path"],
        "target_image_url": target_info["url"],
        "reference_image_url": reference_info["url"],
        # Compatibility aliases for straightforward local integrations.
        "image": target_info,
        "reference_image": reference_info,
        "original_prompt": prompt,
        "original_copy": copy,
        "prompt_status": "available" if prompt else "missing",
        "product_info": stored_product_info,
        "expected_output": expected_output,
        "result_submit_url": f"{base_url}/api/ai/revision-results",
    }


def _source_context(source_id):
    source = db.source(source_id)
    if not source:
        raise HTTPException(404, "图片目录不存在")
    try:
        root = safe_dir(source["path"])
    except HTTPException as error:
        raise HTTPException(409, "图片目录已不可用") from error
    return source, root


def external_ai_task_context(task_value: str, allow_applied: bool = False):
    task = db.external_ai_task(task_value)
    if not task:
        raise HTTPException(404, "AI 修改任务不存在，请先重新获取任务列表")
    source, root = _source_context(task["source_id"])
    manifest = load_manifest(root)
    all_items = attach_ai_repair_metadata(apply_product_exceptions(
        reconcile(db.assets(source["id"]), manifest),
        db.product_exceptions(source["id"]),
    ), root)
    asset = next((item for item in all_items if item.get("id") == task["asset_id"]), None)
    if not asset or asset.get("missing"):
        raise HTTPException(410, "任务对应的原图片已经删除")
    if asset.get("relative_path") != task.get("relative_path"):
        raise HTTPException(409, "任务对应的图片路径已变化，请重新获取任务")

    is_applied = task.get("status") == "applied"
    if is_applied:
        if not allow_applied:
            raise HTTPException(409, "任务已经应用过，请获取该图片的新任务")
        # Historical detail remains useful even when a later manifest/product
        # decision makes the current asset ineligible. Its content URLs stay
        # absent, so it cannot become an editing source again.
        return source, root, task, asset, reference_images(all_items)

    task_dimension_repair = _task_dimension_repair(task)
    if not asset.get("ai_repairable"):
        raise HTTPException(422, "该图片已不再是可由 AI 修复的清单图片")
    if not _source_matches_scanned_png(root, asset, verify_bytes=True):
        raise HTTPException(422, "源文件不是可解码的 PNG，或内容已在扫描后变化，不能自动 AI 修复")
    if asset.get("inventory_status") == "invalid_dimensions":
        if not dimension_repair_matches(asset, task_dimension_repair):
            raise HTTPException(409, "尺寸异常信息已变化，请重新获取任务")
    elif task_dimension_repair:
        raise HTTPException(409, "图片尺寸状态已变化，请重新获取任务")
    if Path(str(task.get("relative_path") or "")).suffix.lower() != ".png":
        raise HTTPException(422, "外部 AI 修改目前只支持 PNG 目标图片")
    if asset["revision"] != task["source_revision"] or asset["sha256"] != task["source_sha256"]:
        raise HTTPException(409, "任务已过期，请重新获取任务列表")
    if comments_hash(str(asset.get("comments") or "")) != task.get("instructions_hash"):
        raise HTTPException(409, "人工修改意见已变化，请重新获取任务列表")
    allowed_statuses = {"needs_revision"}
    if asset.get("inventory_status") == "invalid_dimensions":
        allowed_statuses.update(DIMENSION_REPAIR_REVIEW_STATUSES)
    if asset.get("status") not in allowed_statuses:
        raise HTTPException(422, "该图片当前不在可由 AI 修复的状态")
    target_path = _safe_asset_path(root, task.get("relative_path"))
    if not target_path:
        raise HTTPException(410, "任务对应的原图片已经删除或不可访问")
    try:
        if file_digest(target_path) != task.get("source_sha256"):
            raise HTTPException(409, "磁盘上的原图片已变化，请重新获取任务")
    except OSError as error:
        raise HTTPException(409, "任务对应的原图片不可访问，请重新获取任务") from error

    references = reference_images(all_items)
    # A task without a reference is intentionally still visible so an agent can
    # submit needs_human_input instead of fabricating product facts. A completed
    # image result is rejected later by apply_external_result.
    if not task.get("reference_path"):
        return source, root, task, asset, references
    reference = _task_reference_from_items(task, references)
    if not reference:
        raise HTTPException(409, "任务对应的产品主图已删除或变化，请重新获取任务")
    if (
        reference.get("relative_path") != task.get("reference_path")
        or reference.get("revision") != task.get("reference_revision")
        or reference.get("sha256") != task.get("reference_sha256")
    ):
        raise HTTPException(409, "产品主图已变化，请重新获取任务")
    reference_path = _safe_asset_path(root, task.get("reference_path"))
    if not reference_path:
        raise HTTPException(409, "任务对应的产品主图不可访问，请重新获取任务")
    try:
        if file_digest(reference_path) != task.get("reference_sha256"):
            raise HTTPException(409, "磁盘上的产品主图已变化，请重新获取任务")
    except OSError as error:
        raise HTTPException(409, "任务对应的产品主图不可访问，请重新获取任务") from error
    return source, root, task, asset, references


@app.get("/api/ai/revision-tasks")
def external_ai_revision_tasks(status: str = "needs_revision", sku: str = "", limit: int = 60, offset: int = 0):
    raw_statuses = {item.strip() for item in unquote(status).split(",") if item.strip()}
    if not raw_statuses:
        raw_statuses = {"needs_revision"}
    if raw_statuses != {"needs_revision"}:
        raise HTTPException(422, "外部 AI 仅接收人工标记为“需修改”的图片；“已修改”必须先人工复核")
    source, root = active()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    manifest = load_manifest(root)
    all_items = attach_ai_repair_metadata(apply_product_exceptions(
        reconcile(db.assets(source["id"]), manifest),
        db.product_exceptions(source["id"]),
    ), root)
    references = reference_images(all_items)
    prompts = aplus_prompts_by_sku()
    product_info = product_info_by_sku()
    needle = sku.strip().lower()
    result = []
    for asset in all_items:
        if not asset.get("ai_repairable"):
            continue
        if not _source_matches_scanned_png(root, asset):
            continue
        dimension_repair = is_dimension_repair(asset)
        if dimension_repair:
            if not is_dimension_repair_queueable(asset):
                continue
        elif asset.get("status") != "needs_revision":
            continue
        if not dimension_repair and not str(asset.get("comments") or "").strip():
            continue
        if Path(str(asset.get("relative_path") or "")).suffix.lower() != ".png":
            continue
        if needle and needle not in str(asset.get("sku") or "").lower():
            continue
        task, reference = _external_task_snapshot(source, root, asset, references, prompts, product_info)
        result.append(external_ai_task_item(source, root, asset, task, reference))
    result.sort(key=lambda item: (item["sku"], item["module"], item["task_id"]))
    return {
        "source": {"id": source["id"], "name": source["name"]},
        "target_output": {"format": "PNG", "width": 970, "height": 600},
        "result_statuses": sorted(AI_RESULT_STATUSES),
        "items": result[offset:offset + limit],
        "total": len(result),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(result),
    }


@app.get("/api/ai/revision-tasks/{task_value}")
def external_ai_revision_task(task_value: str):
    source, root, task, asset, references = external_ai_task_context(task_value, allow_applied=True)
    return external_ai_task_item(source, root, asset, task, _task_reference_from_items(task, references))


@app.get("/api/ai/revision-tasks/{task_value}/target")
def external_ai_revision_target(task_value: str):
    source, root, task, asset, _ = external_ai_task_context(task_value)
    target = _safe_asset_path(root, task["relative_path"])
    if not target:
        raise HTTPException(410, "目标图片文件已经删除或不可访问")
    return FileResponse(target, media_type="image/png", headers={
        "Cache-Control": "no-cache",
        "ETag": f'"sha256-{task["source_sha256"]}"',
        "X-Image-Reviewer-Revision": str(task["source_revision"]),
        "X-Image-Reviewer-SHA256": task["source_sha256"],
    })


@app.get("/api/ai/revision-tasks/{task_value}/reference")
def external_ai_revision_reference(task_value: str):
    source, root, task, _, _ = external_ai_task_context(task_value)
    reference = _safe_asset_path(root, task.get("reference_path"))
    if not reference:
        raise HTTPException(404, "该 SKU 没有可用的产品主图")
    return FileResponse(reference, media_type=mimetypes.guess_type(reference.name)[0] or "image/jpeg", headers={
        "Cache-Control": "no-cache",
        "ETag": f'"sha256-{task["reference_sha256"]}"',
        "X-Image-Reviewer-Revision": str(task["reference_revision"]),
        "X-Image-Reviewer-SHA256": task["reference_sha256"],
    })


MAX_EXTERNAL_AI_METADATA_BYTES = 20_000
MAX_EXTERNAL_AI_LIST_ITEMS = 50
MAX_EXTERNAL_AI_TEXT_LENGTH = 4_000


def _parse_json_array(value: str, field: str):
    if len((value or "").encode("utf-8")) > MAX_EXTERNAL_AI_METADATA_BYTES:
        raise HTTPException(422, f"{field} 内容过长")
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as error:
        raise HTTPException(422, f"{field} 必须是 JSON 数组") from error
    if not isinstance(parsed, list):
        raise HTTPException(422, f"{field} 必须是 JSON 数组")
    if len(parsed) > MAX_EXTERNAL_AI_LIST_ITEMS:
        raise HTTPException(422, f"{field} 最多包含 {MAX_EXTERNAL_AI_LIST_ITEMS} 项")
    if any(not isinstance(item, str) or len(item.strip()) > MAX_EXTERNAL_AI_TEXT_LENGTH for item in parsed):
        raise HTTPException(422, f"{field} 的每一项必须是长度不超过 {MAX_EXTERNAL_AI_TEXT_LENGTH} 的字符串")
    return [item.strip() for item in parsed if item.strip()]


def _external_audit_base(task, source, result_status, summary, changes, uncertainties, provider, model, error_message=""):
    return {
        "task_id": task["task_id"], "source_id": source["id"], "asset_id": task["asset_id"],
        "source_revision": task["source_revision"], "source_sha256": task["source_sha256"],
        "result_status": result_status, "summary": summary.strip(), "changes": changes,
        "uncertainties": uncertainties, "provider": provider.strip(), "model": model.strip(),
        "error_message": error_message, "instructions_hash": task["instructions_hash"],
        "instructions": external_task_instructions(task),
        "comments": task["comments"], "original_prompt": task.get("original_prompt") or "",
        "original_copy": {"headline": task.get("original_headline"), "body": task.get("original_body")},
    }


@app.post("/api/ai/revision-results")
async def submit_external_ai_revision_result(
    task_id: str = Form(...),
    source_revision: int = Form(...),
    source_sha256: str = Form(...),
    instructions_hash_value: str = Form(..., alias="instructions_hash"),
    result_status: str = Form(...),
    summary: str = Form(...),
    changes_json: str = Form("[]"),
    uncertainties_json: str = Form("[]"),
    provider: str = Form(""),
    model: str = Form(""),
    image: UploadFile | None = File(None),
):
    changes = _parse_json_array(changes_json, "changes_json")
    uncertainties = _parse_json_array(uncertainties_json, "uncertainties_json")
    normalized_summary = summary.strip()
    normalized_provider = provider.strip()
    normalized_model = model.strip()
    if result_status not in AI_RESULT_STATUSES:
        raise HTTPException(422, "result_status 必须是 completed、needs_human_input 或 failed")
    if not normalized_summary:
        raise HTTPException(422, "summary 不能为空")
    if len(normalized_summary) > MAX_EXTERNAL_AI_TEXT_LENGTH:
        raise HTTPException(422, f"summary 不能超过 {MAX_EXTERNAL_AI_TEXT_LENGTH} 个字符")
    if len(normalized_provider) > 200 or len(normalized_model) > 200:
        raise HTTPException(422, "provider 和 model 不能超过 200 个字符")
    if result_status != "completed" and image is not None:
        raise HTTPException(422, "needs_human_input 和 failed 只能提交报告，不能上传 image")

    stored_task = db.external_ai_task(task_id)
    if not stored_task:
        raise HTTPException(404, "AI 修改任务不存在，请先重新获取任务列表")
    if (
        source_revision != stored_task["source_revision"]
        or source_sha256 != stored_task["source_sha256"]
        or instructions_hash_value != stored_task["instructions_hash"]
    ):
        raise HTTPException(409, "任务快照不匹配或已过期，请重新获取任务")
    task_already_applied = bool(stored_task["status"] == "applied" and stored_task.get("result_id"))
    if task_already_applied and result_status != "completed":
        record = db.external_ai_result_for_task(task_id, applied_only=True)
        return {
            "ok": True,
            "applied": True,
            "idempotent": True,
            "result": record,
            "message": "该任务已经应用过，返回原应用结果",
        }

    source, root, task, asset, _ = external_ai_task_context(task_id, allow_applied=task_already_applied)
    if result_status != "completed":
        try:
            recorded = db.record_external_report(
                task_id,
                _external_audit_base(
                    task,
                    source,
                    result_status,
                    normalized_summary,
                    changes,
                    uncertainties,
                    normalized_provider,
                    normalized_model,
                    "AI 未提交可自动应用的图片结果",
                ),
                "reported",
            )
        except (KeyError, RuntimeError) as error:
            raise HTTPException(409, f"记录 AI 报告时发生冲突：{error}") from error
        if recorded["applied"]:
            return {
                "ok": True,
                "applied": True,
                "idempotent": True,
                "result": recorded["result"],
                "message": "该任务已在并发请求中成功应用，返回原应用结果",
            }
        return {
            "ok": True,
            "applied": False,
            "idempotent": recorded["idempotent"],
            "result": recorded["result"],
            "task_status": recorded["task"]["status"],
            "message": "已记录 AI 报告，原图未修改",
        }

    if uncertainties:
        raise HTTPException(422, "completed 结果的 uncertainties 必须为空")
    if image is None:
        raise HTTPException(422, "result_status=completed 时必须上传 image")
    if image.content_type not in {"image/png", "application/octet-stream"}:
        raise HTTPException(415, "只接受 PNG 图片上传")
    incoming_dir = BASE_DIR / "data" / "ai-incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    uploaded_path = incoming_dir / f"{uuid.uuid4()}.png"
    try:
        received = 0
        with uploaded_path.open("wb") as stream:
            while chunk := await image.read(1024 * 1024):
                received += len(chunk)
                if received > MAX_EXTERNAL_AI_UPLOAD_BYTES:
                    raise AIIntegrationError(
                        f"上传图片超过 {MAX_EXTERNAL_AI_UPLOAD_BYTES // (1024 * 1024)}MB 限制",
                        code="upload_too_large",
                    )
                stream.write(chunk)
        applied = apply_external_result(
            db,
            source,
            root,
            task,
            asset,
            uploaded_path,
            {
                "result_status": result_status,
                "summary": normalized_summary,
                "changes": changes,
                "uncertainties": uncertainties,
                "provider": normalized_provider,
                "model": normalized_model,
            },
        )
        record = applied.get("result")
        applied_asset = applied.get("asset")
        return {
            "ok": True,
            "applied": True,
            "idempotent": bool(applied.get("idempotent")),
            "result": record,
            "old_revision": task["source_revision"],
            "new_revision": applied_asset["revision"] if applied_asset else None,
            "old_status": asset["status"],
            "new_status": applied_asset["status"] if applied_asset else None,
            "backup_path": applied.get("backup_path", ""),
            "validation": applied.get("validation"),
        }
    except AIIntegrationError as error:
        try:
            db.record_external_report(
                task_id,
                _external_audit_base(
                    task,
                    source,
                    "rejected",
                    normalized_summary,
                    changes,
                    uncertainties,
                    normalized_provider,
                    normalized_model,
                    str(error),
                ),
                "rejected",
            )
        except (KeyError, RuntimeError):
            # Preserve the original cause. A concurrent successful apply is
            # already protected by record_external_report's state transition.
            pass
        raise HTTPException(error.status_code, str(error)) from error
    finally:
        if image is not None:
            await image.close()
        uploaded_path.unlink(missing_ok=True)


@app.post("/api/dimension-repair-queue")
def queue_dimension_repairs(body: DimensionRepairQueue):
    """Explicitly turn valid canvas mismatches into normal AI repair tasks."""
    source, root = active()
    all_items = attach_ai_repair_metadata(apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    ), root)
    requested_ids = set(body.asset_ids)
    scoped_items = [
        item for item in all_items
        if body.all_eligible or item.get("id") in requested_ids
    ]
    if not body.all_eligible and not requested_ids:
        raise HTTPException(422, "请选择至少一张尺寸异常图片，或设置 all_eligible=true")

    queued = []
    existing = []
    skipped = []
    candidates = []
    for item in scoped_items:
        repair = item.get("dimension_repair") or {}
        if item.get("inventory_status") != "invalid_dimensions":
            if not body.all_eligible:
                skipped.append({"asset_id": item.get("id"), "reason": "不是尺寸异常图片"})
            continue
        if not repair.get("required") or not item.get("ai_repairable"):
            skipped.append({
                "asset_id": item.get("id"),
                "reason": repair.get("reason") or "不是可自动修复的 PNG 970×600 尺寸异常图片",
            })
            continue
        candidates.append(item)
    if not body.all_eligible:
        for asset_id in requested_ids - {item.get("id") for item in scoped_items}:
            skipped.append({"asset_id": asset_id, "reason": "图片不存在"})
    for item in candidates:
        asset_id = item["id"]
        if item.get("status") == "modified_pending_review":
            skipped.append({"asset_id": asset_id, "reason": "图片已修改，必须先人工复核后才能重新加入尺寸修复队列"})
            continue
        if item.get("status") == "needs_revision":
            existing.append(item)
            continue
        try:
            db.update(asset_id, "needs_revision", str(item.get("comments") or ""), item["revision"], source["id"])
            queued.append(db.asset(asset_id, source["id"]))
        except RuntimeError:
            skipped.append({"asset_id": asset_id, "reason": "图片版本已变化，请刷新后重试"})
        except (KeyError, FileNotFoundError):
            skipped.append({"asset_id": asset_id, "reason": "图片已删除或不可用"})
    return {"queued": queued, "existing": existing, "skipped": skipped}


@app.get("/api/ai/revision-results")
def list_external_ai_revision_results(asset_id: int | None = None, task_id: str | None = None, limit: int = 100):
    source, _ = active()
    if asset_id is not None:
        asset = db.asset(asset_id, source["id"])
        if not asset:
            raise HTTPException(404, "图片不存在")
    if task_id is not None:
        task = db.external_ai_task(task_id, source["id"])
        if not task:
            raise HTTPException(404, "AI 修改任务不存在")
    return db.ai_revision_results(asset_id, source["id"], task_id, max(1, min(limit, 500)))



@app.put("/api/assets/{asset_id}/alt-text")
def update_alt_text(asset_id: int, body: AltTextUpdate):
    source, root = active()
    asset = next(
        (item for item in apply_product_exceptions(
            reconcile(db.assets(source["id"]), load_manifest(root)),
            db.product_exceptions(source["id"]),
        ) if item.get("id") == asset_id),
        None,
    )
    if not asset:
        raise HTTPException(404, "图片不存在")
    if asset.get("asset_role") != "deliverable" or asset.get("inventory_status") != "present":
        raise HTTPException(403, "仅可为清单内且正常的交付图片维护 Alt Text")
    if asset["revision"] != body.revision:
        raise HTTPException(409, "图片已被更新，请刷新后再保存 Alt Text")
    text = " ".join(body.alt_text.split())
    if len(text) > 100:
        raise HTTPException(422, "Alt Text 不能超过 100 个字符")
    # Alt Text 是随成图交付的元数据，不另设草稿/确认工作流。
    db.upsert_alt_text(asset_id, text, "manual", "ready", asset["revision"])
    return {"ok": True, "alt_text": db.alt_text(asset_id)}


@app.post("/api/aplus/deliveries/{sku}")
def create_aplus_delivery(sku: str, body: DeliveryCreate):
    """冻结已审核的 UAE 固定五图交付快照；尚不向 A+ Tool 推送。"""
    source, root = active()
    profile = APLUS_DELIVERY_PROFILES[0]
    normalized_sku = sku.strip()
    if not normalized_sku:
        raise HTTPException(422, "SKU 不能为空")
    reconciled = apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    )
    if normalized_sku in db.product_exceptions(source["id"]):
        raise HTTPException(422, "该产品已标记为产品异常，解除异常后才能创建 A+ 交付版本")
    assets_by_module = {
        reviewer_module(item): item
        for item in reconciled
        if item.get("sku") == normalized_sku and item.get("asset_role") == "deliverable"
    }
    alt_texts = db.alt_texts([item["id"] for item in assets_by_module.values() if item.get("id") is not None])
    slots, errors = delivery_slots_and_errors(profile, assets_by_module, alt_texts)
    if errors:
        raise HTTPException(422, {"message": "尚不满足 A+ 交付条件", "errors": errors})
    asins = list(dict.fromkeys(asin.strip().upper() for asin in body.asins if asin.strip()))
    fingerprint_payload = {"sku": normalized_sku, "profile": profile["id"], "asins": asins, "slots": slots}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = db.delivery_by_fingerprint(fingerprint)
    if existing:
        return {"delivery": existing, "created": False}
    delivery = db.create_delivery(str(uuid.uuid4()), normalized_sku, profile["id"], fingerprint, asins, slots)
    return {"delivery": delivery, "created": True}


@app.post("/api/aplus/batch-sync")
def batch_create_and_sync_aplus_deliveries():
    """Create and ingest every currently eligible SKU using ASINs maintained in the product CSV."""
    source, root = active()
    reconciled = apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    )
    delivery_items = attach_delivery_eligibility(reconciled, source["id"])
    product_info = product_info_by_sku()
    # Batch sync should not cycle every unchanged historical SKU into a new
    # delivery version. Work only on initially deliverable or changed snapshots;
    # the per-SKU creation endpoint remains the final authority for every gate.
    skus = sorted({
        str(item["sku"]) for item in delivery_items
        if item.get("asset_role") == "deliverable"
        and item.get("sku")
        and (item.get("delivery") or {}).get("required")
    })
    synced = []
    skipped = []
    failed = []
    for sku in skus:
        asin = (product_info.get(sku) or {}).get("asin", "").strip().upper()
        if not asin:
            skipped.append({"sku": sku, "reason": "商品 CSV 未维护 ASIN"})
            continue
        try:
            created = create_aplus_delivery(sku, DeliveryCreate(asins=[asin]))
            delivery = created["delivery"]
            sync_result = sync_aplus_delivery(delivery["id"])
            synced.append({
                "sku": sku,
                "delivery_id": delivery["id"],
                "version": delivery["version"],
                "created": created["created"],
                "delivered_assets": sync_result.get("delivered_assets", 0),
            })
        except HTTPException as error:
            detail = error.detail
            if isinstance(detail, dict):
                detail = detail.get("message") or "；".join(detail.get("errors", [])) or "不满足交付条件"
            skipped.append({"sku": sku, "reason": str(detail)})
        except Exception as error:
            failed.append({"sku": sku, "reason": str(error)})
    return {
        "total_skus": len(skus), "synced": synced, "skipped": skipped, "failed": failed,
    }


@app.post("/api/aplus/deliveries/{delivery_id}/sync")
def sync_aplus_delivery(delivery_id: int):
    """将冻结的交付快照推送到 A+ Tool；图片由对方回拉 reviewer 受控内容接口。"""
    with db.connect() as con:
        delivery_row = con.execute("SELECT * FROM aplus_deliveries WHERE id=?", (delivery_id,)).fetchone()
        slots = con.execute(
            """SELECT slots.*, assets.relative_path FROM aplus_delivery_slots AS slots
               JOIN assets ON assets.id=slots.asset_id WHERE slots.delivery_id=? ORDER BY slots.sequence""",
            (delivery_id,),
        ).fetchall()
    if not delivery_row:
        raise HTTPException(404, "交付版本不存在")
    delivery = dict(delivery_row)
    if delivery["status"] not in {"ready_to_sync", "synced"}:
        raise HTTPException(409, "仅可同步已准备好且带有 ASIN 的交付版本")
    if len(slots) != 5:
        raise HTTPException(409, "交付版本槽位不完整")
    tool_config = config.get("aplus_tool", {})
    tool_url = str(tool_config.get("url", "")).rstrip("/")
    token = str(tool_config.get("sync_token", ""))
    if not tool_url or not token:
        raise HTTPException(503, "未配置 A+ Tool 同步地址或令牌")
    server = config.get("server", {})
    reviewer_host = str(server.get("public_url") or f"http://127.0.0.1:{server.get('port', 8700)}").rstrip("/")
    payload = {
        "source_delivery_id": delivery["source_delivery_id"], "sku": delivery["sku"],
        "profile_id": delivery["profile_id"], "fingerprint": delivery["fingerprint"],
        "name": f"{delivery['sku']} A+ delivery v{delivery['version']}", "marketplace_id": "A2VIGQ35RCS4UG",
        "locale": "en_AE", "asins": json.loads(delivery["asins_json"]),
        "slots": [
            {
                "slot_key": slot["slot_key"], "sequence": slot["sequence"], "module": slot["module"],
                "reviewer_asset_id": slot["asset_id"], "revision": slot["revision"], "sha256": slot["sha256"],
                "width": slot["width"], "height": slot["height"],
                "content_type": mimetypes.guess_type(slot["relative_path"])[0] or "image/png",
                "alt_text": slot["alt_text"],
                "content_url": (
                    f"{reviewer_host}/api/integrations/aplus/assets/{slot['asset_id']}/content"
                    f"?revision={slot['revision']}&sha256={slot['sha256']}"
                ),
            }
            for slot in slots
        ],
    }
    request = urllib.request.Request(
        f"{tool_url}/api/deliveries/image-reviewer", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Image-Reviewer-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(tool_config.get("request_timeout_seconds", 30))) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise HTTPException(502, f"A+ Tool 拒绝交付：{detail}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise HTTPException(502, f"无法同步到 A+ Tool：{error}") from error
    with db.connect() as con:
        con.execute("UPDATE aplus_deliveries SET status='synced' WHERE id=?", (delivery_id,))
    delivered_assets = db.mark_delivery_assets_delivered(delivery_id)
    return {"ok": True, "aplus_tool": result, "delivered_assets": delivered_assets}


@app.get("/api/aplus/deliveries/{sku}")
def list_aplus_deliveries(sku: str):
    with db.connect() as con:
        deliveries = [dict(row) for row in con.execute(
            "SELECT * FROM aplus_deliveries WHERE sku=? ORDER BY version DESC", (sku.strip(),)
        )]
    for delivery in deliveries:
        delivery["asins"] = json.loads(delivery.pop("asins_json"))
        with db.connect() as con:
            delivery["slots"] = [dict(row) for row in con.execute(
                "SELECT * FROM aplus_delivery_slots WHERE delivery_id=? ORDER BY sequence", (delivery["id"],)
            )]
    return {"items": deliveries}


@app.get("/api/integrations/aplus/assets")
def list_aplus_integration_assets(sku: str = ""):
    """只读集成契约：返回当前图片源可供 A+ 交付评估的图片元数据。"""
    source, root = active()
    items = reconcile(db.assets(source["id"]), load_manifest(root))
    normalized_sku = sku.strip()
    result = []
    for item in items:
        if item.get("asset_role") != "deliverable" or item.get("id") is None:
            continue
        if normalized_sku and item.get("sku") != normalized_sku:
            continue
        content_type = mimetypes.guess_type(item.get("relative_path", ""))[0]
        if content_type not in {"image/jpeg", "image/png"}:
            content_type = None
        module_match = re.search(r"(A\+L\d+)$", item["module"])
        result.append({
            "reviewer_asset_id": item["id"],
            "sku": item["sku"],
            "module": module_match.group(1) if module_match else item["module"],
            "relative_path": item["relative_path"],
            "revision": item["revision"],
            "reviewed_revision": item["reviewed_revision"],
            "review_status": item["status"],
            "inventory_status": item.get("inventory_status"),
            "deliverable": True,
            "sha256": item["sha256"],
            "size": item["size"],
            "width": item["width"],
            "height": item["height"],
            "content_type": content_type,
            "image_url": f"/api/integrations/aplus/assets/{item['id']}/content",
        })
    return {"source_id": source["id"], "source_name": source["name"], "delivery_profiles": APLUS_DELIVERY_PROFILES, "items": result}


@app.get("/api/integrations/aplus/assets/{asset_id}/content")
def get_aplus_integration_asset_content(asset_id: int, revision: int | None = None, sha256: str = ""):
    """Return only the exact current byte snapshot requested by an integration.

    A delivery sync supplies revision/SHA query parameters captured in its
    immutable slot. A later AI repair therefore returns 409 instead of serving
    new unreviewed bytes through an old delivery URL.
    """
    source, root = active()
    reconciled = apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    )
    asset = next((item for item in reconciled if item.get("id") == asset_id), None)
    if not asset:
        raise HTTPException(404, "图片不存在")
    if asset.get("asset_role") != "deliverable" or asset.get("inventory_status") != "present":
        raise HTTPException(403, "该图片不是可交付的当前素材")
    if revision is not None and asset.get("revision") != revision:
        raise HTTPException(409, "图片 revision 已变化，交付快照失效")
    if sha256 and asset.get("sha256") != sha256.lower():
        raise HTTPException(409, "图片 SHA-256 已变化，交付快照失效")
    target = _safe_asset_path(root, str(asset.get("relative_path") or ""))
    if not target:
        raise HTTPException(404, "图片文件不存在")
    content_type = mimetypes.guess_type(target.name)[0]
    if content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(415, "集成仅支持 JPEG 或 PNG 图片")
    return FileResponse(
        target,
        media_type=content_type,
        headers={
            "Cache-Control": "no-cache",
            "ETag": f'"sha256-{str(asset.get("sha256") or "")}"',
            "X-Image-Reviewer-Revision": str(asset.get("revision") or 0),
            "X-Image-Reviewer-SHA256": str(asset.get("sha256") or ""),
        },
    )


@app.get("/api/overview")
def get_overview():
    source, root = active()
    manifest = load_manifest(root)
    current_manifest_error = manifest_error(manifest)
    items = apply_product_exceptions(
        reconcile(db.assets(source["id"]), manifest),
        db.product_exceptions(source["id"]),
    )
    review_counts = {key: sum(item.get("status") == key for item in items if item.get("asset_role") == "deliverable") for key in STATUSES}
    inventory_counts = {key: sum(item.get("inventory_status") == key for item in items) for key in INVENTORY_STATUSES if key != "all"}
    reference_count = sum(item.get("asset_role") == "reference" for item in items)
    deliverable_items = attach_delivery_eligibility(items, source["id"])
    current_delivery_counts = delivery_counts(deliverable_items)
    deliverable_skus = current_delivery_counts["deliverable"]
    return {
        "source": {"id": source["id"], "name": source["name"], "path": source["path"], "last_scanned_at": source.get("last_scanned_at")},
        "manifest": bool(manifest) and not current_manifest_error,
        "manifest_path": str(root / "_image-reviewer-manifest.json") if manifest is not None else "",
        "manifest_error": current_manifest_error or "",
        "total_images": sum(item.get("id") is not None and not item.get("missing") for item in items),
        "expected_images": sum(bool(item.get("expected")) for item in items), "reference_images": reference_count,
        "product_count": len({item.get("sku") for item in items if item.get("asset_role") == "deliverable" and item.get("sku")}),
        "deliverable_skus": deliverable_skus,
        "delivery_counts": current_delivery_counts,
        "review_counts": review_counts,
        "inventory_counts": inventory_counts,
    }


@app.get("/api/inventory")
def get_inventory():
    source, root = active()
    manifest = load_manifest(root)
    current_manifest_error = manifest_error(manifest)
    items = reconcile(db.assets(source["id"]), manifest)
    items = apply_product_exceptions(items, db.product_exceptions(source["id"]))
    counts = {
        key: sum(item["inventory_status"] == key for item in items)
        for key in INVENTORY_STATUSES
        if key != "all"
    }
    return {
        "manifest": bool(manifest) and not current_manifest_error,
        "manifest_error": current_manifest_error or "",
        "expected": sum(1 for item in items if item["expected"]),
        **counts,
    }


@app.get("/api/missing-assets")
def get_missing_assets():
    source, root = active()
    items = apply_product_exceptions(
        reconcile(db.assets(source["id"]), load_manifest(root)),
        db.product_exceptions(source["id"]),
    )
    return [
        item for item in items
        if item["inventory_status"] in {
            "missing", "blocked", "invalid_dimensions", "invalid_manifest", "wrong_path", "duplicate_module",
        }
    ]


@app.get("/images/{path:path}")
def image(path: str):
    _, root = active()
    target = root / path
    if target.is_symlink():
        raise HTTPException(404)
    target = target.resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/suggestions")
def get_suggestions():
    return db.suggestions()


@app.post("/api/suggestions", status_code=201)
def create_suggestion(body: Suggestion):
    title, content = body.title.strip(), body.content.strip()
    if not title or not content:
        raise HTTPException(422, "按钮名称和插入文案不能为空")
    return db.create_suggestion(title, content)


@app.put("/api/suggestions/{suggestion_id}")
def update_suggestion(suggestion_id: int, body: Suggestion):
    title, content = body.title.strip(), body.content.strip()
    if not title or not content:
        raise HTTPException(422, "按钮名称和插入文案不能为空")
    try:
        return db.update_suggestion(suggestion_id, title, content)
    except KeyError:
        raise HTTPException(404, "快捷评论不存在")


@app.delete("/api/suggestions/{suggestion_id}")
def delete_suggestion(suggestion_id: int):
    try:
        db.delete_suggestion(suggestion_id)
    except KeyError:
        raise HTTPException(404, "快捷评论不存在")
    return {"ok": True}


@app.post("/api/suggestions/{suggestion_id}/move")
def move_suggestion(suggestion_id: int, body: SuggestionMove):
    try:
        return db.move_suggestion(suggestion_id, body.direction)
    except KeyError:
        raise HTTPException(404, "快捷评论不存在")


@app.get("/api/revision-tasks")
def tasks():
    source, _ = active()
    return db.revision_tasks(source["id"])


@app.get("/api/export/revision-tasks.md")
def export_md():
    source, _ = active()
    lines = [f"# A+ 图片修改任务 - {source['name']}", ""]
    for item in db.revision_tasks(source["id"]):
        lines += [
            f"## {item['sku']} / {item['module']}",
            f"图片：`{item['image_path']}`",
            "",
            "修改要求：",
            *[f"- {instruction}" for instruction in item["instructions"]],
            "",
        ]
    return JSONResponse({"content": "\n".join(lines)})


def run():
    uvicorn.run(app, host=config["server"]["host"], port=config["server"]["port"])
