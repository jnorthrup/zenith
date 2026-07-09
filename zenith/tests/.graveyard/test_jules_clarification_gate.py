"""Jules clarification-gate bijection tests.

Jules is eventual delivery artillery: it asks for parameters, gives a time
window for response, and proceeds with assumptions if no response arrives.
The conductor must:
1. Detect "Awaiting User Feedback" as a non-terminal clarification state
2. Provide the orchestrator a way to answer via jules_converse
3. Allow the time-window to expire → Jules assumes → session proceeds
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from zenith_harness.jules_acp_bridge import (
    _parse_remote_state,
    _extract_status,
    JulesRemoteState,
    _normalize_status,
    check_jules_status,
    TERMINAL_SUCCESS,
    TERMINAL_FAILURE,
)


def test_awaiting_user_feedback_is_non_terminal() -> None:
    """'Awaiting User Feedback' must normalize to a recognized non-terminal
    state, not be misread as terminal-completed or leaked as 'unknown'.
    """
    raw = '{"status":"Awaiting User Feedback"}'
    state = _parse_remote_state("sess-1", raw)
    assert state.normalized_status == "awaiting_user_feedback"
    assert state.is_terminal is False
    assert state.succeeded is False


def test_list_output_awaiting_user_feedback_parsed() -> None:
    """Real `jules remote list` row for awaiting session must parse correctly.

    Jules abbreviates the status in the list output to "Awaiting User F" when
    the row is truncated to its column width. The conductor must recognize
    both the full form and the abbreviated form.
    """
    raw_line = (
        '1519881458151363358    provide trikeshed with the nginx featureset '
        'to run a full …  jnorthrup/TrikeShed    2 days ago    Awaiting User F'
    )
    status = _extract_status(raw_line)
    assert status and "awaiting" in status.lower(), (
        f"Expected awaiting-family status from list, got {status!r}"
    )
    state = _parse_remote_state("1519881458151363358", raw_line)
    assert state.is_terminal is False
    assert state.succeeded is False
    # Conductor routes awaiting to attention_required regardless of abbreviation
    assert state.normalized_status.startswith("awaiting_user")


def test_awaiting_does_not_block_terminal_detection() -> None:
    """An awaiting session has no PR (the work hasn't been done yet)."""
    awaiting = JulesRemoteState(
        remote_id="x",
        status="awaiting_user_feedback",
        raw="",
        pr_url=None,
    )
    # Non-terminal — wait, but don't BLOCK like a poll loop.
    assert awaiting.is_terminal is False
    # No PR, so not succeeded
    assert awaiting.succeeded is False


@pytest.mark.asyncio
async def test_check_jules_status_recognizes_awaiting(tmp_path: Path) -> None:
    """Single non-blocking status check must flag awaiting state, not loop
    forever or treat it as terminal-so-stop-polling.
    """
    import asyncio
    from unittest.mock import patch
    from zenith_harness.jules_acp_bridge import JULES_BIN, _run_command

    awaiting_line = (
        '12345    add nginx featureset    jnorthrup/TrikeShed    1m ago    '
        'Awaiting User Feedback'
    )

    # Simulate REST 404 (recently-launched session) then CLI fallback
    async def mock_rest(*a, **kw):
        raise __import__(
            "zenith_harness.jules_acp_bridge", fromlist=["BridgeError"]
        ).BridgeError("404")

    async def mock_run_cmd(args, cwd=None, timeout=None):
        return 0, awaiting_line, ""

    with patch(
        "zenith_harness.jules_acp_bridge._rest_json",
        side_effect=mock_rest,
    ), patch(
        "zenith_harness.jules_acp_bridge._run_command",
        side_effect=mock_run_cmd,
    ):
        state = await check_jules_status("12345", str(tmp_path))

    assert state.normalized_status == "awaiting_user_feedback"
    assert state.is_terminal is False
    assert state.succeeded is False
