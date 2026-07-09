#!/usr/bin/env python3
"""Jules ACP worker bridge — runs as a subprocess via ACP protocol.

This is the blocking, poll-based Jules worker. It is spawned by the zenith
harness as an ACP agent (`jules-acp-bridge` entry point). It does NOT run
in the orchestrator; it runs in its own subprocess and blocks until Jules
reaches terminal state.

Orchestrator (MCP tools) uses non-blocking helpers in jules_acp_bridge.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
import urllib.parse
from urllib import request as urllib_request
from urllib import error as urllib_error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jules_acp_bridge import (
    atomic_write_json,
    check_jules_status,
    combine_output,
    extract_pr_url,
    extract_remote_id,
    get_current_repo_name,
    looks_like_unknown_subcommand,
    parse_remote_state,
    rest_json,
    run_command,
    send_jules_message,
    try_extract_pr_url_from_pull,
    try_find_pushed_branch,
    BridgeError,
    JulesRemoteState,
    JULES_API_BASE,
    JULES_BIN,
    MAX_TRANSIENT_RETRIES,
    PR_URL_RE,
    STATUS_POLL_SECONDS,
    TRANSIENT_BACKOFF_S,
)

JSON = dict[str, Any]


@dataclass
class BridgeSession:
    session_id: str
    cwd: str
    mcp_servers: list[JSON] = field(default_factory=list)
    created_at: float = field(default_factory=lambda: time.time())


class JsonRpcTransport:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def respond(self, request_id: Any, *, result: JSON | None = None, err: str | None = None) -> None:
        payload: JSON = {"jsonrpc": "2.0", "id": request_id}
        if err is not None:
            payload["error"] = {"code": -32000, "message": err}
        else:
            payload["result"] = result or {}
        await self._write(payload)

    async def notify_session_update(self, session_id: str, text: str, *, message_id: str = "jules-progress") -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": message_id,
                    "content": [{"type": "text", "text": text}],
                },
            },
        }
        await self._write(payload)

    async def _write(self, payload: JSON) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._lock:
            sys.stdout.write(line)
            sys.stdout.flush()


async def _poll_jules_cli(remote_id: str, cwd: str) -> JulesRemoteState:
    """Poll Jules CLI until terminal state. Only used by ACP worker subprocess."""
    last_raw = ""
    fallback_to_list = False
    while True:
        args = [JULES_BIN, "remote", "status", remote_id]
        code, stdout, stderr = await run_command(args, cwd=cwd, timeout=120)
        combined = combine_output(stdout, stderr)
        if code != 0 and looks_like_unknown_subcommand(combined):
            fallback_to_list = True
        if fallback_to_list:
            code, stdout, stderr = await run_command(
                [JULES_BIN, "remote", "list", "--session"],
                cwd=cwd,
                timeout=120,
            )
            combined = combine_output(stdout, stderr)
            if code != 0:
                raise BridgeError(best_error(combined, f"jules remote list --session exited {code}"))
        elif code != 0:
            if not looks_like_unknown_subcommand(combined):
                transient_failures = getattr(_poll_jules_cli, "_transient_count", {})
                key = remote_id
                transient_failures[key] = transient_failures.get(key, 0) + 1
                _poll_jules_cli._transient_count = transient_failures
                if transient_failures[key] <= MAX_TRANSIENT_RETRIES:
                    await asyncio.sleep(TRANSIENT_BACKOFF_S)
                    continue
            raise BridgeError(best_error(combined, f"jules remote status exited {code}"))

        last_raw = combined or last_raw
        state = parse_remote_state(remote_id, combined)
        if state.is_terminal:
            if state.succeeded and not state.pr_url:
                pulled_url = await try_extract_pr_url_from_pull(remote_id, cwd)
                if pulled_url:
                    return JulesRemoteState(
                        remote_id=remote_id,
                        status=state.status,
                        raw=state.raw,
                        pr_url=pulled_url,
                    )
            return state
        await asyncio.sleep(STATUS_POLL_SECONDS)


async def _run_jules_rest(prompt_text: str, cwd: str) -> JulesRemoteState:
    repo_arg = get_current_repo_name(cwd) or str(find_git_root(cwd))
    create_payload = {"prompt": prompt_text, "repo": repo_arg}
    create = await rest_json("POST", f"{JULES_API_BASE.rstrip('/')}/tasks", create_payload)
    remote_id = extract_remote_id(create)
    if not remote_id:
        raise BridgeError(f"unable to parse Jules task id from REST response: {json.dumps(create, ensure_ascii=False)}")
    while True:
        data = await rest_json("GET", f"{JULES_API_BASE.rstrip('/')}/tasks/{remote_id}")
        state = parse_remote_state(remote_id, data)
        if state.is_terminal:
            if state.succeeded and not state.pr_url:
                pulled_url = await try_extract_pr_url_from_pull(remote_id, cwd)
                if pulled_url:
                    return JulesRemoteState(
                        remote_id=remote_id,
                        status=state.status,
                        raw=state.raw,
                        pr_url=pulled_url,
                    )
            return state
        await asyncio.sleep(STATUS_POLL_SECONDS)


async def _run_jules_cli(prompt_text: str, cwd: str) -> JulesRemoteState:
    create_args = [JULES_BIN, "remote", "new"]
    repo_arg = get_current_repo_name(cwd)
    if repo_arg:
        create_args += ["--repo", repo_arg]
    create_args += ["--session", prompt_text]
    code, stdout, stderr = await run_command(create_args, cwd=cwd)
    combined = combine_output(stdout, stderr)
    if code != 0:
        raise BridgeError(best_error(combined, f"jules remote new exited {code}"))
    remote_id = extract_remote_id(stdout) or extract_remote_id(stderr)
    if not remote_id:
        raise BridgeError(f"unable to parse Jules session id from output: {combined or '(empty output)'}")
    return await _poll_jules_cli(remote_id, cwd)


def _extract_prompt_text(prompt_payload: Any) -> str:
    fragments: list[str] = []

    def _walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if value.strip():
                fragments.append(value)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item)
            return
        if isinstance(value, dict):
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                fragments.append(value["text"])
            for nested_key in ("prompt", "content", "text"):
                if nested_key in value:
                    _walk(value[nested_key])

    _walk(prompt_payload)
    return "\n".join(part.strip() for part in fragments if part.strip()).strip()


def _write_work_handoff(*, done: bool, report: str) -> None:
    handoff_path = os.environ.get("ZENITH_HANDOFF_PATH")
    if not handoff_path:
        raise BridgeError("ZENITH_HANDOFF_PATH not set")
    node_id = os.environ.get("ZENITH_NODE_ID", "unknown")
    atomic_write_json(
        handoff_path,
        {
            "node_id": node_id,
            "done": done,
            "report": report,
            "request_attention": False,
        },
    )


def best_error(text: str, fallback: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:400] if collapsed else fallback


def find_git_root(path: str | Path) -> Path:
    """Find canonical git root from any workspace path or symlink."""
    import subprocess
    p = Path(path).expanduser().resolve()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).expanduser().resolve()
    except Exception:
        pass
    for parent in (p, *p.parents):
        if (parent / ".git").exists():
            return parent.resolve()
    return p


class JulesACPBridge:
    def __init__(self) -> None:
        self._transport = JsonRpcTransport()
        self._sessions: dict[str, BridgeSession] = {}

    async def serve(self) -> int:
        loop = asyncio.get_running_loop()
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return 0
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._handle_message(loop, message)

    async def _handle_message(self, loop: asyncio.AbstractEventLoop, message: JSON) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            await self._transport.respond(
                request_id,
                result={
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {
                            "audio": False,
                            "embeddedContent": False,
                            "image": False,
                        },
                    },
                    "authMethods": [],
                },
            )
            return
        if method == "session/new":
            cwd = str(Path(params.get("cwd") or os.getcwd()).expanduser().resolve())
            session_id = f"jules-{uuid.uuid4().hex[:12]}"
            self._sessions[session_id] = BridgeSession(
                session_id=session_id,
                cwd=cwd,
                mcp_servers=list(params.get("mcpServers") or []),
            )
            await self._transport.respond(request_id, result={"sessionId": session_id})
            return
        if method == "session/prompt":
            session_id = params.get("sessionId")
            session = self._sessions.get(str(session_id))
            if session is None:
                await self._transport.respond(request_id, err=f"unknown session: {session_id}")
                return
            prompt_text = _extract_prompt_text(params.get("prompt"))
            try:
                await _run_prompt(prompt_text, session.cwd, transport=self._transport, session_id=session.session_id)
            except BridgeError as exc:
                _write_work_handoff(done=False, report=f"cannot_proceed: {exc}")
                await self._transport.notify_session_update(
                    session.session_id,
                    f"fatal: {exc}",
                    message_id="jules-fatal",
                )
            await self._transport.respond(request_id, result={"stopReason": "end_turn"})
            return
        if method == "jules/converse":
            # Follow-up message to an existing Jules session (non-blocking)
            remote_id = params.get("remoteId")
            message = params.get("message")
            cwd = params.get("cwd") or os.getcwd()
            if not remote_id or not message:
                await self._transport.respond(request_id, err="jules/converse requires remoteId and message")
                return
            try:
                await self._transport.notify_session_update(
                    f"jules-{remote_id}",
                    f"Sending follow-up to Jules session {remote_id}...",
                )
                await send_jules_message(remote_id, message, cwd)
                await self._transport.notify_session_update(
                    f"jules-{remote_id}",
                    f"Message sent to {remote_id}. Circle back later for status.",
                )
                _write_work_handoff(
                    done=True,
                    report=f"Jules follow-up message sent to {remote_id}. Non-blocking — circle back later.",
                )
            except BridgeError as exc:
                _write_work_handoff(done=False, report=f"cannot_proceed: {exc}")
            await self._transport.respond(request_id, result={"stopReason": "end_turn"})
            return
        if request_id is not None:
            await self._transport.respond(request_id, err=f"method not found: {method}")


async def _run_prompt(prompt_text: str, cwd: str, *, transport: JsonRpcTransport, session_id: str) -> None:
    if not prompt_text:
        raise BridgeError("empty session/prompt payload")
    await transport.notify_session_update(session_id, "Queued task for Jules remote execution.")
    try:
        await transport.notify_session_update(session_id, "Using Jules REST API transport.")
        state = await _run_jules_rest(prompt_text, cwd)
    except Exception as exc:
        await transport.notify_session_update(
            session_id,
            f"REST API failed: {exc}. Falling back to Jules CLI transport.",
        )
        state = await _run_jules_cli(prompt_text, cwd)

    await transport.notify_session_update(
        session_id,
        f"Jules task {state.remote_id} reached terminal state: {state.status}.",
    )
    if not state.is_terminal:
        raise BridgeError(f"Jules task {state.remote_id} never reached a terminal state")
    if not state.succeeded:
        raise BridgeError(f"Jules task {state.remote_id} ended with status {state.status}")
    if not state.pr_url:
        raise BridgeError(f"Jules task {state.remote_id} succeeded but no PR URL was found")

    _write_work_handoff(
        done=True,
        report=f"Jules task {state.remote_id} completed successfully. PR: {state.pr_url}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jules ACP stdio bridge")
    parser.add_argument("--check", action="store_true", help="Exit 0 if the bridge imports cleanly")
    args = parser.parse_args(argv)
    if args.check:
        return 0
    bridge = JulesACPBridge()
    return asyncio.run(bridge.serve())


if __name__ == "__main__":
    raise SystemExit(main())