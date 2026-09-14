from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any
from urllib.error import HTTPError, URLError

from PIL import Image
from urllib.request import Request, urlopen


class IOPaintError(RuntimeError):
    pass


def resolve_editable_asset(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    unresolved = root / relative_path
    if unresolved.is_symlink():
        raise ValueError("不允许编辑符号链接图片")
    target = unresolved.resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(relative_path)
    return target


def inspect_editable_image(root: Path, relative_path: str) -> tuple[Path, dict[str, Any]]:
    """Safely decode image metadata from a reviewer-managed source file."""
    target = resolve_editable_asset(root, relative_path)
    try:
        with Image.open(target) as image:
            image_format = image.format
            width, height = image.size
            image.load()
    except (OSError, ValueError) as error:
        raise IOPaintError(f"图片无法解码：{error}") from error
    return target, {"format": image_format, "width": width, "height": height}


def backup_asset(target: Path, backup_root: Path, source_id: int, asset: dict) -> Path:
    suffix = target.suffix.lower()
    digest = str(asset.get("sha256") or "unknown")[:12]
    revision = int(asset.get("revision") or 0)
    directory = backup_root / str(source_id) / str(asset["id"])
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"revision-{revision}-{digest}{suffix}"
    if not backup.exists():
        shutil.copy2(target, backup)
    return backup


def set_iopaint_input(base_url: str, target: Path, timeout: float = 5.0) -> None:
    endpoint = f"{base_url.rstrip('/')}/api/v1/set-input"
    request = Request(
        endpoint,
        data=json.dumps({"path": str(target)}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise IOPaintError(f"IOPaint 返回异常状态：{response.status}")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise IOPaintError(f"IOPaint 拒绝打开图片（{error.code}）：{detail or error.reason}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise IOPaintError("无法连接 IOPaint，请确认 5055 服务已经启动") from error
