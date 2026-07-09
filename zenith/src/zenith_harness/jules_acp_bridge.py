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

from .storage import atomic_write_json

JSON = dict[str, Any]
TERMINAL_SUCCESS = {"completed", "complete", "succeeded", "success", "merged"}
TERMINAL_FAILURE = {"failed", "failure", "cancelled", "canceled", "error", "errored"}
STATUS_POLL_SECONDS = float(os.environ.get("JULES_POLL_SECONDS", "5"))
MAX_TRANSIENT_RETRIES = int(os.environ.get("JULES_MAX_TRANSIENT_RETRIES", "3"))
TRANSIENT_BACKOFF_S = float(os.environ.get("JULES_TRANSIENT_BACKOFF_S", "5.0"))
JULES_BIN = os.environ.get("JULES_BIN", "jules")


# -----------------------------------------------------------------------
# OAuth Auto-Login
# -----------------------------------------------------------------------
def ensure_jules_authenticated() -> bool:
    """Ensure Jules is authenticated via OAuth.

    Triggers `jules login` to cache OAuth credentials, then uses gcloud
    to get the Bearer token for API calls. NOT GREEDY: only attempts
    login when explicitly called.
    """
    # Use TokenManager to get OAuth token - this handles jules login + gcloud
    token = _token_manager.get_token()
    return bool(token)


# -----------------------------------------------------------------------
# NARS Contract Promotion Trigger
# NOT GREEDY: Only fires on explicit trigger conditions
# -----------------------------------------------------------------------


def promote_nars_to_jules_landscape(
    project_id: str,
    mission_id: str,
    workspace_dir: str,
) -> list[str]:
    """Promote NARS contracts to Jules workspace landscape.

    NOT GREEDY: This function is triggered explicitly - e.g., when Jules
    session completes and needs the contract assertions rendered on-disk.

    Reads contract/ directory and renders 10-line JSON+contract artifacts
    into .zenith/contracts/ for Jules to consume.

    Returns list of promoted contract file paths.
    """
    from pathlib import Path
    from .storage import ProjectStore
    from .contractreifier import render_contract_head

    from .config import HarnessConfig
    store = ProjectStore(HarnessConfig.discover())
    contract_state = store.load_contract_state(project_id, mission_id)

    # Find all contract assertion files
    contract_dir = store.contract_dir(project_id, mission_id)
    if not contract_dir.exists():
        return []

    promoted: list[str] = []

    # Render each contract assertion as 10-line JSON
    for contract_file in sorted(contract_dir.glob("*.md")):
        assertion_id = contract_file.stem
        cs_entry = contract_state.items.get(assertion_id)

        # Only promote contracts that have been validated (passed or failed)
        if cs_entry is None or cs_entry.status not in ("passed", "failed"):
            continue

        # Read NARS terms from contract file
        contract_text = contract_file.read_text(encoding="utf-8")

        # Extract NARS statements - look for lines starting with -
        nars_lines = []
        for line in contract_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                nars_lines.append(stripped[2:])
            elif stripped.startswith("* "):
                nars_lines.append(stripped[2:])

        if not nars_lines:
            continue

        contract_json = store.mission_dir(project_id, mission_id) / "assertions" / f"{assertion_id}.json"
        if not contract_json.exists():
            continue

        # Create 10-line JSON artifact in Jules-accessible location
        output_dir = Path(workspace_dir) / ".zenith" / "jules_contracts"
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"{assertion_id}.json"
        contract_head = render_contract_head(
            contract_json,
            nars_lines[:9],  # Max 9 NARS terms
        )

        output_file.write_text(contract_head, encoding="utf-8")
        promoted.append(str(output_file))

    return promoted


