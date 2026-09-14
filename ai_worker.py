#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from app.ai_revision.service import RevisionJobError, run_job
from app.config import BASE_DIR, load_config
from app.db import Database


def safe_source_root(source: dict) -> Path:
    root = Path(source["path"]).expanduser()
    return root.resolve() if root.is_absolute() else (BASE_DIR / root).resolve()


async def process_one(db: Database, config: dict) -> bool:
    job = db.claim_ai_job()
    if not job:
        return False
    source = db.source(job["source_id"])
    if not source:
        db.update_ai_job(job["id"], "failed", error_message="图片源不存在", completed=True)
        return True
    settings = config.get("ai_revision", {})
    workspace_value = settings.get("workspace_root", "data/ai-jobs")
    workspace_root = Path(workspace_value).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = (BASE_DIR / workspace_root).resolve()
    timeout = max(30, float(settings.get("task_timeout_seconds", 1200)))
    try:
        agent_command = str(settings.get("agent_command", "agent"))
        await run_job(db, job, safe_source_root(source), workspace_root, timeout, agent_command)
    except Exception as error:
        message = str(error) or type(error).__name__
        status = "stale" if "任务已过期" in message or "已变化" in message else "failed"
        db.update_ai_job(job["id"], status, error_message=message, completed=True)
    return True


async def run_forever(once: bool = False) -> None:
    config = load_config()
    settings = config.get("ai_revision", {})
    if not settings.get("enabled", False):
        # The frontend Cursor ACP entry points are intentionally retired. Exit
        # successfully so an existing LaunchAgent does not restart in a loop.
        return
    db = Database(BASE_DIR / "data" / "reviews.db")
    interval = max(1, float(settings.get("poll_interval_seconds", 3)))
    while True:
        processed = await process_one(db, config)
        if once:
            return
        if not processed:
            await asyncio.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Process image-reviewer Cursor ACP jobs")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job")
    args = parser.parse_args()
    asyncio.run(run_forever(args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
