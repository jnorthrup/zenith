from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, ClassVar, Coroutine, Literal

from .assets import AssetLoader
from .config import HarnessConfig
from .dispatcher import DispatchRequest, NodeHandoff
from .models import (
    Task,
    TerminalReviewHandoff,
    ValidateHandoff,
    WorkHandoff,
)
from .storage import (
    ProjectStore,
    atomic_write_json,
)

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str], Awaitable[None] | None]
SUBPROCESS_STREAM_LIMIT = int(
    os.environ.get(
        "ZENITH_SUBPROCESS_STREAM_LIMIT_BYTES",
        str(8 * 1024 * 1024),
    )
)

# Context window budget for worker prompts — max chars per contract assertion inline.
CONTRACT_INLINE_MAX = int(os.environ.get("ZENITH_CONTRACT_INLINE_MAX", "2000"))
CONTRACT_TRUNCATE_PREVIEW = int(os.environ.get("ZENITH_CONTRACT_TRUNCATE_PREVIEW", "500"))

_port_lock = asyncio.Lock()
_allocated_ports: set[int] = set()


# ---------------------------------------------------------------------------
# Helpers (carried over from stable_version)
# ---------------------------------------------------------------------------


def _run_cmd(command: list[str], cwd: str | None = None, *, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (result.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _normalize_progress_text(text: str) -> str:
    return " ".join(text.split())


def _extract_text_fragments(payload: Any) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            out.extend(_extract_text_fragments(item))
        return out
    if isinstance(payload, dict):
        if payload.get("type") == "text" and isinstance(payload.get("text"), str):
            return [payload["text"]]
        if "content" in payload:
            return _extract_text_fragments(payload["content"])
    return []


def _truncate_text(text: str, *, limit: int = 280) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


class ACPError(Exception):
    pass


def _augment_acp_command(
    command: str, provider, reasoning_effort: str | None = None
) -> str:
    """Append provider-specific config flags to the ACP launch command.

    For codex-acp this is the no-ask, no-sandbox combo — equivalent to
    `codex --dangerously-bypass-approvals-and-sandbox`, which codex-acp
    does not expose as a flag but accepts via `-c` overrides.

    `reasoning_effort` is the per-role override; None keeps the
    historical provider default.
    """
    name = getattr(provider, "name", None)
    if name == "codex":
        # Keep backward compatibility: default to "xhigh" for codex if unset
        effort = reasoning_effort or "xhigh"
        effort_flag = ""
        if hasattr(provider, "effort_flags"):
            effort_flag = provider.effort_flags(effort)
        else:
            effort_flag = f' -c model_reasoning_effort="{effort}"'

        return (
            command
            + ' -c sandbox_mode="danger-full-access"'
            + ' -c approval_policy="never"'
            + effort_flag
        )

    # For other providers, use their custom effort flags if available
    if hasattr(provider, "effort_flags") and reasoning_effort:
        return command + provider.effort_flags(reasoning_effort)

    return command


def _acp_subprocess_env(provider) -> dict[str, str]:
    """Build the env handed to an ACP-agent subprocess.

    For codex we preserve PATH so node-based ACP adapters can launch via
    `/usr/bin/env node`, and pass sandbox-disable hints through env. The
    command line also receives `sandbox_mode="danger-full-access"` in
    `_augment_acp_command`.

    For hermes the env is passed through unchanged.
    """
    env = os.environ.copy()
    name = getattr(provider, "name", None)
    if name == "codex":
        # Env-var hints — harmless if codex ignores them.
        env["CODEX_SANDBOX"] = "danger-full-access"
        env["CODEX_DISABLE_SANDBOX"] = "1"
    elif name == "jules":
        try:
            from .jules_acp_bridge import _token_manager
            token = _token_manager.get_token()
            if token:
                # Pass OAuth token to subprocess for Jules CLI
                env["JULES_API_KEY"] = token
        except Exception:
            pass
    # hermes: no special env needed
    return env


_NOT_FOUND = object()


async def _close_subprocess(process: asyncio.subprocess.Process, *, timeout: float = 5) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except OSError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
    transport: object = getattr(process, "_transport", None)
    if transport is not None:
        try:
            getattr(transport, "close", lambda: None)()
        except Exception:  # noqa: BLE001
            pass


async def _wait_for_process_exit(
    process: asyncio.subprocess.Process, *, timeout: float = 5
) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except OSError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def _drain_stream_chunks(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    chunks: list[str] = []
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if text:
                chunks.append(text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return "".join(chunks)
    return "".join(chunks)


@dataclass
class ManagedTerminal:
    terminal_id: str
    process: asyncio.subprocess.Process
    output: bytearray = field(default_factory=bytearray)
    output_limit: int = 1024 * 1024
    exit_waiters: list[asyncio.Future] = field(default_factory=list)
    truncated: bool = False


@dataclass
class ACPProgressTracker:
    callback: ProgressCallback | None = None
    agent_message_id: str | None = None
    agent_buffer: str = ""
    last_emitted: str = ""

    async def handle_session_update(self, params: dict[str, Any]) -> None:
        update = params.get("update")
        if not isinstance(update, dict):
            return
        if update.get("sessionUpdate") == "agent_message_chunk":
            text = "".join(_extract_text_fragments(update.get("content")))
            if not text.strip():
                return
            mid = update.get("messageId")
            if mid and mid != self.agent_message_id:
                await self._flush()
                self.agent_message_id = str(mid)
            self.agent_buffer += text
            normalized = _normalize_progress_text(self.agent_buffer)
            if (
                "\n" in text
                or self.agent_buffer.rstrip().endswith((".", "!", "?", ":", ";"))
                or len(normalized) >= 160
            ):
                await self._flush()

    async def flush(self) -> None:
        await self._flush()

    async def _flush(self) -> None:
        msg = _normalize_progress_text(self.agent_buffer)
        self.agent_buffer = ""
        if not msg or self.callback is None or msg == self.last_emitted:
            return
        self.last_emitted = msg
        out = self.callback(f"Agent: {msg}")
        if inspect.isawaitable(out):
            await out


# ---------------------------------------------------------------------------
# ACP JSON-RPC client (unchanged from stable)
# ---------------------------------------------------------------------------


class ACPClient:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        working_dir: str,
        session_update_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ):
        self._process = process
        self._working_dir = working_dir
        self._session_update_handler = session_update_handler
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._terminals: dict[str, ManagedTerminal] = {}
        self._terminal_count = 0
        self._request_tasks: set[asyncio.Task] = set()
        self._closing = False

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send_request(self, method: str, params: dict[str, Any]) -> Any:
        if self._closing:
            raise ACPError("ACP client is shutting down")
        self._request_id += 1
        rid = self._request_id
        msg = {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._write(msg)
        except Exception:
            self._pending.pop(rid, None)
            raise
        return await future

    async def _write(self, msg: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        data = json.dumps(msg).encode("utf-8") + b"\n"
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                txt = line.decode("utf-8", errors="replace").strip()
                if not txt:
                    continue
                try:
                    data = json.loads(txt)
                except json.JSONDecodeError:
                    continue
                entries = data if isinstance(data, list) else [data]
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if "result" in entry or "error" in entry:
                        self._handle_response(entry)
                    elif "method" in entry:
                        task = asyncio.create_task(self._handle_request(entry))
                        self._request_tasks.add(task)
                        task.add_done_callback(self._request_tasks.discard)
        except (BrokenPipeError, ConnectionResetError, OSError, asyncio.CancelledError):
            pass
        finally:
            for t in list(self._request_tasks):
                t.cancel()
            err = "ACP client shutting down" if self._closing else "ACP process exited"
            for rid, fut in self._pending.items():
                if not fut.done():
                    fut.set_exception(ACPError(err))
            self._pending.clear()

    def _handle_response(self, data: dict[str, Any]) -> None:
        rid = data.get("id")
        if rid is not None and rid in self._pending:
            fut = self._pending.pop(rid)
            if "error" in data:
                fut.set_exception(ACPError(str(data["error"])))
            else:
                fut.set_result(data.get("result"))

    async def _handle_request(self, data: dict[str, Any]) -> None:
        method = data["method"]
        params = data.get("params", {})
        rid = data.get("id")
        try:
            result = await self._dispatch(method, params)
        except Exception as e:  # noqa: BLE001
            if rid is not None:
                await self._write(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
                )
            return
        if rid is not None:
            if result is _NOT_FOUND:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
            else:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": result if result is not None else {},
                    }
                )

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session/update":
            if self._session_update_handler is not None:
                try:
                    out = self._session_update_handler(params)
                    if inspect.isawaitable(out):
                        await out
                except Exception:  # noqa: BLE001
                    pass
            return None
        if method == "session/request_permission":
            options = params.get("options", [])
            preferred_kinds = ["allow_session", "allow_always", "allow_once"]
            allow = None
            for kind in preferred_kinds:
                allow = next((o for o in options if o.get("kind") == kind), None)
                if allow is not None:
                    break
            if allow is None:
                allow = next((o for o in options if "allow" in str(o.get("kind", "")).lower() or "allow" in str(o.get("optionId", "")).lower()), None)
            if allow is None:
                allow = options[0] if options else {"optionId": "allow"}
            return {
                "outcome": {
                    "optionId": allow.get("optionId", "allow"),
                    "outcome": "selected",
                }
            }
        if method == "fs/read_text_file":
            return self._handle_fs_read(params)
        if method == "fs/write_text_file":
            return self._handle_fs_write(params)
        if method == "terminal/create":
            return await self._handle_terminal_create(params)
        if method == "terminal/output":
            return self._handle_terminal_output(params)
        if method == "terminal/wait_for_exit":
            return await self._handle_terminal_wait(params)
        if method in ("terminal/release", "terminal/kill"):
            return self._handle_terminal_release(params)
        return _NOT_FOUND

    def _handle_fs_read(self, params: dict[str, Any]) -> dict[str, str]:
        path = Path(params.get("path", ""))
        if not path.is_absolute():
            path = Path(self._working_dir) / path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            line = params.get("line")
            limit = params.get("limit")
            if line is not None:
                line = max(0, int(line) - 1)
                lines = text.splitlines()
                if limit is not None:
                    text = "\n".join(lines[line : line + int(limit)])
                else:
                    text = "\n".join(lines[line:])
            
            # Squeeze consecutive whitespace (like tr -s '[:space:]')
            # only for non-indentation grammars (not Python/YAML)
            suffix = path.suffix.lower()
            if suffix not in (".py", ".yaml", ".yml"):
                import re
                text = re.sub(r"([ \t\r\n\v\f])\1+", r"\1", text)
        except OSError:
            text = ""
        return {"content": text}

    def _handle_fs_write(self, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(params["path"])
        if not path.is_absolute():
            path = Path(self._working_dir) / path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = params["content"]
        path.write_text(content, encoding="utf-8")
        return {}

    async def _handle_terminal_create(self, params: dict[str, Any]) -> dict[str, str]:
        self._terminal_count += 1
        terminal_id = f"terminal-{self._terminal_count}"
        cmd = params.get("command", "")
        args = params.get("args") or []
        cwd = params.get("cwd") or self._working_dir
        env_list = params.get("env") or []
        output_limit = params.get("outputByteLimit") or 1024 * 1024
        env = os.environ.copy()
        for var in env_list:
            env[var["name"]] = var["value"]
        full_cmd = " ".join([cmd, *args])
        process = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
            limit=SUBPROCESS_STREAM_LIMIT,
        )
        terminal = ManagedTerminal(
            terminal_id=terminal_id, process=process, output_limit=output_limit
        )
        self._terminals[terminal_id] = terminal
        asyncio.create_task(self._read_terminal_output(terminal))
        return {"terminalId": terminal_id}

    async def _read_terminal_output(self, terminal: ManagedTerminal) -> None:
        assert terminal.process.stdout is not None
        try:
            while chunk := await terminal.process.stdout.read(4096):
                if terminal.truncated:
                    continue
                if len(terminal.output) + len(chunk) > terminal.output_limit:
                    terminal.truncated = True
                    remaining = terminal.output_limit - len(terminal.output)
                    terminal.output.extend(chunk[:remaining])
                    continue
                terminal.output.extend(chunk)
        except Exception:  # noqa: BLE001
            pass
        await terminal.process.wait()
        for w in terminal.exit_waiters:
            if not w.done():
                w.set_result((terminal.process.returncode, None))

    def _handle_terminal_output(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("terminalId", "")
        t = self._terminals.get(tid)
        if t is None:
            return {"output": "", "truncated": False}
        out: dict[str, Any] = {
            "output": t.output.decode("utf-8", errors="replace"),
            "truncated": t.truncated,
        }
        if t.process.returncode is not None:
            out["exitStatus"] = {"exitCode": t.process.returncode}
        return out

    async def _handle_terminal_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("terminalId", "")
        t = self._terminals.get(tid)
        if t is None:
            return {"exitCode": -1, "signal": None}
        if t.process.returncode is not None:
            return {"exitCode": t.process.returncode, "signal": None}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        t.exit_waiters.append(fut)
        code, sig = await fut
        return {"exitCode": code, "signal": sig}

    def _handle_terminal_release(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("terminalId", "")
        t = self._terminals.pop(tid, None)
        if t and t.process.returncode is None:
            try:
                t.process.terminate()
            except OSError:
                pass
        return {}

    async def cleanup(self, *, close_main_process: bool = True) -> None:
        self._closing = True
        for t in self._terminals.values():
            if t.process.returncode is None:
                try:
                    t.process.terminate()
                except OSError:
                    pass
            await _close_subprocess(t.process, timeout=5)
        self._terminals.clear()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if close_main_process:
            await _close_subprocess(self._process, timeout=5)


# ---------------------------------------------------------------------------
# ACPNodeRunner — the v5 equivalent of the stable ACPWorkerRunner
# ---------------------------------------------------------------------------


@dataclass
class ACPNodeRunner:
    """Runs ONE node end-to-end via ACP + worker MCP server subprocess."""

    supports_progress_updates: ClassVar[bool] = True
    config: HarnessConfig
    loader: AssetLoader

    @staticmethod
    async def _allocate_free_port() -> int:
        async with _port_lock:
            for _ in range(50):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                    if port not in _allocated_ports:
                        _allocated_ports.add(port)
                        return port
            import random
            return random.randint(30000, 60000)

    async def _execute_acp_subprocess(
        self,
        acp_command: str,
        workspace_dir: str,
        acp_env: dict[str, str],
        provider: ProviderDefinition,
        mcp_cfg: dict[str, Any],
        first_message: str,
        report_path: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[str | None, int | None, str, str | None]:
        process = await asyncio.create_subprocess_shell(
            acp_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=acp_env,
            limit=SUBPROCESS_STREAM_LIMIT,
        )
        progress_tracker = ACPProgressTracker(callback=progress_callback)
        client = ACPClient(
            process,
            workspace_dir,
            session_update_handler=progress_tracker.handle_session_update,
        )
        await client.start()
        
        prompt_stop_reason: str | None = None
        session_error: str | None = None
        exit_code: int | None = None
        stderr: str = ""
        stderr_task = asyncio.create_task(_drain_stream_chunks(process.stderr))

        try:
            try:
                await client.send_request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {"readTextFile": True, "writeTextFile": True},
                            "terminal": True,
                        },
                        "clientInfo": {"name": "zenith", "version": "0.1.0"},
                    },
                )
                session_params: dict[str, Any] = {
                    "cwd": workspace_dir,
                    "mcpServers": [mcp_cfg],
                }
                session_resp = await client.send_request("session/new", session_params)
                session_id = session_resp["sessionId"]
                await self._maybe_set_mode(client, session_id, provider)

                prompt_result = await client.send_request(
                    "session/prompt",
                    {"prompt": [{"type": "text", "text": first_message}], "sessionId": session_id},
                )
                if isinstance(prompt_result, dict):
                    sr = prompt_result.get("stopReason")
                    if isinstance(sr, str):
                        prompt_stop_reason = sr

                await self._poll_attempt_file(report_path, timeout=2.0)
            except Exception as exc:
                session_error = str(exc)
                logger.error("ACP session failed: %s", exc)

            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    try:
                        process.terminate()
                    except OSError:
                        pass
            await _wait_for_process_exit(process, timeout=5)
            exit_code = process.returncode
            try:
                stderr = _truncate_text(
                    await asyncio.wait_for(stderr_task, timeout=0.5), limit=2000
                )
            except (asyncio.TimeoutError, Exception):
                stderr = ""
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
            await progress_tracker.flush()
            await client.cleanup(close_main_process=False)
            await _close_subprocess(process, timeout=0)
            
        return prompt_stop_reason, exit_code, stderr, session_error

    async def run_node(
        self,
        project_id: str,
        mission_id: str,
        task: Task,
        spawn_ts: str,
        store: ProjectStore,
        *,
        cwd: str | Path | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> NodeHandoff:
        """Spawn the worker MCP server + ACP agent; poll the attempt file; return the handoff."""
        role: Literal["validator", "worker"] = (
            "validator" if task.type == "validate" else "worker"
        )
        role_config = self.config.for_role(role)
        acp_command = role_config.resolved_worker_acp_command

        # Bijective launch: when Jules is worker, launch Jules agent fire-and-forget.
        if role_config.worker_provider.name == "jules":
            return await self._run_jules_bijective(
                task=task,
                project_id=project_id,
                mission_id=mission_id,
                spawn_ts=spawn_ts,
                store=store,
                cwd=cwd,
                progress_callback=progress_callback,
            )

        if not acp_command:
            raise RuntimeError(
                f"No ACP command for role={role}. Set ZENITH_{role.upper()}_ACP_COMMAND."
            )
        acp_command = _augment_acp_command(acp_command, role_config.worker_provider)

        workspace_dir, project_bucket = self._resolve_node_paths(
            store=store, project_id=project_id, cwd=cwd
        )
        handoff_path = store.attempt_path(project_id, mission_id, spawn_ts, task.id)
        handoff_path.parent.mkdir(parents=True, exist_ok=True)

        _ensure_claude_settings(Path(workspace_dir), role_config.worker_provider)

        mcp_port = await self._allocate_free_port()
        mcp_process = None
        try:
            mcp_process = await self._start_worker_mcp_server(
                task=task,
                project_id=project_id,
                mission_id=mission_id,
                handoff_path=str(handoff_path),
                workspace_dir=workspace_dir,
                mcp_port=mcp_port,
            )
            try:
                await self._wait_for_server_ready("127.0.0.1", mcp_port)
            except TimeoutError:
                if mcp_process.returncode is None:
                    mcp_process.terminate()
                await _close_subprocess(mcp_process, timeout=5)
                return self._synthesize_missing_handoff(
                    task, summary="Worker MCP server failed to start"
                )

            worker_mcp_cfg = {
                "type": "http",
                "name": "zenith-worker",
                "url": f"http://127.0.0.1:{mcp_port}/mcp",
                "headers": [],
                "env": [],
            }

            first_message = self._render_prompts(
                task=task,
                mission_id=mission_id,
                project_bucket=project_bucket,
                workspace_dir=workspace_dir,
                store=store,
                project_id=project_id,
                provider_name=role_config.worker_provider.name,
            )

            acp_env = _acp_subprocess_env(role_config.worker_provider)
            acp_env["ZENITH_HANDOFF_PATH"] = str(handoff_path)
            acp_env["ZENITH_NODE_ID"] = task.id
            acp_env["ZENITH_NODE_TYPE"] = task.type
            acp_env["ZENITH_PROJECT_ID"] = project_id
            acp_env["ZENITH_MISSION_ID"] = mission_id
            acp_env["ZENITH_HOME"] = str(self.config.harness_home)

            prompt_stop_reason, worker_exit_code, worker_stderr, session_error = await self._execute_acp_subprocess(
                acp_command=acp_command,
                workspace_dir=workspace_dir,
                acp_env=acp_env,
                provider=role_config.worker_provider,
                mcp_cfg=worker_mcp_cfg,
                first_message=first_message,
                report_path=handoff_path,
                progress_callback=progress_callback,
            )
        finally:
            async with _port_lock:
                _allocated_ports.discard(mcp_port)
            if mcp_process is not None:
                if mcp_process.returncode is None:
                    try:
                        mcp_process.terminate()
                    except OSError:
                        pass
                await _close_subprocess(mcp_process, timeout=5)

        if handoff_path.exists():
            return self._parse_handoff_file(handoff_path, task)
        return self._synthesize_and_persist_missing_handoff(
            handoff_path=handoff_path,
            task=task,
            stop_reason=prompt_stop_reason,
            exit_code=worker_exit_code,
            stderr=worker_stderr,
            session_error=session_error,
        )

    def _parse_handoff_file(self, handoff_path: Path, task: Task) -> NodeHandoff:
        """Parse a handoff file written by the worker MCP server."""
        import json
        raw = handoff_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and ("items" in data or "passed" in data):
            return ValidateHandoff.model_validate(data)
        return WorkHandoff.model_validate(data)

    def _synthesize_missing_handoff(self, task: Task, summary: str) -> NodeHandoff:
        """Synthesize a failure handoff when worker crashes or times out."""
        if task.type == "validate":
            return ValidateHandoff(
                node_id=task.id,
                done=False,
                report=summary,
                items=[],
                passed=False,
                request_attention=False,
            )
        return WorkHandoff(
            node_id=task.id,
            done=False,
            report=summary,
            request_attention=False,
        )

    def _synthesize_and_persist_missing_handoff(
        self,
        handoff_path: Path,
        task: Task,
        *,
        stop_reason: str | None,
        exit_code: int | None,
        stderr: str,
        session_error: str | None,
    ) -> NodeHandoff:
        """Synthesize a handoff when the worker exited without writing one."""
        parts = ["Worker exited without producing a handoff."]
        if stop_reason:
            parts.append(f"Stop reason: {stop_reason}")
        if exit_code is not None:
            parts.append(f"Exit code: {exit_code}")
        if session_error:
            parts.append(f"Session error: {session_error}")
        if stderr:
            parts.append(f"Stderr: {stderr[:500]}")
        summary = "\n".join(parts)

        handoff = self._synthesize_missing_handoff(task, summary)

        try:
            import json
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            handoff_path.write_text(
                json.dumps(handoff.model_dump(mode="json"), indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

        return handoff

    async def run_terminal_review(
        self,
        project_id: str,
        mission_id: str,
        spawn_ts: str,
        store: ProjectStore,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> TerminalReviewHandoff:
        """Adversarial fresh-context terminal review."""
        role_config = self.config.for_role("terminal_reviewer")
        acp_command = role_config.resolved_worker_acp_command
        if not acp_command:
            raise RuntimeError(
                "No ACP command for terminal reviewer. "
                "Set ZENITH_TERMINAL_REVIEWER_ACP_COMMAND."
            )
        acp_command = _augment_acp_command(acp_command, role_config.worker_provider)

        workspace_dir = str(store.workspace_dir(project_id))
        project_bucket = str(store.zenith_dir(project_id))
        report_path = store.terminal_review_path(project_id, mission_id, spawn_ts)
        report_path.parent.mkdir(parents=True, exist_ok=True)

        _ensure_claude_settings(Path(workspace_dir), role_config.worker_provider)

        mcp_port = await self._allocate_free_port()
        mcp_process = None
        try:
            mcp_process = await self._start_terminal_reviewer_mcp(
                project_id=project_id,
                mission_id=mission_id,
                report_path=str(report_path),
                workspace_dir=workspace_dir,
                mcp_port=mcp_port,
            )
            try:
                await self._wait_for_server_ready("127.0.0.1", mcp_port)
            except TimeoutError:
                if mcp_process.returncode is None:
                    mcp_process.terminate()
                await _close_subprocess(mcp_process, timeout=5)
                raise RuntimeError(
                    "Terminal reviewer MCP server failed to start; cannot run terminal review"
                )

            worker_mcp_cfg = {
                "type": "http",
                "name": "zenith-terminal-reviewer",
                "url": f"http://127.0.0.1:{mcp_port}/mcp",
                "headers": [],
                "env": [],
            }

            first_message = self._render_terminal_reviewer_prompts(
                project_bucket=project_bucket,
                workspace_dir=workspace_dir,
                provider_name=role_config.worker_provider.name,
            )

            prompt_stop_reason, exit_code, stderr, session_error = await self._execute_acp_subprocess(
                acp_command=acp_command,
                workspace_dir=workspace_dir,
                acp_env=_acp_subprocess_env(role_config.worker_provider),
                provider=role_config.worker_provider,
                mcp_cfg=worker_mcp_cfg,
                first_message=first_message,
                report_path=report_path,
                progress_callback=progress_callback,
            )
        finally:
            async with _port_lock:
                _allocated_ports.discard(mcp_port)
            if mcp_process is not None:
                if mcp_process.returncode is None:
                    try:
                        mcp_process.terminate()
                    except OSError:
                        pass
                await _close_subprocess(mcp_process, timeout=5)

        if report_path.exists():
            return TerminalReviewHandoff.model_validate_json(report_path.read_text())
        raise RuntimeError(
            "Terminal reviewer exited without calling submit_terminal_review; "
            "no terminal review was written. The mission cannot be sealed as done "
            "without an explicit reviewer verdict."
        )

    # ------------------------------------------------------------------
    # Subprocess plumbing
    # ------------------------------------------------------------------

    async def _start_worker_mcp_server(
        self,
        *,
        task: Task,
        project_id: str,
        mission_id: str,
        handoff_path: str,
        workspace_dir: str,
        mcp_port: int,
    ) -> asyncio.subprocess.Process:
        cmd = [
            sys.executable,
            "-m",
            "zenith_harness",
            "--mode",
            "worker",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(mcp_port),
        ]
        env = os.environ.copy()
        env["ZENITH_HOME"] = str(self.config.harness_home)
        env["ZENITH_PROJECT_ID"] = project_id
        env["ZENITH_MISSION_ID"] = mission_id
        env["ZENITH_NODE_ID"] = task.id
        env["ZENITH_NODE_TYPE"] = task.type
        env["ZENITH_HANDOFF_PATH"] = handoff_path
        # Ensure subprocess uses the local source tree (editable install fallback)
        src_root = str(Path(__file__).parent.parent.parent.parent)
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = src_root + ":" + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = src_root
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=env,
            limit=SUBPROCESS_STREAM_LIMIT,
        )

    async def _start_terminal_reviewer_mcp(
        self,
        *,
        project_id: str,
        mission_id: str,
        report_path: str,
        workspace_dir: str,
        mcp_port: int,
    ) -> asyncio.subprocess.Process:
        cmd = [
            sys.executable,
            "-m",
            "zenith_harness",
            "--mode",
            "terminal-reviewer",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(mcp_port),
        ]
        env = os.environ.copy()
        env["ZENITH_PROJECT_ID"] = project_id
        env["ZENITH_MISSION_ID"] = mission_id
        env["ZENITH_TERMINAL_REVIEW_PATH"] = report_path
        # Ensure subprocess uses the local source tree (editable install fallback)
        src_root = str(Path(__file__).parent.parent.parent.parent)
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = src_root + ":" + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = src_root
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=env,
            limit=SUBPROCESS_STREAM_LIMIT,
        )

    async def _wait_for_server_ready(self, host: str, port: int, *, timeout: float = 15) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                _, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                return
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.3)
        raise TimeoutError(f"MCP server on {host}:{port} not ready within {timeout}s")

    async def _poll_attempt_file(self, path: Path, *, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if path.exists():
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _maybe_set_mode(
        self, client: ACPClient, session_id: str, provider
    ) -> None:
        mode = getattr(provider, "acp_runtime_mode", None)
        if not mode:
            return
        try:
            await client.send_request(
                "session/set_mode", {"sessionId": session_id, "modeId": mode}
            )
        except ACPError as exc:
            raise ACPError(
                f"Failed to set ACP runtime mode {mode!r} for {provider.name}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Bijective Jules Launch
    # ------------------------------------------------------------------

    async def _run_jules_bijective(
        self,
        task: Task,
        project_id: str,
        mission_id: str,
        spawn_ts: str,
        store: ProjectStore,
        cwd: str | Path | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> NodeHandoff:
        """Fire-and-forget Jules launch — task holds a mailbox, not a verdict.

        Jules takes hours. We return ``done=False`` with a single outbound
        ``MailEnvelope`` carrying the rendered first prompt. The
        coordinator treats ``done=False`` + non-empty ``pending_mail`` as
        "waiting on the slow agent's mailbox," not as failure (see
        ``coordinator._apply_handoff_collect``). The orchestrator's
        ``jules_bijective_sync`` MCP tool probes the remote session each
        main-loop turn and appends inbound mail; the dispatched
        ``run_node`` for the next turn will pick up the result.
        """
        from .models import MailEnvelope, WorkHandoff
        from .jules_acp_bridge import (
            BridgeError,
            ensure_jules_authenticated,
            launch_jules_bijective,
        )

        workspace_dir, project_bucket = self._resolve_node_paths(
            store=store, project_id=project_id, cwd=cwd
        )

        first_message = self._render_prompts(
            task=task,
            mission_id=mission_id,
            project_bucket=project_bucket,
            workspace_dir=workspace_dir,
            store=store,
            project_id=project_id,
            provider_name="jules",
        )

        if not ensure_jules_authenticated():
            raise BridgeError("Jules authentication failed")

        # launch_jules_bijective returns remote_id immediately (CLI subprocess).
        # The Jules PR/branch arrives hours later; orchestrator uses
        # jules_bijective_sync / jules_list_sessions to discover it.
        jules_remote_id, jules_state = await launch_jules_bijective(
            prompt_text=first_message,
            cwd=workspace_dir,
            task_id=task.id,
            project_id=project_id,
            mission_id=mission_id,
        )

        # Strictly Unix time for all mail timestamps (user preference).
        outbound = MailEnvelope(
            from_party=jules_remote_id,
            to_party="orchestrator",
            kind="open",
            body=first_message,
            unix_ts=time.time(),
            pending=True,
        )
        report = (
            f"Jules session {jules_remote_id} launched (status={jules_state.status}). "
            f"Mailbox slug={mission_id} — contract header via markdown, "
            f"mail held by main-loop turn batcher. Circle back via jules_bijective_sync."
        )
        return WorkHandoff(
            node_id=task.id,
            done=False,
            report=report,
            request_attention=False,
            pending_mail=[outbound],
        )

    # ------------------------------------------------------------------
    # Path resolution (DRY: shared by run_node and _run_jules_bijective)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_node_paths(
        *, store: ProjectStore, project_id: str, cwd: str | Path | None
    ) -> tuple[str, str]:
        """Resolve ``(workspace_dir, project_bucket)`` for a node dispatch.

        ``workspace_dir`` honours the caller's ``cwd`` if provided, falling
        back to the project's workspace. ``project_bucket`` is the
        ``.zenith`` directory under the project, used by the prompt
        renderer to template absolute paths.
        """
        workspace_dir = str(
            Path(cwd).expanduser().resolve()
            if cwd
            else store.workspace_dir(project_id)
        )
        project_bucket = str(store.zenith_dir(project_id))
        return workspace_dir, project_bucket

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _render_prompts(
        self,
        *,
        task: Task,
        mission_id: str,
        project_bucket: str,
        workspace_dir: str,
        store: ProjectStore,
        project_id: str,
        provider_name: str,
    ) -> str:
        session_type = "validator" if task.type == "validate" else "worker"
        system_prompt = self.loader.load_prompt_file(session_type, "system_prompt.md")
        if provider_name == "hermes":
            system_prompt += "\n\nCRITICAL DIRECTIVE: You are a Hermes agent. Use the 'recall' and memory queries as your primary context retrieval mechanism, as you tend to provide eidetic responses."
        template_vars = self._build_template_vars(
            task=task,
            mission_id=mission_id,
            project_bucket=project_bucket,
            workspace_dir=workspace_dir,
            store=store,
            project_id=project_id,
        )
        template = self.loader.render_prompt_template(session_type, "template.md", template_vars)
        if system_prompt:
            return system_prompt + "\n\n---\n\n" + template
        return template

    def _build_template_vars(
        self,
        *,
        task: Task,
        mission_id: str,
        project_bucket: str,
        workspace_dir: str,
        store: ProjectStore,
        project_id: str,
    ) -> dict[str, Any]:
        """Build template variables for worker/validator prompt templates."""
        attempts_dir = str(store.attempts_dir(project_id, mission_id))
        agents_path = str(Path(project_bucket) / "agents")
        memory_path = str(Path(project_bucket) / "MEMORY.md")
        skill_name = task.skill or "none"
        assignment_body = task.body

        # Load contract assertions from markdown files
        contract_assertions = []
        contract_target_paths = []
        for target in task.targets:
            contract_path = store.contract_assertion_path(project_id, mission_id, target)
            if contract_path.exists():
                contract_content = contract_path.read_text(encoding="utf-8")
                contract_assertions.append(f"## Contract: {target}\n\n{contract_content}")
                contract_target_paths.append(f"- `{target}`")

        contract_target_paths_str = "\n".join(contract_target_paths)
        contract_assertions_str = "\n\n---\n\n".join(contract_assertions)

        return {
            "assignment_body": assignment_body,
            "skill_name": skill_name,
            "agents_path": agents_path,
            "memory_path": memory_path,
            "contract_target_paths": contract_target_paths_str,
            "contract_assertions": contract_assertions_str,
            "attempts_dir": attempts_dir,
        }

    def _render_terminal_reviewer_prompts(
        self,
        *,
        project_bucket: str,
        workspace_dir: str,
        provider_name: str,
    ) -> str:
        brief_path = Path(project_bucket) / "brief.md"
        brief_text = brief_path.read_text() if brief_path.exists() else ""
        system_prompt = self.loader.render_prompt_template(
            "terminal-reviewer",
            "system_prompt.md",
            {
                "user_request": brief_text,
                "workspace": workspace_dir,
            },
        )
        if provider_name == "hermes":
            system_prompt += "\n\nCRITICAL DIRECTIVE: You are a Hermes agent. Use the 'recall' and memory queries as your primary context retrieval mechanism, as you tend to provide eidetic responses."
        return system_prompt

    # ---------------------------------------------------------------------------
# Contract assertion truncation helpers
# ---------------------------------------------------------------------------


def _truncate_contract_assertion(body: str, path: str) -> str:
    """Return inline-ready contract body with truncation marker if over limit."""
    if len(body) <= CONTRACT_INLINE_MAX:
        return body.rstrip() + "\n"
    preview = body[:CONTRACT_TRUNCATE_PREVIEW].rstrip()
    return (
        f"{preview}\n\n"
        f"--- TRUNCATED ({len(body)} chars, read full at `{path}`) ---\n"
    )


def _format_attempts_dir_hint(attempts_dir: str) -> str:
    """Render a context-aware attempts directory hint for the worker prompt."""
    return (
        f"{attempts_dir}\n"
        f"(Read only the most recent 1-2 attempt reports relevant to your targets; "
        f"do not load the entire history.)"
    )


# ---------------------------------------------------------------------------
# ACP Node Runner
# ---------------------------------------------------------------------------



def _run_coro_blocking(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine to completion from sync code.

    Uses `asyncio.run` when no event loop is active. When the caller is
    already inside a running loop (e.g. an MCP tool handler that forgot
    to wrap with `asyncio.to_thread`), execute the coroutine in a fresh
    loop on a worker thread instead of crashing.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Inside a running loop: hop to a worker thread with its own loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class ACPNodeDispatcher:
    """Implements `NodeDispatcher` by calling `ACPNodeRunner.run_node`."""

    def __init__(self, config: HarnessConfig, store: ProjectStore | None = None):
        self.config = config
        self.store = store or ProjectStore(config)
        self.loader = AssetLoader(config)
        self.runner = ACPNodeRunner(config=config, loader=self.loader)

    def dispatch(self, request: DispatchRequest) -> NodeHandoff:
        return _run_coro_blocking(
            self.runner.run_node(
                project_id=request.project_id,
                mission_id=request.mission_id,
                task=request.task,
                spawn_ts=request.spawn_ts,
                store=self.store,
                cwd=request.cwd,
            )
        )

    def dispatch_batch(self, requests: list[DispatchRequest]) -> list[NodeHandoff]:
        async def _run_all() -> list[NodeHandoff]:
            tasks = [
                self.runner.run_node(
                    project_id=r.project_id,
                    mission_id=r.mission_id,
                    task=r.task,
                    spawn_ts=r.spawn_ts,
                    store=self.store,
                    cwd=r.cwd,
                )
                for r in requests
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            handoffs: list[NodeHandoff] = []
            for request, result in zip(requests, results, strict=True):
                if isinstance(result, BaseException):
                    handoffs.append(
                        self.runner._synthesize_missing_handoff(
                            request.task,
                            summary=f"Dispatcher crashed: {result}",
                        )
                    )
                else:
                    handoffs.append(result)
            return handoffs

        return _run_coro_blocking(_run_all())


class ACPTerminalReviewer:
    """Implements `TerminalReviewer` by calling `ACPNodeRunner.run_terminal_review`."""

    def __init__(self, config: HarnessConfig, store: ProjectStore | None = None):
        self.config = config
        self.store = store or ProjectStore(config)
        self.loader = AssetLoader(config)
        self.runner = ACPNodeRunner(config=config, loader=self.loader)

    def review(
        self, project_id: str, mission_id: str, spawn_ts: str
    ) -> TerminalReviewHandoff:
        return _run_coro_blocking(
            self.runner.run_terminal_review(
                project_id=project_id,
                mission_id=mission_id,
                spawn_ts=spawn_ts,
                store=self.store,
            )
        )


# ---------------------------------------------------------------------------
# claude-agent-acp settings workaround
# ---------------------------------------------------------------------------


def _ensure_claude_settings(workspace: Path, provider) -> None:
    """Write `<workspace>/.claude/settings.json` overriding `permissions.defaultMode`.

    The `@zed-industries/claude-agent-acp` adapter loads settings as
    user → project → local → enterprise (last write wins). Without this, a
    user with `"permissions": {"defaultMode": "auto"}` in their global
    `~/.claude/settings.json` will see session/new fail with
    `Invalid permissions.defaultMode: auto.` — the Claude Code SDK does not
    accept "auto".

    We touch this file only when:
    - The provider declares a non-empty `acp_runtime_mode` (i.e. claude).
    - The file does not already exist (respect any user-authored override).

    In v5 the workspace is the user's repo, so we conservatively no-op on
    pre-existing files.
    """
    mode = getattr(provider, "acp_runtime_mode", None)
    if not mode:
        return
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        # Respect user-authored settings; the user can put whatever they want
        # in there. If their setting is "auto" we can't help — they need to
        # change it manually.
        return
    settings_path.write_text(
        json.dumps({"permissions": {"defaultMode": mode}}, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "ACPClient",
    "ACPError",
    "ACPNodeRunner",
    "ACPNodeDispatcher",
    "ACPTerminalReviewer",
    "ACPProgressTracker",
    "SUBPROCESS_STREAM_LIMIT",
]
