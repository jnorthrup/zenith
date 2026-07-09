"""Non-blocking Jules bijection tests.

Jules is an eventual delivery agent — it chats but can't be waited on.
These tests verify:
1. jules_converse sends a message and returns immediately (no block-wait)
2. Live Jules CLI availability (curl-like probe)
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

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


@pytest.mark.asyncio
async def test_converse_non_blocking_returns_immediately(
    config: HarnessConfig, workspace: Path
) -> None:
    """jules_converse must send the message and return without blocking on a poll loop.

    Jules chats asynchronously — we send the message and circle back later.
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
        {"brief": "Converse non-blocking test.", "workspace_dir": str(workspace)},
    )
    pid = ProjectStore(config).list_projects()[0].id

    send_called = False

    async def mock_send_message(remote_id, message, cwd):
        nonlocal send_called
        send_called = True
        assert remote_id == "sess-456"
        assert message == "circle back on the forge-zenith PR"

    async def mock_check(remote_id, cwd):
        return JulesRemoteState(
            remote_id=remote_id, status="running", raw="{}", pr_url=None
        )

    with patch(
        "zenith_harness.jules_acp_bridge.send_jules_message",
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


def test_jules_remote_list_works() -> None:
    """Verify `jules remote list --session` works (curl-like probe)."""
    result = subprocess.run(
        ["jules", "remote", "list", "--session"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"jules CLI failed: {result.stderr}"
    # Output should have header row and at least one session
    assert "ID" in result.stdout
