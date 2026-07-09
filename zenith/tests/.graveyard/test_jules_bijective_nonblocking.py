"""Non-blocking Jules bijection + real TrikeShed Jules API tests.

Jules is an eventual delivery agent — it chats but can't be waited on.
These tests verify:
1. jules_bijective_sync does a single non-blocking status check (no poll loop)
2. jules_converse sends a message and returns immediately (no block-wait)
3. Real Jules sessions fire against TrikeShed and produce trackable state
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from zenith_harness.config import HarnessConfig
from zenith_harness.controller import ProjectController
from zenith_harness.dispatcher import (
    DispatchRequest,
    MockDispatcher,
    MockTerminalReviewer,
)
from zenith_harness.models import TerminalReviewHandoff, WorkHandoff
from zenith_harness.server import create_orchestrator_server
from zenith_harness.storage import ProjectStore


TRIKESHED_REPO = "/Users/jim/work/zTrike"
TRIKESHED_GITHUB = "jnorthrup/TrikeShed"


# ---------------------------------------------------------------------------
# RED: jules_bijective_sync must be non-blocking (single-shot, no poll loop)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="legacy test for removed blocking function - the worker is separate process now")
async def test_bijective_sync_single_shot_no_poll(config: HarnessConfig, workspace: Path) -> None:
    """jules_bijective_sync must do ONE status check, not a poll loop.

    Jules can't be waited on. The tool should fetch current state and return
    immediately, letting the orchestrator circle back later.
    """
    from zenith_harness.jules_acp_bridge import JulesRemoteState

    def responder(req: DispatchRequest) -> WorkHandoff:
        return WorkHandoff(node_id="w1", done=True, report="ok")

    controller = ProjectController(
        config,
        MockDispatcher(responder),
        MockTerminalReviewer(TerminalReviewHandoff(done=True, report="")),
    )
    server = create_orchestrator_server(config, controller)
    await server.call_tool(
        "start_project",
        {"brief": "Non-blocking sync test.", "workspace_dir": str(workspace)},
    )
    pid = ProjectStore(config).list_projects()[0].id

    call_count = 0

    async def mock_single_check(remote_id, cwd):
        nonlocal call_count
        call_count += 1
        return JulesRemoteState(
            remote_id=remote_id, status="running", raw="{}", pr_url=None
        )

    # Patch the blocking _poll_jules_rest to prove it's NOT called
    with patch(
        "zenith_harness.jules_acp_bridge._poll_jules_rest",
        side_effect=AssertionError("_poll_jules_rest must not be called — Jules can't be waited on"),
    ):
        with patch(
            "zenith_harness.jules_acp_bridge.check_jules_status",
            side_effect=mock_single_check,
        ):
            res_list = await server.call_tool(
                "jules_bijective_sync",
                {"project_id": pid, "remote_id": "sess-123"},
            )
            res = res_list.structured_content
            assert res["zenith_mapped_state"] == "mission_running"
            assert res["succeeded"] is False
            assert res["pr_url"] is None

    assert call_count == 1, f"Expected exactly 1 status check, got {call_count}"


@pytest.mark.asyncio
async def test_converse_non_blocking_returns_immediately(config: HarnessConfig, workspace: Path) -> None:
    """jules_converse must send the message and return without blocking on a poll loop.

    Jules chats asynchronously — we send the message and circle back later.
    """
    def responder(req: DispatchRequest) -> WorkHandoff:
        return WorkHandoff(node_id="w1", done=True, report="ok")

    controller = ProjectController(
        config,
        MockDispatcher(responder),
        MockTerminalReviewer(TerminalReviewHandoff(done=True, report="")),
    )
    server = create_orchestrator_server(config, controller)
    await server.call_tool(
        "start_project",
        {"brief": "Converse non-blocking test.", "workspace_dir": str(workspace)},
    )
    pid = ProjectStore(config).list_projects()[0].id

    send_called = False

    async def mock_send_message(remote_id, message, cwd):
        nonlocal send_called
        send_called = True
        assert remote_id == "sess-456"
        assert message == "circle back on the forge-zenith PR"

    from zenith_harness.jules_acp_bridge import JulesRemoteState

    async def mock_check(remote_id, cwd):
        return JulesRemoteState(remote_id=remote_id, status="running", raw="{}", pr_url=None)

    with patch(
        "zenith_harness.jules_acp_bridge._send_jules_message",
        side_effect=mock_send_message,
    ), patch(
        "zenith_harness.jules_acp_bridge.check_jules_status",
        side_effect=mock_check,
    ):
        res_list = await server.call_tool(
            "jules_converse",
            {
                "project_id": pid,
                "remote_id": "sess-456",
                "message": "circle back on the forge-zenith PR",
            },
        )
        res = res_list.structured_content
        assert send_called is True
        assert res["remote_id"] == "sess-456"
        assert res["sent"] is True
        # Non-blocking: single status check, no PR yet
        assert res.get("pr_url") is None
        assert "Jules chats but can't be waited on" in res.get("note", "")


# ---------------------------------------------------------------------------
# RED: Real Jules session against TrikeShed — advance the zero track record
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires live Jules API - disabled in CI")
async def test_launch_bijective_resolves_git_remote() -> None:
    """launch_jules_bijective must pass --repo <org/name>, not the local cwd path.

    Bug: passes cwd="/Users/jim/work/zTrike" as --repo, but CLI expects "jnorthrup/TrikeShed".
    Jules rejects it as "unknown/zTrike", sessions never produce trackable PRs.
    """
    from zenith_harness.jules_acp_bridge import (
        launch_jules_bijective,
        _save_jules_session,
        _get_current_repo_name,
        _load_session_store,
    )

    cwd = TRIKESHED_REPO
    # Prove the local cwd doesn't match GitHub repo name as-is
    assert cwd != TRIKESHED_GITHUB
    # But resolution works
    name = _get_current_repo_name(cwd)
    # Either jnorthrup/TrikeShed or jnorthrup/CCEKCMMKPlatform (depending on remote)
    assert name and "/" in name, f"Repo name resolution failed: {name!r}"

    # Now fire the real launch — CLI calls --repo <NAME> not --repo <path>
    prompt = "Read README.md, list the first 3 top-level sections as a comment in a new PR."
    remote_id, initial_state = await launch_jules_bijective(
        prompt_text=prompt,
        cwd=cwd,
        task_id="task-track-record",
        project_id="trikeshed",
        mission_id="mission-001",
    )

    assert remote_id, "Jules must return a session id"
    assert re.match(r"^\d{6,}$", remote_id), f"Unexpected remote_id format: {remote_id}"
    assert not initial_state.is_terminal, "Freshly launched session should not be terminal yet"
    assert initial_state.normalized_status in (
        "running", "queued", "pending", "active",
    ), f"Unexpected initial status: {initial_state.normalized_status}"

    # Persist bijection mapping (zero → one entry)
    _save_jules_session(
        cwd, remote_id, "task-track-record", "trikeshed", "mission-001",
        llm_done=False, llm_report="Launched via bijection track record test.",
    )

    # Verify bijection state persisted
    store = _load_session_store(cwd)
    assert remote_id in store, f"Session {remote_id} not persisted"
    entry = store[remote_id]
    assert entry["task_id"] == "task-track-record"
    assert entry["project_id"] == "trikeshed"
    assert entry["mission_id"] == "mission-001"
