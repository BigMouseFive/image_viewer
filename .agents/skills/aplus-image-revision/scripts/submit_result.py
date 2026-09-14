#!/usr/bin/env python3
"""Submit a completed or report-only external AI revision result.

The script uses a small standard-library multipart/form-data encoder rather
than requests. A completed candidate is locally checked with validate_image.py
before any HTTP request; report-only statuses deliberately omit the image part.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from validate_image import ImageValidationError, validate_image


DEFAULT_REVIEWER_URL = "http://127.0.0.1:8700"
REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = "aplus-image-revision-submit-result/1.0"
RESULT_STATUSES = ("completed", "needs_human_input", "failed")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ScriptError(RuntimeError):
    """A user-actionable local error."""


class HttpResponseError(ScriptError):
    def __init__(self, status: int, response: Any):
        self.status = status
        self.response = response
        super().__init__(f"image-reviewer returned HTTP {status}")


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


def parse_json_array(value: str, argument: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ScriptError(f"{argument} must be a JSON array: {exc.msg}") from exc
    if not isinstance(parsed, list):
        raise ScriptError(f"{argument} must be a JSON array")
    return parsed


def select_task(payload: Any) -> dict[str, Any]:
    """Accept a direct task object or a one-item GET list response."""
    if isinstance(payload, dict) and isinstance(payload.get("task_id"), str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ScriptError(
                "Task file is a list response with zero or multiple items. Use one task object, "
                "for example <download-dir>/<task_id>/task.json."
            )
        return items[0]
    raise ScriptError("Task JSON must be a task object or a one-item revision-tasks response")


def task_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScriptError(f"Task JSON file does not exist: {path}") from exc
    except OSError as exc:
        raise ScriptError(f"Cannot read task JSON {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Task JSON is invalid: {exc}") from exc

    task = select_task(payload)
    task_id = task.get("task_id")
    revision = task.get("revision")
    source_sha256 = task.get("sha256")
    instructions_hash = task.get("instructions_hash")

    if not isinstance(task_id, str) or not task_id.strip():
        raise ScriptError("Task is missing a non-empty task_id")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ScriptError("Task is missing a non-negative integer revision")
    for name, value in (("sha256", source_sha256), ("instructions_hash", instructions_hash)):
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise ScriptError(f"Task field {name} must be a 64-character SHA-256 hex digest")

    return {
        "task_id": task_id,
        "revision": revision,
        "sha256": source_sha256,
        "instructions_hash": instructions_hash,
        "result_submit_url": task.get("result_submit_url"),
    }


def default_base_url() -> str:
    value = os.environ.get("IMAGE_REVIEWER_URL", DEFAULT_REVIEWER_URL).strip()
    return (value or DEFAULT_REVIEWER_URL).rstrip("/")


def normalize_submit_url(value: str) -> str:
    """Allow --url to be either a base reviewer URL or the full endpoint URL."""
    candidate = value.strip()
    if not candidate:
        raise ScriptError("Submission URL cannot be empty")
    parsed = parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ScriptError("Submission URL must be an absolute http(s) URL")
    if parsed.path.rstrip("/").endswith("/api/ai/revision-results"):
        return candidate.rstrip("/")
    return candidate.rstrip("/") + "/api/ai/revision-results"


def submit_url(override: str | None, task: dict[str, Any]) -> str:
    if override:
        return normalize_submit_url(override)
    task_url = task.get("result_submit_url")
    if isinstance(task_url, str) and task_url.strip():
        return normalize_submit_url(task_url)
    return normalize_submit_url(default_base_url())


def encode_multipart(
    fields: list[tuple[str, str]],
    image_path: Path | None,
) -> tuple[bytes, str]:
    """Build a multipart body using only the standard library.

    Completed candidates are capped at 30 MiB by local validation, so holding
    the complete body in memory keeps the implementation simple and bounded.
    """
    boundary = f"----aplus-image-revision-{uuid.uuid4().hex}"
    body = io.BytesIO()

    for name, value in fields:
        body.write(f"--{boundary}\r\n".encode("ascii"))
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.write(value.encode("utf-8"))
        body.write(b"\r\n")

    if image_path is not None:
        body.write(f"--{boundary}\r\n".encode("ascii"))
        # A stable ASCII filename avoids malformed multipart headers when a
        # local candidate filename contains non-ASCII characters. The server
        # validates bytes and MIME, not the supplied filename.
        body.write(b'Content-Disposition: form-data; name="image"; filename="candidate.png"\r\n')
        body.write(b"Content-Type: image/png\r\n\r\n")
        with image_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                body.write(chunk)
        body.write(b"\r\n")

    body.write(f"--{boundary}--\r\n".encode("ascii"))
    return body.getvalue(), boundary


def post_multipart(url: str, body: bytes, boundary: str) -> Any:
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return decode_response(response.read(), response.headers.get("Content-Type"))
    except error.HTTPError as exc:
        raise HttpResponseError(
            exc.code,
            decode_response(exc.read(), exc.headers.get("Content-Type") if exc.headers else None),
        ) from exc
    except error.URLError as exc:
        raise ScriptError(f"Cannot connect to image-reviewer at {url}: {exc.reason}") from exc
    except OSError as exc:
        raise ScriptError(f"Request to image-reviewer failed: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit an image-reviewer external AI revision result.")
    parser.add_argument("--task", required=True, type=Path, help="Task JSON object or one-item list response")
    parser.add_argument("--image", type=Path, help="Candidate PNG; required only when --status completed")
    parser.add_argument("--status", choices=RESULT_STATUSES, default="completed", help="Result status")
    parser.add_argument("--summary", required=True, help="Non-empty concise result summary")
    parser.add_argument("--changes", default="[]", help="JSON array describing concrete changes")
    parser.add_argument("--uncertainties", default="[]", help="JSON array of unresolved uncertainties")
    parser.add_argument("--provider", default="", help="Optional generation/editing provider")
    parser.add_argument("--model", default="", help="Optional model identifier")
    parser.add_argument(
        "--url",
        help="Override result URL; accepts either reviewer base URL or full /api/ai/revision-results URL",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        task = task_snapshot(args.task)
        summary = args.summary.strip()
        if not summary:
            raise ScriptError("--summary must not be empty")
        changes = parse_json_array(args.changes, "--changes")
        uncertainties = parse_json_array(args.uncertainties, "--uncertainties")

        image_path: Path | None = None
        if args.status == "completed":
            if args.image is None:
                raise ScriptError("--image is required when --status completed")
            if uncertainties:
                raise ScriptError("--uncertainties must be [] when --status completed")
            try:
                validate_image(args.image, source_sha256=task["sha256"])
            except ImageValidationError as exc:
                raise ScriptError(f"Local candidate validation failed: {exc}") from exc
            image_path = args.image
        elif args.image is not None:
            raise ScriptError(
                "--image is only valid with --status completed; report-only results never upload an image"
            )

        fields = [
            ("task_id", task["task_id"]),
            ("source_revision", str(task["revision"])),
            ("source_sha256", task["sha256"]),
            ("instructions_hash", task["instructions_hash"]),
            ("result_status", args.status),
            ("summary", summary),
            ("changes_json", json.dumps(changes, ensure_ascii=False, separators=(",", ":"))),
            ("uncertainties_json", json.dumps(uncertainties, ensure_ascii=False, separators=(",", ":"))),
            ("provider", args.provider.strip()),
            ("model", args.model.strip()),
        ]
        body, boundary = encode_multipart(fields, image_path)
        response = post_multipart(submit_url(args.url, task), body, boundary)
    except HttpResponseError as exc:
        print(
            json.dumps({"ok": False, "status": exc.status, "response": exc.response}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1
    except ScriptError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
