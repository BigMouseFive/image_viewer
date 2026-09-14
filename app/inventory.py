from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

MANIFEST_NAME = "_image-reviewer-manifest.json"
MANIFEST_ERROR_KEY = "_image_reviewer_manifest_error"
MODULES = [
    {"id": "A+L01", "width": 970, "height": 600},
    {"id": "A+L02", "width": 970, "height": 600},
    {"id": "A+L03", "width": 970, "height": 600},
    {"id": "A+L04", "width": 970, "height": 600},
    {"id": "A+L05", "width": 970, "height": 600},
]

# A wrong canvas is actionable: an external image editor can regenerate it at
# the manifest dimensions. Other inventory errors remain read-only because they
# need a human to resolve the file/path/manifest problem first.
AI_REPAIRABLE_INVENTORY_STATUSES = {"present", "invalid_dimensions"}
# A dimension repair is explicit human intent: the UI changes it to
# needs_revision without a confirmation dialog, then the normal external queue
# can batch-process it. This never silently rewrites approved/delivered assets.
DIMENSION_REPAIR_REVIEW_STATUSES = {"needs_revision"}


def _reference_sku(item: dict[str, Any]) -> str | None:
    relative_path = Path(str(item.get("relative_path") or ""))
    return relative_path.stem if relative_path.parts[:1] == ("_refs",) else None


def _is_reference(item: dict[str, Any]) -> bool:
    return _reference_sku(item) is not None


def _reviewer_module(value: str) -> str:
    match = re.search(r"(A\+L\d+)$", value or "")
    return match.group(1) if match else value


def _manifest_error(message: str) -> dict[str, Any]:
    return {MANIFEST_ERROR_KEY: message, "items": []}


def manifest_error(manifest: dict[str, Any] | None) -> str | None:
    if isinstance(manifest, dict):
        value = manifest.get(MANIFEST_ERROR_KEY)
        return str(value) if value else None
    return None


def _normal_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path 不能为空")
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"path 不安全：{value!r}")
    normalized = candidate.as_posix()
    if normalized == "." or normalized.startswith("../"):
        raise ValueError(f"path 不安全：{value!r}")
    return normalized


def _validated_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("根对象必须是 JSON 对象")
    if data.get("schema_version") != 1:
        raise ValueError("schema_version 必须为 1")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("items 必须是数组")

    items = []
    paths: set[str] = set()
    modules: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_items, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{index}] 必须是对象")
        sku = str(raw.get("sku") or "").strip()
        module = str(raw.get("module") or "").strip()
        if not sku or not module:
            raise ValueError(f"items[{index}] 缺少 sku 或 module")
        path = _normal_relative_path(raw.get("path"))
        width, height = raw.get("width"), raw.get("height")
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise ValueError(f"items[{index}] 的 width 必须是正整数")
        if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
            raise ValueError(f"items[{index}] 的 height 必须是正整数")
        canonical_module = _reviewer_module(module)
        canonical_spec = next((definition for definition in MODULES if definition["id"] == canonical_module), None)
        if canonical_spec and (width, height) != (canonical_spec["width"], canonical_spec["height"]):
            raise ValueError(
                f"items[{index}] 的 {canonical_module} 必须为 {canonical_spec['width']}×{canonical_spec['height']}"
            )
        key = (sku, canonical_module)
        if path in paths:
            raise ValueError(f"清单存在重复 path：{path}")
        if key in modules:
            raise ValueError(f"清单存在重复 SKU/模块：{sku} / {key[1]}")
        paths.add(path)
        modules.add(key)
        items.append({**raw, "sku": sku, "module": module, "path": path, "width": width, "height": height})
    return {**data, "items": items}


def load_manifest(root: Path) -> dict[str, Any] | None:
    """Load a manifest while making an invalid existing manifest fail closed.

    ``None`` retains the established no-manifest behaviour. A manifest that
    exists but cannot be parsed/validated returns a sentinel consumed by
    :func:`reconcile`, so it cannot accidentally turn arbitrary files into
    normal deliverable assets.
    """
    path = root / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return _validated_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return _manifest_error(f"清单无效：{error}")


def _inventory_item(item: dict[str, Any], **values: Any) -> dict[str, Any]:
    return dict(item, **values)


