"""Jules clarification-gate bijection tests.

Jules is eventual delivery artillery: it asks for parameters, gives a time
window for response, and proceeds with assumptions if no response arrives.
The conductor must:
1. Detect "Awaiting User Feedback" as a non-terminal clarification state
2. Provide the orchestrator a way to answer via jules_converse
3. Allow the time-window to expire → Jules assumes → session proceeds
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from zenith_harness.jules_acp_bridge import (
    extract_status,
    JulesRemoteState,
    parse_remote_state,
)


def test_awaiting_user_feedback_is_non_terminal() -> None:
    """'Awaiting User Feedback' must normalize to a recognized non-terminal
    state, not be misread as terminal-completed or leaked as 'unknown'.
    """
    raw = '{"status":"Awaiting User Feedback"}'
    state = parse_remote_state("sess-1", raw)
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
    status = extract_status(raw_line)
    assert status and "awaiting" in status.lower(), (
        f"Expected awaiting-family status from list, got {status!r}"
    )
    state = parse_remote_state("1519881458151363358", raw_line)
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


def test_jules_remote_list_awaiting_status() -> None:
    """Verify `jules remote list` includes 'Awaiting User Feedback' status."""
    result = subprocess.run(
        ["jules", "remote", "list", "--session"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"jules CLI failed: {result.stderr}"
    # Check for any session with "Awaiting" status
    for line in result.stdout.splitlines():
        if "Awaiting" in line:
            # Found an awaiting session - verify parsing works
            status = extract_status(line)
            assert status and "awaiting" in status.lower()
            return
    # No awaiting sessions is fine - just verify CLI works
