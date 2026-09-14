from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class CursorACPError(RuntimeError):
    pass


class CursorACPClient:
    """Small newline-delimited JSON-RPC client for `agent acp`."""

    def __init__(self, cwd: Path, log_path: Path, timeout: float = 1200, agent_command: str = "agent") -> None:
        self.cwd = cwd
        self.agent_command = agent_command
        self.log_path = log_path
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.image_events: list[dict[str, Any]] = []
        self._tasks: list[asyncio.Task] = []
        self._log_handle = None

    async def __aenter__(self) -> "CursorACPClient":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            self.agent_command, "acp", cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.process and self.process.stdin:
            self.process.stdin.close()
        if self.process and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._log_handle:
            self._log_handle.close()

    def _log(self, direction: str, value: Any) -> None:
        if self._log_handle:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            self._log_handle.write(f"[{direction}] {text}\n")
            self._log_handle.flush()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self._log("stderr", line.decode(errors="replace").rstrip())

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            text = line.decode(errors="replace").strip()
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
                        future.set_exception(CursorACPError(json.dumps(message["error"], ensure_ascii=False)))
                    else:
                        future.set_result(message.get("result"))
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "cursor/generate_image":
                self.image_events.append(params)
            elif method == "session/request_permission" and message_id is not None:
                await self.respond(message_id, self._permission_response(params))
            elif method == "cursor/ask_question" and message_id is not None:
                await self.respond(message_id, {"outcome": {"outcome": "skipped", "reason": "Unattended revision task"}})
            elif method == "cursor/create_plan" and message_id is not None:
                await self.respond(message_id, {"outcome": {"outcome": "accepted"}})

        error = CursorACPError("Cursor ACP exited before completing pending requests")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    @staticmethod
    def _permission_response(params: dict[str, Any]) -> dict[str, Any]:
        options = params.get("options") or []
        option = next((item for item in options if item.get("optionId") == "allow-once"), None)
        if not option:
            option = next((item for item in options if item.get("optionId") == "allow-always"), None)
        return {"outcome": {"outcome": "selected", "optionId": option.get("optionId") if option else "allow-once"}}

    async def _write(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self._log("send", message)
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def respond(self, message_id: int, result: Any) -> None:
        await self._write({"jsonrpc": "2.0", "id": message_id, "result": result})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        message_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self._write({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, self.timeout)
        except asyncio.TimeoutError as error:
            self.pending.pop(message_id, None)
            raise CursorACPError(f"ACP request timed out: {method}") from error

    async def start_session(self) -> dict[str, Any]:
        initialize = await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "image-reviewer-worker", "version": "0.1.0"},
        })
        await self.request("authenticate", {"methodId": "cursor_login"})
        session = await self.request("session/new", {"cwd": str(self.cwd), "mcpServers": []})
        return {"initialize": initialize, "session": session}

    async def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        return await self.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        })