class TokenManager:
    """Manages Jules authentication tokens.

    Two authentication methods:
    1. JULES_API_KEY - explicit API key (used directly if set)
    2. OAuth - via `jules login` + gcloud (fallback if no API key)

    The Jules API requires OAuth Bearer tokens, but JULES_API_KEY can be
    used directly for testing or when explicitly provided.
    """

    def __init__(self) -> None:
        self._token = None

    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token:
            return self._token

        # First check: explicit JULES_API_KEY takes priority
        env_key = os.environ.get("JULES_API_KEY", "")
        if env_key:
            self._token = env_key
            return env_key

        # Second: OAuth fallback via jules login + gcloud
        import subprocess
        import shutil

        # Check if jules binary exists
        if not shutil.which("jules"):
            raise BridgeError("Jules binary not found")

        # Run jules login if needed (non-interactive, uses cached credentials)
        try:
            subprocess.run(
                ["jules", "login", "--no-launch-browser"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except Exception:
            pass  # Cached credentials may already exist

        # Get OAuth token from gcloud
        try:
            res = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0:
                token = res.stdout.strip()
                if token:
                    self._token = token
                    return token
        except Exception:
            pass

        return ""

    def has_api_key(self) -> bool:
        """Check if we have valid credentials (explicit key or OAuth)."""
        # Explicit key takes priority
        if os.environ.get("JULES_API_KEY"):
            return True
        # Fallback: OAuth via jules + gcloud
        import shutil
        return bool(shutil.which("jules") and shutil.which("gcloud"))

_token_manager = TokenManager()

def __getattr__(name: str) -> Any:
    if name == "JULES_API_KEY":
        return _token_manager.get_token()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

JULES_API_BASE = os.environ.get("JULES_API_BASE", "https://aida.googleapis.com/v1")
PR_URL_RE = re.compile(r"https?://[^\s'\"]+/pull/\d+\b")
ID_PATTERNS = (
    re.compile(r"(?i)\b(?:session|task)\s*id\s*[:=#]\s*([A-Za-z0-9_-]{3,})\b"),
    re.compile(r"(?i)\b(?:session|task)\s+([A-Za-z0-9_-]{3,})\b"),
    re.compile(r"/tasks/([A-Za-z0-9_-]{3,})\b"),
    re.compile(r"/sessions/([A-Za-z0-9_-]{3,})\b"),
)
STATUS_PATTERNS = (
    re.compile(r"(?i)\bstatus\s*[:=]\s*([A-Za-z][A-Za-z _-]+)"),
    re.compile(r"(?i)\bstate\s*[:=]\s*([A-Za-z][A-Za-z _-]+)"),
    re.compile(r"(?i)\bphase\s*[:=]\s*([A-Za-z][A-Za-z _-]+)"),
)


class BridgeError(RuntimeError):
    """Raised when Jules cannot complete the request."""


@dataclass
class BridgeSession:
    session_id: str
    cwd: str
    mcp_servers: list[JSON] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class JulesRemoteState:
    remote_id: str
    status: str
    raw: str
    pr_url: str | None = None

    @property
    def normalized_status(self) -> str:
        return _normalize_status(self.status)

    @property
    def is_terminal(self) -> bool:
        normalized = self.normalized_status
        return normalized in TERMINAL_SUCCESS or normalized in TERMINAL_FAILURE

    @property
    def succeeded(self) -> bool:
        return self.normalized_status in TERMINAL_SUCCESS


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


async def _run_command(args: list[str], *, cwd: str, timeout: float = 300) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BridgeError(f"command not found: {args[0]}") from exc
    try:
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise BridgeError(f"command timed out: {' '.join(args)}") from exc
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    returncode = process.returncode if process.returncode is not None else 1
    return returncode, stdout, stderr


async def _run_jules_cli(prompt_text: str, cwd: str) -> JulesRemoteState:
    create_args = [JULES_BIN, "remote", "new", "--repo", cwd, "--session", prompt_text]
    code, stdout, stderr = await _run_command(create_args, cwd=cwd)
    combined = _combine_output(stdout, stderr)
    if code != 0:
        raise BridgeError(_best_error(combined, f"jules remote new exited {code}"))
    remote_id = _extract_remote_id(stdout) or _extract_remote_id(stderr)
    if not remote_id:
        raise BridgeError(f"unable to parse Jules session/task id from output: {combined or '(empty output)'}")
    return await _poll_jules_cli(remote_id, cwd)


# -----------------------------------------------------------------------
# Bijective LLM + Jules Launch
# -----------------------------------------------------------------------


async def launch_jules_bijective(
    prompt_text: str,
    cwd: str,
    task_id: str,
    project_id: str,
    mission_id: str,
) -> tuple[str, JulesRemoteState]:
    """Launch Jules as a bijective agent alongside LLM agent.

    Returns (remote_id, initial_state). The remote_id is used to track
    the Jules session for bijective sync.
    """
    create_args = [JULES_BIN, "remote", "new", "--repo", cwd, "--session", prompt_text]
    code, stdout, stderr = await _run_command(create_args, cwd=cwd)
    combined = _combine_output(stdout, stderr)
    if code != 0:
        raise BridgeError(_best_error(combined, f"jules remote new exited {code}"))

    remote_id = _extract_remote_id(stdout) or _extract_remote_id(stderr)
    if not remote_id:
        raise BridgeError(f"unable to parse Jules session id from output: {combined or '(empty output)'}")

    # Store the mapping for later sync
    _save_jules_session(cwd, remote_id, task_id, project_id, mission_id)

    # Return initial non-terminal state
    return remote_id, JulesRemoteState(
        remote_id=remote_id,
        status="running",
        raw=combined,
        pr_url=None,
    )


def _save_jules_session(
    cwd: str,
    remote_id: str,
    task_id: str,
    project_id: str,
    mission_id: str,
) -> None:
    """Persist Jules session mapping for bijective tracking."""
    store = _load_session_store(cwd)
    store[remote_id] = {
        "task_id": task_id,
        "project_id": project_id,
        "mission_id": mission_id,
        "created_at": time.time(),
    }
    path = _session_store_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, store)


async def _poll_jules_cli(remote_id: str, cwd: str) -> JulesRemoteState:
    last_raw = ""
    fallback_to_list = False
    while True:
        args = [JULES_BIN, "remote", "status", remote_id]
        code, stdout, stderr = await _run_command(args, cwd=cwd, timeout=120)
        combined = _combine_output(stdout, stderr)
        if code != 0 and _looks_like_unknown_subcommand(combined):
            fallback_to_list = True
        if fallback_to_list:
            code, stdout, stderr = await _run_command(
                [JULES_BIN, "remote", "list", "--session"],
                cwd=cwd,
                timeout=120,
            )
            combined = _combine_output(stdout, stderr)
            if code != 0:
                raise BridgeError(_best_error(combined, f"jules remote list --session exited {code}"))
        elif code != 0:
            if not _looks_like_unknown_subcommand(combined):
                # transient non-zero: retry up to MAX_TRANSIENT_RETRIES times
                transient_failures = getattr(_poll_jules_cli, "_transient_count", {})
                key = remote_id
                transient_failures[key] = transient_failures.get(key, 0) + 1
                _poll_jules_cli._transient_count = transient_failures  # type: ignore[attr-defined]
                if transient_failures[key] <= MAX_TRANSIENT_RETRIES:
                    await asyncio.sleep(TRANSIENT_BACKOFF_S)
                    continue
            raise BridgeError(_best_error(combined, f"jules remote status exited {code}"))

        last_raw = combined or last_raw
        state = _parse_remote_state(remote_id, combined)
        if state.is_terminal:
            if state.succeeded and not state.pr_url:
                pulled_url = await _try_extract_pr_url_from_pull(remote_id, cwd)
                if pulled_url:
                    return JulesRemoteState(
                        remote_id=remote_id,
                        status=state.status,
                        raw=state.raw,
                        pr_url=pulled_url,
                    )
            return state
        await asyncio.sleep(STATUS_POLL_SECONDS)


async def _try_extract_pr_url_from_pull(remote_id: str, cwd: str) -> str | None:
    code, stdout, stderr = await _run_command(
        [JULES_BIN, "remote", "pull", "--session", remote_id],
        cwd=cwd,
        timeout=120,
    )
    if code != 0:
        return None
    return _extract_pr_url(_combine_output(stdout, stderr))


async def _run_jules_rest(prompt_text: str, cwd: str) -> JulesRemoteState:
    create_payload = {"prompt": prompt_text, "repo": cwd}
    create = await _rest_json("POST", f"{JULES_API_BASE.rstrip('/')}/tasks", create_payload)
    remote_id = _extract_remote_id(create)
    if not remote_id:
        raise BridgeError(f"unable to parse Jules task id from REST response: {json.dumps(create, ensure_ascii=False)}")
    while True:
        data = await _rest_json("GET", f"{JULES_API_BASE.rstrip('/')}/tasks/{remote_id}")
        state = _parse_remote_state(remote_id, data)
        if state.is_terminal:
            return state
        await asyncio.sleep(STATUS_POLL_SECONDS)


async def _rest_json(method: str, url: str, payload: JSON | None = None) -> Any:
    def _do_request(token: str) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            if token.startswith("AIzaSy"):
                headers["x-goog-api-key"] = token
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(url, data=body, headers=headers, method=method)
        with urllib_request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"Jules REST returned non-JSON payload from {url}: {raw[:400]}") from exc

    token = _token_manager.get_token()
    try:
        return await asyncio.to_thread(_do_request, token)
    except urllib_error.HTTPError as exc:
        if exc.code == 401:
            # Token might be expired, refresh it and retry once
            token = _token_manager.get_token(force_refresh=True)
            try:
                return await asyncio.to_thread(_do_request, token)
            except urllib_error.HTTPError as retry_exc:
                detail = retry_exc.read().decode("utf-8", errors="replace")
                raise BridgeError(f"Jules REST {method} {url} failed after token refresh: HTTP {retry_exc.code}: {detail[:400]}") from retry_exc

        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code >= 500:
            # transient server error: retry
            for attempt in range(MAX_TRANSIENT_RETRIES):
                await asyncio.sleep(TRANSIENT_BACKOFF_S)
                try:
                    return await asyncio.to_thread(_do_request, token)
                except (urllib_error.HTTPError, urllib_error.URLError):
                    if attempt == MAX_TRANSIENT_RETRIES - 1:
                        raise BridgeError(
                            f"Jules REST {method} {url} failed after {MAX_TRANSIENT_RETRIES} retries: "
                            f"HTTP {exc.code}: {detail[:400]}"
                        ) from exc
        raise BridgeError(f"Jules REST {method} {url} failed: HTTP {exc.code}: {detail[:400]}") from exc
    except urllib_error.URLError as exc:
        # transient network error: retry
        for attempt in range(MAX_TRANSIENT_RETRIES):
            await asyncio.sleep(TRANSIENT_BACKOFF_S)
            try:
                return await asyncio.to_thread(_do_request, token)
            except urllib_error.URLError:
                if attempt == MAX_TRANSIENT_RETRIES - 1:
                    raise BridgeError(
                        f"Jules REST {method} {url} failed after {MAX_TRANSIENT_RETRIES} retries: {exc.reason}"
                    ) from exc
        raise BridgeError(f"Jules REST {method} {url} failed: {exc.reason}") from exc