def is_dimension_repair(item: dict[str, Any]) -> bool:
    """Whether this is a repairable A+ canvas mismatch, not a manifest defect."""
    width, height = item.get("width"), item.get("height")
    return (
        item.get("asset_role") == "deliverable"
        and item.get("inventory_status") == "invalid_dimensions"
        and Path(str(item.get("relative_path") or "")).suffix.lower() == ".png"
        and isinstance(width, int) and width > 0
        and isinstance(height, int) and height > 0
        and (item.get("expected_width") or 970) == 970
        and (item.get("expected_height") or 600) == 600
    )


def is_standard_aplus_canvas(item: dict[str, Any]) -> bool:
    """Whether a normal revision already belongs to this fixed A+ canvas."""
    expected_width = item.get("expected_width")
    expected_height = item.get("expected_height")
    return (
        Path(str(item.get("relative_path") or "")).suffix.lower() == ".png"
        and item.get("width") == 970
        and item.get("height") == 600
        and (expected_width in {None, 970})
        and (expected_height in {None, 600})
    )


def is_ai_repairable_asset(item: dict[str, Any]) -> bool:
    """Whether a delivery asset can be sent to the controlled AI repair flow."""
    return (
        item.get("asset_role") == "deliverable"
        and (
            (item.get("inventory_status") == "present" and is_standard_aplus_canvas(item))
            or is_dimension_repair(item)
        )
    )


def dimension_repair_instruction(item: dict[str, Any]) -> str:
    """Return the system requirement for a wrong-canvas A+ asset."""
    actual_width = item.get("width") or "未知"
    actual_height = item.get("height") or "未知"
    expected_width = item.get("expected_width") or 970
    expected_height = item.get("expected_height") or 600
    return (
        f"尺寸异常：当前画布为 {actual_width}×{actual_height}，清单要求 {expected_width}×{expected_height}。"
        f"请输出真实 PNG {expected_width}×{expected_height}，保持产品比例、完整内容和可读文字，不要仅拉伸产品。"
    )


def dimension_repair_context(item: dict[str, Any]) -> dict[str, Any] | None:
    if not is_dimension_repair(item):
        return None
    return {
        "required": True,
        "current_dimensions": {"width": item.get("width"), "height": item.get("height")},
        "expected_dimensions": {
            "width": item.get("expected_width") or 970,
            "height": item.get("expected_height") or 600,
        },
        "instruction": dimension_repair_instruction(item),
    }


def is_dimension_repair_queueable(item: dict[str, Any]) -> bool:
    return is_dimension_repair(item) and item.get("status") in DIMENSION_REPAIR_REVIEW_STATUSES


