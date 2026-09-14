#!/usr/bin/env python3
"""Probe Cursor ACP image editing in an isolated workspace.

The probe copies its inputs into data/ai-probe and never writes to the source
image directory. It speaks newline-delimited JSON-RPC to `agent acp`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "ai-probe"


class ACPError(RuntimeError):
    pass


class CursorACPClient:
    def __init__(self, cwd: Path, log_path: Path, timeout: float = 1200) -> None:
        self.cwd = cwd
        self.log_path = log_path
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.session_updates: list[dict[str, Any]] = []
        self.image_events: list[dict[str, Any]] = []
        self.initialized: dict[str, Any] = {}
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._log_handle = None

    async def __aenter__(self) -> "CursorACPClient":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            "agent", "acp", cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.process and self.process.stdin:
            self.process.stdin.close()
        if self.process and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        if self._log_handle:
            self._log_handle.close()

    def _log(self, direction: str, payload: Any) -> None:
        if not self._log_handle:
            return
        value = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self._log_handle.write(f"[{direction}] {value}\n")
        self._log_handle.flush()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self._log("stderr", line.decode("utf-8", errors="replace").rstrip())

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                self._log("stdout-non-json", text)
                continue
            self._log("recv", message)
            message_id = message.get("id")
            if message_id is not None and ("result" in message or "error" in message):
                future = self.pending.pop(message_id, None)
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(ACPError(json.dumps(message["error"], ensure_ascii=False)))
                    else:
                        future.set_result(message.get("result"))
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "session/update":
                self.session_updates.append(params)
            elif method == "cursor/generate_image":
                self.image_events.append(params)
            elif method == "session/request_permission" and message_id is not None:
                await self.respond(message_id, self._permission_response(params))
            elif method == "cursor/ask_question" and message_id is not None:
                await self.respond(message_id, {"outcome": {"outcome": "skipped", "reason": "Unattended image revision probe"}})
            elif method == "cursor/create_plan" and message_id is not None:
                await self.respond(message_id, {"outcome": {"outcome": "accepted"}})

        error = ACPError("Cursor ACP process exited before all requests completed")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    def _permission_response(self, params: dict[str, Any]) -> dict[str, Any]:
        options = params.get("options") or []
        allowed = next(
            (option for option in options if option.get("optionId") in {"allow-once", "allow-always"}),
            None,
        )
        option_id = allowed.get("optionId") if allowed else "allow-once"
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    async def _write(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self._log("send", message)
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def respond(self, message_id: int, result: Any) -> None:
        await self._write({"jsonrpc": "2.0", "id": message_id, "result": result})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        message_id = self.next_id
        self.next_id += 1
        future = loop.create_future()
        self.pending[message_id] = future
        await self._write({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as error:
            self.pending.pop(message_id, None)
            raise ACPError(f"ACP request timed out: {method}") from error

    async def initialize(self) -> dict[str, Any]:
        result = await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "image-reviewer-acp-probe", "version": "0.1.0"},
        })
        self.initialized = result or {}
        return self.initialized



def prepare_workspace(target: Path, reference: Path, output_root: Path) -> tuple[Path, Path, Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    workspace = output_root / stamp
    input_dir = workspace / "input"
    output_dir = workspace / "output"
    log_dir = workspace / "logs"
    input_dir.mkdir(parents=True)
    output_dir.mkdir()
    log_dir.mkdir()
    target_copy = input_dir / "target.png"
    reference_copy = input_dir / f"product-reference{reference.suffix.lower()}"
    shutil.copy2(target, target_copy)
    shutil.copy2(reference, reference_copy)
    return workspace, target_copy, reference_copy


def build_prompt(target: Path, reference: Path, candidate: Path, report: Path, instruction: str) -> str:
    return f"""你是 Amazon A+ 图片修改代理。请只处理当前任务，不要修改 input 文件或工作目录以外的任何文件。

待修改图片：{target}
产品主图：{reference}
人工评审意见：{instruction}

要求：
1. 查看待修改图和产品主图，产品主图是产品外形、颜色、材质、结构、数量和配件的事实依据。
2. 自主分析评审意见对应的问题和最小必要修改方法，保留未涉及且合格的内容。
3. 使用 Cursor 自带图片生成/编辑能力生成候选图。
4. 候选图必须是 PNG，严格为 970×600，写入：{candidate}
5. 不得添加价格、折扣、评分、Amazon/Prime、联系方式或未经支持的功能宣称。
6. 完成后将 JSON 报告写入：{report}
   格式：{{"status":"completed","summary":"...","resolved_instructions":["..."],"changes":["..."],"uncertainties":[],"output_file":"{candidate}"}}
7. 如果无法可靠修改，不要猜测或伪造候选图；报告 status 使用 needs_human_input 并说明 uncertainties。
8. 不要停下来询问用户，直接完成任务或记录信息不足。
"""


def validate_candidate(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ACPError(f"Cursor did not create candidate image: {path}")
    try:
        with Image.open(path) as image:
            image.load()
            image_format = image.format
            size = image.size
    except (OSError, ValueError) as error:
        raise ACPError(f"Candidate image cannot be decoded: {error}") from error
    if image_format != "PNG":
        raise ACPError(f"Candidate must be PNG, got {image_format}")
    if size != (970, 600):
        raise ACPError(f"Candidate must be 970x600, got {size[0]}x{size[1]}")
    return {"format": image_format, "width": size[0], "height": size[1], "bytes": path.stat().st_size}


async def run(args: argparse.Namespace) -> int:
    target = args.target.expanduser().resolve()
    reference = args.reference.expanduser().resolve()
    if not target.is_file():
        raise ACPError(f"Target image does not exist: {target}")
    if not reference.is_file():
        raise ACPError(f"Reference image does not exist: {reference}")
    workspace, target_copy, reference_copy = prepare_workspace(target, reference, args.output_root.expanduser().resolve())
    candidate = workspace / "output" / "candidate.png"
    report = workspace / "output" / "result.json"
    metadata = workspace / "probe-result.json"
    payload: dict[str, Any] = {"workspace": str(workspace), "source_target": str(target), "source_reference": str(reference)}

    try:
        async with CursorACPClient(workspace, workspace / "logs" / "cursor-agent.log", args.timeout) as client:
            payload["initialize"] = await client.initialize()
            if args.initialize_only:
                payload["status"] = "initialized"
            else:
                await client.request("authenticate", {"methodId": "cursor_login"})
                session = await client.request("session/new", {"cwd": str(workspace), "mcpServers": []})
                session_id = session["sessionId"]
                payload["session"] = session
                prompt_result = await client.request("session/prompt", {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": build_prompt(target_copy, reference_copy, candidate, report, args.instruction)}],
                })
                payload["prompt_result"] = prompt_result
                payload["image_events"] = client.image_events
                payload["candidate"] = validate_candidate(candidate)
                if report.is_file():
                    payload["agent_report"] = json.loads(report.read_text(encoding="utf-8"))
                payload["status"] = "completed"
    except Exception as error:
        payload["status"] = "failed"
        payload["error"] = str(error)
        metadata.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    metadata.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Cursor ACP image editing without modifying source images")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--instruction", default="文字显示不全")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--initialize-only", action="store_true", help="Only initialize ACP and print server capabilities")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