def _combine_output(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
    return "\n".join(parts)


def _best_error(text: str, fallback: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:400] if collapsed else fallback


def _looks_like_unknown_subcommand(text: str) -> bool:
    normalized = text.lower()
    return "unknown command" in normalized or "help for remote" in normalized


def _extract_remote_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("sessionId", "taskId", "id", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.rsplit("/", 1)[-1]
        for value in payload.values():
            found = _extract_remote_id(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _extract_remote_id(item)
            if found:
                return found
        return None
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text:
        return None
    try:
        return _extract_remote_id(json.loads(text))
    except json.JSONDecodeError:
        pass
    for pattern in ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    standalone = re.findall(r"\b[0-9]{4,}\b", text)
    return standalone[0] if standalone else None


def _extract_pr_url(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("prUrl", "pullRequestUrl", "pull_request_url", "url"):
            value = payload.get(key)
            if isinstance(value, str):
                match = PR_URL_RE.search(value)
                if match:
                    return match.group(0)
        for value in payload.values():
            found = _extract_pr_url(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _extract_pr_url(item)
            if found:
                return found
        return None
    if not isinstance(payload, str):
        return None
    match = PR_URL_RE.search(payload)
    return match.group(0) if match else None


def _extract_status(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("status", "state", "phase"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            found = _extract_status(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _extract_status(item)
            if found:
                return found
        return None
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text:
        return None
    try:
        return _extract_status(json.loads(text))
    except json.JSONDecodeError:
        pass
    for pattern in STATUS_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    normalized = text.lower()
    for state in sorted(TERMINAL_SUCCESS | TERMINAL_FAILURE | {"running", "queued", "pending", "active"}):
        if re.search(rf"\b{re.escape(state)}\b", normalized):
            return state
    return None


def _normalize_status(status: str | None) -> str:
    if not status:
        return "unknown"
    return re.sub(r"[^a-z]+", "_", status.lower()).strip("_") or "unknown"


def _parse_remote_state(remote_id: str, payload: Any) -> JulesRemoteState:
    status = _extract_status(payload) or "running"
    pr_url = _extract_pr_url(payload)
    if isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload, ensure_ascii=False)
    return JulesRemoteState(remote_id=remote_id, status=status, raw=raw, pr_url=pr_url)


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


def _session_store_path(cwd: str) -> Path:
    """Path to persist Jules session metadata."""
    return Path(cwd) / ".zenith" / "jules_sessions.json"


def _load_session_store(cwd: str) -> dict[str, Any]:
    path = _session_store_path(cwd)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_session_store(cwd: str, store: dict[str, Any]) -> None:
    path = _session_store_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, store)


async def _poll_jules_rest(remote_id: str, cwd: str) -> JulesRemoteState:
    """Poll an existing Jules task by remote_id until terminal state."""
    while True:
        data = await _rest_json("GET", f"{JULES_API_BASE.rstrip('/')}/tasks/{remote_id}")
        state = _parse_remote_state(remote_id, data)
        if state.is_terminal:
            if state.succeeded and not state.pr_url:
                pulled_url = await _try_extract_pr_url_from_pull(remote_id, cwd)
                if pulled_url:
                    return JulesRemoteState(
                        remote_id=remote_id,
                        status=state.status,
                        raw=state.raw,
                        pr_url=pulled_url,
                    )
            return state
        await asyncio.sleep(STATUS_POLL_SECONDS)


async def _send_jules_message(remote_id: str, message: str, cwd: str) -> None:
    """Send a follow-up message to an existing Jules session via REST API."""
    create_payload = {"prompt": message}
    await _rest_json("POST", f"{JULES_API_BASE.rstrip('/')}/tasks/{remote_id}:sendMessage", create_payload)


async def _run_prompt(prompt_text: str, cwd: str, *, transport: JsonRpcTransport, session_id: str) -> None:
    if not prompt_text:
        raise BridgeError("empty session/prompt payload")
    await transport.notify_session_update(session_id, "Queued task for Jules remote execution.")
    # CLI is the reliable path — REST endpoints differ by release and return 404
    await transport.notify_session_update(session_id, "Using Jules CLI transport.")
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
            # Follow-up message to an existing Jules session
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
                await _send_jules_message(remote_id, message, cwd)
                # Poll for response
                await self._transport.notify_session_update(
                    f"jules-{remote_id}",
                    "Waiting for Jules response...",
                )
                state = await _poll_jules_rest(remote_id, cwd)
                if state.succeeded and state.pr_url:
                    await self._transport.notify_session_update(
                        f"jules-{remote_id}",
                        f"Jules updated PR: {state.pr_url}",
                    )
                    _write_work_handoff(
                        done=True,
                        report=f"Jules follow-up completed. PR: {state.pr_url}",
                    )
                else:
                    _write_work_handoff(
                        done=False,
                        report=f"Jules follow-up failed: {state.status}",
                    )
            except BridgeError as exc:
                _write_work_handoff(done=False, report=f"cannot_proceed: {exc}")
            await self._transport.respond(request_id, result={"stopReason": "end_turn"})
            return
        if request_id is not None:
            await self._transport.respond(request_id, err=f"method not found: {method}")


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