def attach_ai_repair_metadata(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose repairability to the UI without making other errors editable."""
    return [
        dict(
            item,
            dimension_repair=dimension_repair_context(item),
            ai_repairable=is_ai_repairable_asset(item),
        )
        for item in items
    ]


def dimension_repair_matches(item: dict[str, Any], snapshot: Any) -> bool:
    """Whether an active task still describes this same size-repair problem."""
    if not isinstance(snapshot, dict) or not snapshot.get("required"):
        return False
    return dimension_repair_context(item) == snapshot


def _reference_item(item: dict[str, Any]) -> dict[str, Any]:
    return _inventory_item(
        item,
        inventory_status="extra",
        asset_role="reference",
        expected=False,
        expected_width=None,
        expected_height=None,
        reference_sku=_reference_sku(item),
    )


def reconcile(items: list[dict[str, Any]], manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Attach inventory roles/statuses without letting filename suffixes bypass a manifest.

    A valid manifest owns the exact relative path for every delivery slot.
    Files with the same SKU/module but another path are never deliverable, and
    duplicate module files make the intended slot non-deliverable as well.
    """
    if manifest is None:
        # The legacy no-manifest mode treats scanned files as delivery assets,
        # but it must still not silently choose one when a SKU/module appears
        # more than once. That would make five-image delivery nondeterministic.
        module_counts: dict[tuple[str, str], int] = {}
        for item in items:
            if _is_reference(item):
                continue
            key = (str(item.get("sku") or ""), _reviewer_module(str(item.get("module") or "")))
            module_counts[key] = module_counts.get(key, 0) + 1
        result = []
        for item in items:
            if _is_reference(item):
                result.append(_reference_item(item))
                continue
            key = (str(item.get("sku") or ""), _reviewer_module(str(item.get("module") or "")))
            duplicate = module_counts[key] > 1
            result.append(_inventory_item(
                item,
                inventory_status="duplicate_module" if duplicate else "present",
                asset_role="deliverable",
                expected=True,
                expected_width=item.get("width"),
                expected_height=item.get("height"),
                reference_sku=None,
                reason=f"{key[0]} / {key[1]} 存在多个实际文件" if duplicate else item.get("reason", ""),
            ))
        return result

    error = manifest_error(manifest)
    if error:
        return [
            _reference_item(item)
            if _is_reference(item)
            else _inventory_item(
                item,
                inventory_status="invalid_manifest",
                asset_role="other",
                expected=False,
                expected_width=None,
                expected_height=None,
                reference_sku=None,
                reason=error,
            )
            for item in items
        ]

    expected_by_path: dict[str, dict[str, Any]] = {
        item["path"]: item for item in manifest.get("items", [])
    }
    expected_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (item["sku"], _reviewer_module(item["module"])): item
        for item in manifest.get("items", [])
    }
    actual_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    actual_paths: set[str] = set()
    for item in items:
        if _is_reference(item):
            continue
        relative_path = str(item.get("relative_path") or "")
        actual_paths.add(relative_path)
        key = (str(item.get("sku") or ""), _reviewer_module(str(item.get("module") or "")))
        actual_by_key.setdefault(key, []).append(item)

    result: list[dict[str, Any]] = []
    for item in items:
        if _is_reference(item):
            result.append(_reference_item(item))
            continue
        relative_path = str(item.get("relative_path") or "")
        key = (str(item.get("sku") or ""), _reviewer_module(str(item.get("module") or "")))
        wanted = expected_by_path.get(relative_path)
        key_expected = expected_by_key.get(key)
        same_module = actual_by_key.get(key, [])
        if wanted:
            if len(same_module) > 1:
                status = "duplicate_module"
                reason = f"{key[0]} / {key[1]} 存在多个实际文件"
            elif (item.get("width"), item.get("height")) != (wanted["width"], wanted["height"]):
                status = "invalid_dimensions"
                reason = ""
            else:
                status = "present"
                reason = ""
            result.append(_inventory_item(
                item,
                inventory_status=status,
                asset_role="deliverable",
                expected=True,
                expected_width=wanted["width"],
                expected_height=wanted["height"],
                expected_path=wanted["path"],
                reference_sku=None,
                reason=reason or item.get("reason", ""),
            ))
            continue

        if key_expected:
            status = "duplicate_module" if len(same_module) > 1 else "wrong_path"
            reason = f"清单要求路径：{key_expected['path']}"
        else:
            status = "extra"
            reason = item.get("reason", "")
        result.append(_inventory_item(
            item,
            inventory_status=status,
            asset_role="other",
            expected=False,
            expected_width=None,
            expected_height=None,
            reference_sku=None,
            reason=reason,
        ))

    for path, wanted in expected_by_path.items():
        if path in actual_paths:
            continue
        status = "blocked" if wanted.get("generation_status") == "blocked" else "missing"
        result.append({
            "id": None,
            "source_id": None,
            "sku": wanted["sku"],
            "module": wanted["module"],
            "relative_path": path,
            "size": 0,
            "mtime": None,
            "sha256": "",
            "width": None,
            "height": None,
            "revision": 0,
            "status": "unreviewed",
            "comments": "",
            "reviewed_revision": -1,
            "missing": 1,
            "inventory_status": status,
            "expected": True,
            "expected_width": wanted["width"],
            "expected_height": wanted["height"],
            "expected_path": path,
            "generation_status": wanted.get("generation_status", "ready"),
            "reason": wanted.get("reason", ""),
            "asset_role": "deliverable",
            "reference_sku": None,
        })
    return result


def reference_images(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    references: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("asset_role") == "reference" and item.get("reference_sku"):
            references.setdefault(item["reference_sku"], []).append(item)
    for values in references.values():
        values.sort(key=lambda item: item["relative_path"])
    return references


def attach_references(items: list[dict[str, Any]], references: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [dict(item, reference_images=references.get(item["sku"], [])) for item in items]


def apply_product_exceptions(items, exception_skus):
    return [
        dict(item, inventory_status="product_exception", product_exception=True)
        if item.get("sku") in exception_skus and item.get("asset_role") == "deliverable"
        else dict(item, product_exception=False)
        for item in items
    ]


def filter_inventory(items, statuses=None, q=""):
    statuses = set(statuses or ())
    if statuses and "all" not in statuses:
        items = [x for x in items if x["inventory_status"] in statuses]
    if q:
        needle = q.lower()
        items = [x for x in items if needle in x["sku"].lower() or needle in x["module"].lower() or needle in x["relative_path"].lower()]
    return sorted(items, key=lambda x: (x["sku"], x["module"], x["relative_path"]))
