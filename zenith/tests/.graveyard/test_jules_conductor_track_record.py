"""Tests for the TrikeShed zenith conductor bijection — drive Jules + Zenith
through real session lifecycle to advance the track record.

The conductor is:
  real Jules session ↔ non-blocking Zenith bijective sync ↔ NARS contract promotion

These tests prove the conductor tracks real sessions, doesn't get stuck waiting,
and promotes NARS contracts when sessions reach terminal state.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from zenith_harness.config import HarnessConfig
from zenith_harness.jules_acp_bridge import (
    _parse_remote_state,
    _extract_status,
    JulesRemoteState,
)


# ---------------------------------------------------------------------------
# RED: parse the real jules remote list Completed state correctly
# ---------------------------------------------------------------------------


def test_parse_jules_remote_list_completed() -> None:
    """`_parse_remote_state` must recognize "Completed" status from a real
    jules remote list line, not fall back to "running".

    Bug: the conductor reads session-create output which has no status field,
    so it defaults to "running". The Completed sessions from `jules remote list`
    never get marked terminal and the bijection becomes stale.
    """
    # Real format from `jules remote list --session`:
    # `15038806424387816539    Read README.md...    jnorthrup/CCEKCMMK...    3m36s ago    Completed`
    raw_completed_line = (
        "15038806424387816539    Read README.md, list the first 3 top-level "
        "sections as a co…  jnorthrup/CCEKCMMKPla…    3m36s ago    Completed"
    )
    status = _extract_status(raw_completed_line)
    assert status and status.lower() == "completed", (
        f"_extract_status must read 'Completed' from list output, got {status!r}"
    )

    state = _parse_remote_state("15038806424387816539", raw_completed_line)
    assert state.normalized_status == "completed", (
        f"normalized_status must be terminal-completed, got {state.normalized_status}"
    )
    assert state.is_terminal is True
    # Conductor rule: completed-no-PR is not a delivery. Real evidence:
    # session 15038806424387816539 → `jules remote pull` returned "No diff found"
    assert state.succeeded is False, (
        "Completed without PR must not register as succeeded in the bijection. "
        "See test_completed_no_diff_is_not_succeeded for the conductor rule."
    )


def test_parse_jules_remote_list_running() -> None:
    """`_parse_remote_state` must recognize in-progress sessions too."""
    # Real format: trailing blank in the Status column means still running
    raw_running_line = (
        "6160573275259219693    # ⚡ Performance Optimization TaskYou are a "
        "performance-foc…  jnorthrup/TrikeShed    2 days ago"
    )
    status = _extract_status(raw_running_line)
    # No completed/failed keyword → expected default is "running"
    state = _parse_remote_state("6160573275259219693", raw_running_line)
    assert state.is_terminal is False, (
        f"Expected non-terminal for running session, got {state.normalized_status}"
    )


def test_parse_failed_session() -> None:
    """`_parse_remote_state` must mark failed sessions terminal but not succeeded."""
    raw_failed = '{"status":"failed","id":"sess-1","error":"build error"}'
    state = _parse_remote_state("sess-1", raw_failed)
    assert state.is_terminal is True
    assert state.succeeded is False
    # failed → routes to patch debt mitigation
    assert state.normalized_status == "failed"


# ---------------------------------------------------------------------------
# RED: real session → contract promotion path
# ---------------------------------------------------------------------------


def readme_first_section_prompt() -> str:
    """Tiny read-only prompt — minimal Jules work to advance track record.

    NB: this prompt is a bad direction-follower (it asks Jules to add a
    comment without giving it a file to comment on). It reliably produces
    Completed without a diff, exposing the conductor's gap.
    """
    return (
        "Read README.md and list the first 3 top-level markdown headers in a "
        "single comment-only PR. No source code changes."
    )


async def _run_for_pull_test(remote_id: str):
    """Run `jules remote pull --session <id>` and return (code, stdout, stderr)."""
    from zenith_harness.jules_acp_bridge import JULES_BIN, _run_command
    return await _run_command(
        [JULES_BIN, "remote", "pull", "--session", remote_id],
        cwd="/Users/jim/work/zTrike",
        timeout=60,
    )


# ---------------------------------------------------------------------------
# RED: completed-but-no-diff must NOT count as a delivery
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires live Jules API - disabled in CI")
async def test_completed_no_diff_is_not_succeeded(tmp_path: Path) -> None:
    """Jules-7395203169723873685 completed but produced no diff — conductor
    must NOT mark succeeded=True just because status==completed.

    Failure-to-follow-directions: the agent was asked to add a comment but
    couldn't figure out where. Status moved to Completed anyway. The conductor
    must verify: status==completed AND pr_url != None → succeeded=True.
    """
    from zenith_harness.jules_acp_bridge import (
        _parse_state_from_list_output,
        JulesRemoteState,
        _try_extract_pr_url_from_pull,
    )

    list_line = (
        "7395203169723873685    Read README.md, list the first 3 top-level "
        "sections as a co…  jnorthrup/CCEKCMMKPlat  2m14s ago    Completed"
    )
    state = _parse_state_from_list_output("7395203169723873685", list_line)
    assert state is not None and state.normalized_status == "completed"

    # Confirm the actual pull returns "No diff found"
    code, stdout, stderr = await _run_for_pull_test(state.remote_id)
    combined = (stdout or "") + (stderr or "")
    assert "no diff" in combined.lower() or "no changes" in combined.lower(), (
        f"Expected empty-diff result, got: {combined[:200]}"
    )

    pr_url = await _try_extract_pr_url_from_pull(state.remote_id, "/Users/jim/work/zTrike")
    assert pr_url is None, f"Expected no PR when no diff, got {pr_url}"

    # The conductor's truthful answer: succeeded = has_pr AND is_terminal-succeeded.
    has_pr = pr_url is not None
    assert has_pr is False

    # Pinning the RED: today's `is_terminal`/`succeeded` don't account for pr_url.
    # The fix is to require both: completed + pr_url.
    corrected = JulesRemoteState(
        remote_id=state.remote_id,
        status="completed",
        raw=state.raw,
        pr_url=pr_url,
    )
    # RED: current `succeeded` ignores pr_url — it returns True.
    # We want: completed-without-PR → succeeded=False (debt mitigation route).
    assert corrected.succeeded is False, (
        "FAILURE-TO-FOLLOW-DIRECTIONS: completed-without-PR must NOT succeed"
    )


# ---------------------------------------------------------------------------
# Real session track-record advance (separate from no-diff test)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires live Jules API - disabled in CI")
async def test_real_session_track_record_promotes_contract(config: HarnessConfig) -> None:
    """End-to-end: real Jules session → bijection store non-zero → conductor
    declares the session.

    Advances the conductor's track record by firing a real session against
    TrikeShed via bijection. Verifies the session lands in jules_sessions.json.
    """
    from zenith_harness.jules_acp_bridge import (
        launch_jules_bijective,
        check_jules_status,
        _load_session_store,
    )

    workspace = Path("/Users/jim/work/zTrike")

    # Start the project against the real TrikeShed workspace so the Jules CLI
    # can resolve its git remote.
    from zenith_harness.controller import ProjectController
    from zenith_harness.dispatcher import (
        DispatchRequest,
        MockDispatcher,
        MockTerminalReviewer,
    )
    from zenith_harness.models import TerminalReviewHandoff, WorkHandoff
    from zenith_harness.server import create_orchestrator_server

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
        {"brief": "conductor track-record advance", "workspace_dir": str(workspace)},
    )
    from zenith_harness.storage import ProjectStore
    pid = ProjectStore(config).list_projects()[0].id

    remote_id, _ = await launch_jules_bijective(
        prompt_text=readme_first_section_prompt(),
        cwd=str(workspace),
        task_id="conductor-track-record",
        project_id=pid,
        mission_id="mission-001",
    )
    assert re.match(r"^\d{6,}$", remote_id), f"Unexpected remote_id: {remote_id}"

    state = await check_jules_status(remote_id, str(workspace))
    assert state.remote_id == remote_id

    store = _load_session_store(str(workspace))
    assert remote_id in store, "Session must be in bijection store"
    assert store[remote_id]["task_id"] == "conductor-track-record"
    assert store[remote_id]["project_id"] == pid
    assert store[remote_id]["mission_id"] == "mission-001"
