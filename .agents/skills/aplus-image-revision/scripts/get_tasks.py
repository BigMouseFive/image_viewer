#!/usr/bin/env python3
"""Retrieve external AI image-revision tasks from image-reviewer.

This script intentionally uses only Python's standard-library urllib. With
--download-dir it saves each self-contained task snapshot plus its target and
available product-main reference image; it never writes to the production
asset path returned by the server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_REVIEWER_URL = "http://127.0.0.1:8700"
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "aplus-image-revision-get-tasks/1.0"
ALLOWED_STATUS = "needs_revision"


class ScriptError(RuntimeError):
    """A user-actionable local or HTTP failure."""


class HttpResponseError(ScriptError):
    def __init__(self, status: int, response: Any):
        self.status = status
        self.response = response
        super().__init__(f"image-reviewer returned HTTP {status}")


def reviewer_url() -> str:
    value = os.environ.get("IMAGE_REVIEWER_URL", DEFAULT_REVIEWER_URL).strip()
    return (value or DEFAULT_REVIEWER_URL).rstrip("/")


def decode_response(data: bytes, content_type: str | None = None) -> Any:
    charset = "utf-8"
    if content_type:
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "charset":
                charset = value.strip().strip('"') or charset
                break
    text = data.decode(charset, errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def get_json(url: str) -> dict[str, Any]:
    req = request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = decode_response(response.read(), response.headers.get("Content-Type"))
    except error.HTTPError as exc:
        raise HttpResponseError(
            exc.code,
            decode_response(exc.read(), exc.headers.get("Content-Type") if exc.headers else None),
        ) from exc
    except error.URLError as exc:
        raise ScriptError(f"Cannot connect to image-reviewer at {url}: {exc.reason}") from exc
    except OSError as exc:
        raise ScriptError(f"Request to image-reviewer failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise ScriptError("Expected a JSON object from the task-list endpoint")
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "task"


def suffix_for(info: dict[str, Any], fallback: str) -> str:
    relative_path = info.get("relative_path")
    if isinstance(relative_path, str):
        suffix = Path(relative_path).suffix.lower()
        if suffix and len(suffix) <= 10:
            return suffix
    return fallback


def download_file(url: str, destination: Path) -> dict[str, Any]:
    """Download atomically and return byte/hash/header metadata."""
    req = request.Request(url, headers={"Accept": "image/*,*/*;q=0.8", "User-Agent": USER_AGENT})
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            destination.parent.mkdir(parents=True, exist_ok=True)
            received = 0
            with temporary.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                    received += len(chunk)
            temporary.replace(destination)
            return {
                "path": str(destination.resolve()),
                "bytes": received,
                "sha256": file_sha256(destination),
                "content_type": response.headers.get("Content-Type"),
                "revision": response.headers.get("X-Image-Reviewer-Revision"),
                "source_sha256": response.headers.get("X-Image-Reviewer-SHA256"),
            }
    except error.HTTPError as exc:
        raise HttpResponseError(
            exc.code,
            decode_response(exc.read(), exc.headers.get("Content-Type") if exc.headers else None),
        ) from exc
    except error.URLError as exc:
        raise ScriptError(f"Cannot download {url}: {exc.reason}") from exc
    except OSError as exc:
        raise ScriptError(f"Cannot save {destination}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def download_task_assets(task: dict[str, Any], download_root: Path) -> None:
    task_id = task.get("task_id")
    target_info = task.get("target_image") or task.get("image")
    reference_info = task.get("main_image") or task.get("reference_image")
    if not isinstance(task_id, str) or not task_id:
        raise ScriptError("Task response is missing task_id")
    if not isinstance(target_info, dict) or not isinstance(target_info.get("url"), str):
        raise ScriptError(f"Task {task_id} is missing target_image.url")

    task_dir = download_root / safe_component(task_id)
    target_path = task_dir / f"target{suffix_for(target_info, '.png')}"
    target_download = download_file(target_info["url"], target_path)

    expected_sha256 = task.get("sha256")
    expected_revision = task.get("revision")
    if isinstance(expected_sha256, str) and expected_sha256:
        if target_download["sha256"].lower() != expected_sha256.lower():
            target_path.unlink(missing_ok=True)
            raise ScriptError(
                f"Downloaded target for {task_id} does not match task sha256; the task is stale. Fetch it again."
            )
    if target_download["source_sha256"] and isinstance(expected_sha256, str):
        if target_download["source_sha256"].lower() != expected_sha256.lower():
            target_path.unlink(missing_ok=True)
            raise ScriptError(
                f"Target response header for {task_id} does not match task sha256; the task is stale. Fetch it again."
            )
    if target_download["revision"] is not None and expected_revision is not None:
        if target_download["revision"] != str(expected_revision):
            target_path.unlink(missing_ok=True)
            raise ScriptError(
                f"Target response revision for {task_id} does not match the task snapshot. Fetch it again."
            )

    downloads: dict[str, Any] = {"target_image": target_download, "main_image": None}
    if isinstance(reference_info, dict) and isinstance(reference_info.get("url"), str) and reference_info["url"]:
        reference_path = task_dir / f"reference{suffix_for(reference_info, '.img')}"
        reference_download = download_file(reference_info["url"], reference_path)
        expected_reference_sha256 = reference_info.get("sha256")
        if isinstance(expected_reference_sha256, str) and expected_reference_sha256:
            if reference_download["sha256"].lower() != expected_reference_sha256.lower():
                reference_path.unlink(missing_ok=True)
                raise ScriptError(
                    f"Downloaded product reference for {task_id} does not match its frozen sha256; fetch a new task."
                )
            if reference_download["source_sha256"] and reference_download["source_sha256"].lower() != expected_reference_sha256.lower():
                reference_path.unlink(missing_ok=True)
                raise ScriptError(
                    f"Product reference response header for {task_id} does not match its frozen sha256; fetch a new task."
                )
        downloads["main_image"] = reference_download

    # Keep client-only download metadata out of the original list response.
    # The per-task snapshot is intentionally enriched for submit_result.py.
    local_task = dict(task)
    local_task["downloads"] = downloads
    write_json(task_dir / "task.json", local_task)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch image-reviewer external AI revision tasks as JSON.")
    parser.add_argument(
        "--status",
        default="needs_revision",
        help="Must be needs_revision; 已修改 images must be reviewed by a human before another AI task",
    )
    parser.add_argument("--sku", default="", help="Case-insensitive SKU substring filter")
    parser.add_argument("--limit", type=int, default=60, help="Page size (image-reviewer clamps this to 1..200)")
    parser.add_argument("--offset", type=int, default=0, help="Zero-based page offset")
    parser.add_argument("--output", type=Path, help="Also write the full JSON list response to this file")
    parser.add_argument(
        "--download-dir",
        type=Path,
        help="Download each task's target/reference and write <task_id>/task.json beneath this directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status.strip() != ALLOWED_STATUS:
        print(
            json.dumps({"ok": False, "error": "--status must be needs_revision; 已修改 images are human-review only"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    if args.limit < 1:
        print(json.dumps({"ok": False, "error": "--limit must be at least 1"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.offset < 0:
        print(json.dumps({"ok": False, "error": "--offset must be at least 0"}, ensure_ascii=False), file=sys.stderr)
        return 2

    params: dict[str, str | int] = {
        "status": args.status,
        "limit": args.limit,
        "offset": args.offset,
    }
    if args.sku:
        params["sku"] = args.sku
    url = f"{reviewer_url()}/api/ai/revision-tasks?{parse.urlencode(params)}"

    try:
        payload = get_json(url)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ScriptError("Task-list response has no JSON array field named items")
        if args.download_dir:
            for task in items:
                if not isinstance(task, dict):
                    raise ScriptError("Task-list response contains a non-object task")
                download_task_assets(task, args.download_dir)
        if args.output:
            write_json(args.output, payload)
    except HttpResponseError as exc:
        print(
            json.dumps({"ok": False, "status": exc.status, "response": exc.response}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1
    except ScriptError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
