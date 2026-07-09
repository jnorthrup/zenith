#!/usr/bin/env python3
"""Jules ACP bridge — non-blocking helpers for the Zenith orchestrator.

These are called from MCP tools (jules_bijective_sync, jules_converse, etc.)
and from the bijective runner in acp_runner.py. They do ONE status check and
return immediately — Jules is eventual delivery, not awaitable.

The blocking ACP worker (poll loop, REST/CLI transports, JsonRpcTransport)
lives in jules_acp_worker.py and is spawned as a subprocess.

# TODO:
# 1. Extract learned facts from completed mailbox transcripts for downstream tasks
# 2. Inject mailbox conversation context into subsequent phase prompts
# 3. Parse Jules session history for audit/knowledge retrieval
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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

JULES_API_BASE = os.environ.get("JULES_API_BASE", "https://aida.googleapis.com/v1")
PR_URL_RE = re.compile(r"https?://[^\s'\"]+/pull/\d+\b")

# ---------------------------------------------------------------------------
# OAuth Auto-Login
# ---------------------------------------------------------------------------


def ensure_jules_authenticated() -> bool:
    """Ensure Jules is authenticated via OAuth.

    Triggers `jules login` to cache OAuth credentials, then uses gcloud
    to get the Bearer token for API calls. NOT GREEDY: only attempts
    login when explicitly called.
    """
    token = _token_manager.get_token()
    return bool(token)


# -----------------------------------------------------------------------------
# NARS (Non-Axiomatic Reasoning System) Contract Promotion Trigger
# NARS = Non-Axiomatic Reasoning System (Pei Wang's logic)
# NOT GREEDY: Only fires on explicit trigger conditions
# -----------------------------------------------------------------------------


def promote_nars_to_jules_landscape(
    project_id: str,
    mission_id: str,
    workspace_dir: str,
) -> list[str]:
    """Promote NARS (Non-Axiomatic Reasoning System) contracts to Jules workspace landscape.

    NOT GREEDY: This function is triggered explicitly - e.g., when Jules
    session completes and needs the contract assertions rendered on-disk.

    Reads contract/ directory and extracts NARS (Non-Axiomatic Reasoning System) terms from markdown files,
    writing plain JSON with NARS (Non-Axiomatic Reasoning System) list to .zenith/jules_contracts/ for Jules.

    Returns list of promoted contract file paths.
    """
    from pathlib import Path
    import json
    from .storage import ProjectStore
    from .config import HarnessConfig

    store = ProjectStore(HarnessConfig.discover())
    contract_state = store.load_contract_state(project_id, mission_id)

    contract_dir = store.contract_dir(project_id, mission_id)
    if not contract_dir.exists():
        return []

    promoted: list[str] = []

    for contract_file in sorted(contract_dir.glob("*.md")):
        assertion_id = contract_file.stem
        cs_entry = contract_state.items.get(assertion_id)

        if cs_entry is None or cs_entry.status not in ("passed", "failed"):
            continue

        contract_text = contract_file.read_text(encoding="utf-8")
        nars_lines = []
        in_nars_section = False
        for line in contract_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## NARS") or stripped.startswith("## nars"):  # NARS = Non-Axiomatic Reasoning System
                in_nars_section = True
                continue
            if in_nars_section and (stripped.startswith("- ") or stripped.startswith("* ")):
                nars_lines.append(stripped[2:])
            elif in_nars_section and stripped and not stripped.startswith("#"):
                # End of NARS (Non-Axiomatic Reasoning System) section
                in_nars_section = False

        if not nars_lines:
            continue

        output_dir = Path(workspace_dir) / ".zenith" / "jules_contracts"
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"{assertion_id}.json"
        # Write plain JSON with NARS (Non-Axiomatic Reasoning System) list (no reification formatting)
        data = {"id": assertion_id, "nars": nars_lines}
        output_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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

        # First check: explicit JULES_API_KEY takes priority, but try cached OAuth token first if active
        env_key = os.environ.get("JULES_API_KEY", "")
        if env_key:
            import subprocess
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
            self._token = env_key
            return env_key

        # Second: OAuth fallback via jules login + gcloud
        import subprocess
        import shutil

        if not shutil.which("jules"):
            raise BridgeError("Jules binary not found")

        try:
            subprocess.run(
                ["jules", "login", "--no-launch-browser"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except Exception:
            pass

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
        if os.environ.get("JULES_API_KEY"):
            return True
        import shutil
        return bool(shutil.which("jules") and shutil.which("gcloud"))


_token_manager = TokenManager()


def __getattr__(name: str) -> Any:
    if name == "JULES_API_KEY":
        return _token_manager.get_token()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Shared parsing / CLI helpers
# ---------------------------------------------------------------------------


class BridgeError(RuntimeError):
    """Raised when Jules cannot complete the request."""


async def run_command(args: list[str], *, cwd: str, timeout: float = 300) -> tuple[int, str, str]:
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


def combine_output(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
    return "\n".join(parts)


def best_error(text: str, fallback: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:400] if collapsed else fallback


def looks_like_unknown_subcommand(text: str) -> bool:
    normalized = text.lower()
    return "unknown command" in normalized or "help for remote" in normalized


def extract_remote_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("sessionId", "taskId", "id", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.rsplit("/", 1)[-1]
        for value in payload.values():
            found = extract_remote_id(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = extract_remote_id(item)
            if found:
                return found
        return None
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text:
        return None
    try:
        return extract_remote_id(json.loads(text))
    except json.JSONDecodeError:
        pass
    for pattern in (
        re.compile(r"(?i)\b(?:session|task)\s*id\s*[:=#]\s*([A-Za-z0-9_-]{3,})\b"),
        re.compile(r"(?i)\b(?:session|task)\s+([A-Za-z0-9_-]{3,})\b"),
        re.compile(r"/tasks/([A-Za-z0-9_-]{3,})\b"),
        re.compile(r"/sessions/([A-Za-z0-9_-]{3,})\b"),
    ):
        match = pattern.search(text)
        if match:
            return match.group(1)
    standalone = re.findall(r"\b[0-9]{4,}\b", text)
    return standalone[0] if standalone else None


def extract_pr_url(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("prUrl", "pullRequestUrl", "pull_request_url", "url"):
            value = payload.get(key)
            if isinstance(value, str):
                match = PR_URL_RE.search(value)
                if match:
                    return match.group(0)
        for value in payload.values():
            found = extract_pr_url(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = extract_pr_url(item)
            if found:
                return found
        return None
    if not isinstance(payload, str):
        return None
    match = PR_URL_RE.search(payload)
    return match.group(0) if match else None


def extract_status(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("status", "state", "phase"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            found = extract_status(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = extract_status(item)
            if found:
                return found
        return None
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text:
        return None
    try:
        return extract_status(json.loads(text))
    except json.JSONDecodeError:
        pass
    for pattern in (
        re.compile(r"(?i)\bstatus\s*[:=]\s*([A-Za-z][A-Za-z _-]+)"),
        re.compile(r"(?i)\bstate\s*[:=]\s*([A-Za-z][A-Za-z _-]+)"),
        re.compile(r"(?i)\bphase\s*[:=]\s*([A-Za-z][A-Za-z _-]+)"),
    ):
        match = pattern.search(text)
        if match:
            return match.group(1)
    normalized = text.lower()
    if "awaiting user" in normalized:
        return "awaiting_user_feedback"
    for state in sorted(TERMINAL_SUCCESS | TERMINAL_FAILURE | {"running", "queued", "pending", "active", "awaiting_user_feedback"}):
        if re.search(rf"\b{re.escape(state)}\b", normalized):
            return state
    return None


def normalize_status(status: str | None) -> str:
    if not status:
        return "unknown"
    return re.sub(r"[^a-z]+", "_", status.lower()).strip("_") or "unknown"


def parse_remote_state(remote_id: str, payload: Any) -> JulesRemoteState:
    status = extract_status(payload) or "running"
    pr_url = extract_pr_url(payload)
    if isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload, ensure_ascii=False)
    return JulesRemoteState(remote_id=remote_id, status=status, raw=raw, pr_url=pr_url)


# ---------------------------------------------------------------------------
# Canonical in-repo mailbox (git-root anchored, symlink-safe)
# ---------------------------------------------------------------------------


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


def mailbox_dir(cwd: str | Path) -> Path:
    """Canonical in-repo mailbox root for agent conversations."""
    root = find_git_root(cwd).resolve()
    return root / ".zenith" / "mailbox"


def mission_mailbox_path(cwd: str | Path, slug: str) -> Path:
    """Mailbox file for a mission (slug = mission id).

    The first line is the contract header (plain markdown summary).
    Subsequent lines are envelope events.

    Security: enforces that the resulting path stays inside the mailbox directory.
    """
    # Reject suspicious patterns (path remnants)
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        raise ValueError(f"Invalid mailbox slug: {slug!r}")
    # Sanitize: strip path separators, resolve to prevent traversal
    safe_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", slug).lstrip("/")
    mailbox_root = mailbox_dir(cwd).resolve()
    path = (mailbox_root / f"{safe_slug}.jsonl").resolve()

    # Security: ensure path stays inside mailbox root
    try:
        path.relative_to(mailbox_root)
    except ValueError:
        raise ValueError(f"Invalid mailbox slug: escapes directory")

    return path


def jules_mailbox_path(cwd: str | Path, remote_id: str) -> Path:
    """Legacy per-session mailbox (DEPRECATED, use mission_mailbox_path)."""
    safe_remote_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", remote_id)
    return mailbox_dir(cwd) / "jules" / f"{safe_remote_id}.jsonl"


def read_mission_mailbox(
    cwd: str | Path,
    slug: str,
    *,
    to_party: str | None = None,
    since_ts: float | None = None,
    last_n: int = 1,
) -> list[dict[str, Any]]:
    """Read the last N envelope events from a mission's mailbox.

    Args:
        slug: mission id (the contract identifier)
        to_party: filter to this recipient (or None for all)
        since_ts: only events after this Unix timestamp
        last_n: return last N events (default 1)

    Returns:
        List of envelope dicts (JSONL records).
    """
    path = mission_mailbox_path(cwd, slug)
    # Backwards compat: try new path, then legacy per-session path
    if not path.exists():
        # Legacy: derive from session index
        store = load_session_store(cwd)
        session = store.get(slug) or store.get(f"jules-{slug}")
        if session:
            legacy_path = Path(session.get("mailbox_path", ""))
            if legacy_path.exists():
                path = legacy_path
    if not path.exists():
        return []

    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Filter by recipient
            if to_party and rec.get("to_party") != to_party:
                continue
            # Filter by time
            if since_ts and rec.get("unix_ts", 0) < since_ts:
                continue
            events.append(rec)
    return events[-last_n:] if last_n > 0 else events


def append_mission_mailbox(
    cwd: str | Path,
    slug: str,
    *,
    from_party: str,
    to_party: str,
    kind: str = "round",
    nars: list[str] | None = None,
    body: str = "",
) -> Path:
    """Append an envelope to a mission's mailbox.

    The envelope is NARS-anchored (Non-Axiomatic Reasoning System): ``nars`` must be non-empty (the scope discipline).
    NARS = Non-Axiomatic Reasoning System (Pei Wang's logic).

    ``body`` is capped at 200 chars.
    """
    if nars is None:
        nars = []
    if len(body) > 200:
        raise ValueError("body exceeds 200 char limit")
    path = mission_mailbox_path(cwd, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {
        "unix_ts": now,
        "from_party": from_party,
        "to_party": to_party,
        "kind": kind,
        "nars": nars,
        "body": body,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def session_store_path(cwd: str | Path) -> Path:
    """Canonical in-repo session store (inside the mailbox)."""
    return mailbox_dir(cwd) / "jules_sessions.json"


def load_session_store(cwd: str | Path) -> dict[str, Any]:
    path = session_store_path(cwd)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_session_store(cwd: str | Path, store: dict[str, Any]) -> None:
    path = session_store_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, store)


def append_jules_mailbox(cwd: str | Path, remote_id: str, event: str, **fields: Any) -> Path:
    """Append one JSONL event to the per-session mailbox and update the session index."""
    path = jules_mailbox_path(cwd, remote_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {"ts": now, "remote_id": remote_id, "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    # Keep the session index in sync so list_sessions sees latest status.
    store = load_session_store(cwd)
    entry = dict(store.get(remote_id, {}))
    entry.setdefault("created_at", now)
    entry.setdefault("repo_root", str(find_git_root(cwd)))
    entry.setdefault("mailbox_path", str(path))
    for key, value in fields.items():
        if key in ("status", "normalized_status", "pr_url", "pushed_branch", "delivered"):
            entry[key] = value
    entry["updated_at"] = now
    store[remote_id] = entry
    save_session_store(cwd, store)
    return path


def update_session_state(cwd: str | Path, remote_id: str, state: str) -> None:
    """Update a session's state (launched -> conversational -> ended)."""
    store = load_session_store(cwd)
    if remote_id in store:
        store[remote_id]["state"] = state
        store[remote_id]["updated_at"] = time.time()
        save_session_store(cwd, store)


def save_jules_session(
    cwd: str | Path,
    remote_id: str,
    task_id: str,
    project_id: str,
    mission_id: str,
    *,
    llm_done: bool = False,
    llm_report: str = "",
) -> None:
    """Persist Jules session mapping for bijective tracking."""
    store = load_session_store(cwd)
    now = time.time()
    store[remote_id] = {
        "task_id": task_id,
        "project_id": project_id,
        "mission_id": mission_id,
        "created_at": now,
        "updated_at": now,
        "llm_done": llm_done,
        "llm_report": llm_report,
        "state": "launched",
        "repo_root": str(find_git_root(cwd)),
        "mailbox_path": str(jules_mailbox_path(cwd, remote_id)),
    }
    save_session_store(cwd, store)


def get_current_repo_name(cwd: str) -> str:
    import subprocess
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            url = res.stdout.strip()
            cleaned = url.rstrip("/").replace(".git", "")
            if ":" in cleaned:
                cleaned = cleaned.split(":")[-1]
            parts = cleaned.split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Bijective LLM + Jules Launch
# ---------------------------------------------------------------------------


async def launch_jules_bijective(
    prompt_text: str,
    cwd: str,
    task_id: str,
    project_id: str,
    mission_id: str,
    *,
    llm_done: bool = False,
    llm_report: str = "",
) -> tuple[str, JulesRemoteState]:
    """Launch Jules as a bijective agent alongside LLM agent.

    Returns (remote_id, initial_state). The remote_id is used to track
    the Jules session for bijective sync.
    """
    repo_arg: str | None = None
    resolved = get_current_repo_name(cwd)
    if resolved and "/" in resolved:
        repo_arg = resolved

    create_args = [JULES_BIN, "remote", "new"]
    if repo_arg is not None:
        create_args += ["--repo", repo_arg]
    create_args += ["--session", prompt_text]
    code, stdout, stderr = await run_command(create_args, cwd=cwd)
    combined = combine_output(stdout, stderr)
    if code != 0:
        raise BridgeError(best_error(combined, f"jules remote new exited {code}"))

    remote_id = extract_remote_id(stdout) or extract_remote_id(stderr)
    if not remote_id:
        raise BridgeError(f"unable to parse Jules session id from output: {combined or '(empty output)'}")

    save_jules_session(cwd, remote_id, task_id, project_id, mission_id, llm_done=llm_done, llm_report=llm_report)
    append_jules_mailbox(
        cwd,
        remote_id,
        "launch",
        direction="outbound",
        task_id=task_id,
        project_id=project_id,
        mission_id=mission_id,
        body=prompt_text,
    )
    record_jules_dispatch(cwd, project_id)

    return remote_id, JulesRemoteState(
        remote_id=remote_id,
        status="running",
        raw=combined,
        pr_url=None,
    )


# ---------------------------------------------------------------------------
# Single-shot status check (non-blocking)
# ---------------------------------------------------------------------------



async def check_jules_quota(cwd: str) -> tuple[int, int]:
    """Check live Jules quota: (active_sessions, max_concurrent).

    Queries `jules remote list --session` and counts Running/In progress sessions.
    Returns (active_count, quota_headroom). quota_headroom = max(0, MAX_ACTIVE - active).
    """
    MAX_ACTIVE = 4  # Jules concurrent session limit
    try:
        code, stdout, stderr = await run_command(
            [JULES_BIN, "remote", "list", "--session"],
            cwd=cwd,
            timeout=60,
        )
        if code != 0:
            return (0, MAX_ACTIVE)  # Assume headroom on error
        # Parse status column - look for Running/In progress/Queued
        active = 0
        for line in stdout.splitlines():
            if any(s in line for s in ("Running", "In progress", "Queued", "Working")):
                active += 1
        headroom = max(0, MAX_ACTIVE - active)
        return (active, headroom)
    except Exception:
        return (0, MAX_ACTIVE)  # Safe default


def record_jules_dispatch(cwd: str, project_id: str) -> None:
    """Record a Jules dispatch to the 24-hour rolling log via ProjectStore."""
    try:
        from .storage import ProjectStore
        from .config import HarnessConfig
        store = ProjectStore(HarnessConfig.discover())
        store.record_jules_dispatch(project_id)
    except Exception:
        pass  # Non-fatal

async def check_jules_status(remote_id: str, cwd: str) -> JulesRemoteState:
    """Single non-blocking status check for a Jules session.

    Jules is an eventual delivery agent — it chats but can't be waited on.
    This does ONE status fetch and returns immediately. The orchestrator circles
    back later if the session is still running.

    Falls back from REST to CLI `jules remote list` parsing when REST 404s
    (recently-launched sessions are not yet visible via REST).
    """
    try:
        data = await rest_json("GET", f"{JULES_API_BASE.rstrip('/')}/tasks/{remote_id}")
        state = parse_remote_state(remote_id, data)
    except BridgeError as exc:
        # REST 404 -> fall back to CLI list (returns "Completed"/"Failed" or empty)
        code, stdout, stderr = await run_command(
            [JULES_BIN, "remote", "list", "--session"],
            cwd=cwd,
            timeout=60,
        )
        combined = combine_output(stdout, stderr)
        if code != 0:
            raise BridgeError(best_error(combined, str(exc)))
        state = parse_state_from_list_output(remote_id, combined)
        if state is None:
            state = JulesRemoteState(
                remote_id=remote_id,
                status="running",
                raw=combined,
                pr_url=None,
            )

    if state.succeeded and not state.pr_url:
        pulled_url = await try_extract_pr_url_from_pull(remote_id, cwd)
        if pulled_url:
            state = JulesRemoteState(
                remote_id=remote_id,
                status=state.status,
                raw=state.raw,
                pr_url=pulled_url,
                description=state.description,
                pushed_branch=state.pushed_branch,
            )
            # Transition to conversational when PR is created
            update_session_state(cwd, remote_id, "conversational")

    # Honest delivery check: did jules push a branch to origin?
    if state.pushed_branch is None:
        branch = await try_find_pushed_branch(remote_id, cwd)
        if branch:
            state = JulesRemoteState(
                remote_id=remote_id,
                status=state.status,
                raw=state.raw,
                pr_url=state.pr_url,
                description=state.description,
                pushed_branch=branch,
            )
    append_jules_mailbox(
        cwd,
        state.remote_id,
        "status",
        direction="inbound",
        status=state.status,
        normalized_status=state.normalized_status,
        pr_url=state.pr_url,
        pushed_branch=state.pushed_branch,
        delivered=state.delivered,
        raw=state.raw[:4000],
    )
    return state


def parse_state_from_list_output(remote_id: str, list_output: str) -> JulesRemoteState | None:
    """Parse a status entry from `jules remote list --session` output."""
    for line in list_output.splitlines():
        if remote_id not in line:
            continue
        status = extract_status(line) or "running"
        pr_url = extract_pr_url(line)
        description = extract_description(line)
        return JulesRemoteState(
            remote_id=remote_id,
            status=status,
            raw=line,
            pr_url=pr_url,
            description=description,
        )
    return None


def extract_description(line: str) -> str:
    parts = re.split(r"\s{2,}|\t", line.strip())
    if len(parts) < 2:
        return line.strip()
    return parts[1].strip() if len(parts) > 1 else ""





# ---------------------------------------------------------------------------
# REST / OAuth internals
# ---------------------------------------------------------------------------


async def rest_json(method: str, url: str, payload: JSON | None = None) -> Any:
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
            token = _token_manager.get_token(force_refresh=True)
            try:
                return await asyncio.to_thread(_do_request, token)
            except urllib_error.HTTPError as retry_exc:
                detail = retry_exc.read().decode("utf-8", errors="replace")
                raise BridgeError(f"Jules REST {method} {url} failed after token refresh: HTTP {retry_exc.code}: {detail[:400]}") from retry_exc
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code >= 500:
            for attempt in range(MAX_TRANSIENT_RETRIES):
                await asyncio.sleep(TRANSIENT_BACKOFF_S)
                try:
                    return await asyncio.to_thread(_do_request, token)
                except (urllib_error.HTTPError, urllib_error.URLError):
                    if attempt == MAX_TRANSIENT_RETRIES - 1:
                        raise BridgeError(f"Jules REST {method} {url} failed after {MAX_TRANSIENT_RETRIES} retries: HTTP {exc.code}: {detail[:400]}") from exc
        raise BridgeError(f"Jules REST {method} {url} failed: HTTP {exc.code}: {detail[:400]}") from exc
    except urllib_error.URLError as exc:
        for attempt in range(MAX_TRANSIENT_RETRIES):
            await asyncio.sleep(TRANSIENT_BACKOFF_S)
            try:
                return await asyncio.to_thread(_do_request, token)
            except urllib_error.URLError:
                if attempt == MAX_TRANSIENT_RETRIES - 1:
                    raise BridgeError(f"Jules REST {method} {url} failed after {MAX_TRANSIENT_RETRIES} retries: {exc.reason}") from exc
        raise BridgeError(f"Jules REST {method} {url} failed: {exc.reason}") from exc


async def send_jules_message(remote_id: str, message: str, cwd: str) -> None:
    """Send a follow-up message to an existing Jules session via REST API."""
    create_payload = {"prompt": message}
    await rest_json("POST", f"{JULES_API_BASE.rstrip('/')}/tasks/{remote_id}:sendMessage", create_payload)
    append_jules_mailbox(
        cwd,
        remote_id,
        "message",
        direction="outbound",
        body=message,
    )


async def try_extract_pr_url_from_pull(remote_id: str, cwd: str) -> str | None:
    code, stdout, stderr = await run_command(
        [JULES_BIN, "remote", "pull", "--session", remote_id],
        cwd=cwd,
        timeout=120,
    )
    if code != 0:
        return None
    return extract_pr_url(combine_output(stdout, stderr))


async def try_find_pushed_branch(remote_id: str, cwd: str) -> str | None:
    """Probe origin for a branch named ``jules-<session_id>-*``.

    Jules pushes work to ``refs/heads/jules-<id>-<hash>`` on the origin
    remote when the VM commits upstream. The branch ref is the honest
    signal that work shipped — jules does NOT auto-open PRs on this
    repository, so ``pr_url`` stays None even on successful delivery.

    Returns the full ref (e.g. ``refs/heads/jules-42729...-abc1234``) or
    None if no matching branch exists yet (still pushing, or failed).
    """
    import subprocess
    try:
        res = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/jules-{remote_id}-*"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return None
    except Exception:
        return None
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/jules-"):
            return parts[1]
    return None


# ---------------------------------------------------------------------------
# Public dataclass for state
# ---------------------------------------------------------------------------


@dataclass
class JulesRemoteState:
    remote_id: str
    status: str
    raw: str
    pr_url: str | None = None
    description: str = ""
    pushed_branch: str | None = None  # git ref like 'refs/heads/jules-<id>-<hash>' if upstream branch landed

    @property
    def normalized_status(self) -> str:
        return normalize_status(self.status)

    @property
    def is_terminal(self) -> bool:
        normalized = self.normalized_status
        return normalized in TERMINAL_SUCCESS or normalized in TERMINAL_FAILURE

    @property
    def needs_orchestrator_answer(self) -> bool:
        return self.normalized_status == "awaiting_user_feedback"

    @property
    def delivered(self) -> bool:
        return self.pr_url is not None or self.pushed_branch is not None

    @property
    def succeeded(self) -> bool:
        normalized = self.normalized_status
        if normalized not in TERMINAL_SUCCESS:
            return False
        return self.delivered


if __name__ == "__main__":
    import sys
    print("jules_acp_bridge imports cleanly")
    sys.exit(0)