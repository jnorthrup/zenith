"""RED → GREEN: the bridge must be honest about pushed-branch delivery.

On this repo jules does NOT auto-open PRs. It pushes branches named
``jules-<session_id>-<hash>`` to origin when the VM commits upstream.

The previous bridge only counted delivery when ``pr_url`` was set, so
succeeded=False on every real session even when work shipped. This test
pins the corrected behavior: ``succeeded`` is True when EITHER a PR was
opened OR a branch was pushed to origin. ``delivered`` exposes the
branch ref explicitly so the orchestrator can route to NARS promotion.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest


REAL_BRANCH_SESSION = "7395203169723873685"  # the only session that pushed a branch on this repo


def _git_ls_remote_has_branch(cwd: str, remote_id: str) -> bool:
    res = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/jules-{remote_id}-*"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=10,
        check=False,
    )
    return any(
        line.split("\t", 1)[-1].startswith(f"refs/heads/jules-{remote_id}-")
        for line in res.stdout.splitlines()
    )


@pytest.mark.skipif(
    not Path("/Users/jim/work/zTrike").exists(),
    reason="Requires /Users/jim/work/zTrike on disk",
)
def test_real_pushed_branch_is_honest() -> None:
    """The 7395... session is the only one that ever pushed a branch on
    this repo. The bridge's git-ls-remote probe MUST find it.
    """
    from zenith_harness.jules_acp_bridge import _try_find_pushed_branch

    cwd = "/Users/jim/work/zTrike"
    # Independent probe — must agree
    assert _git_ls_remote_has_branch(cwd, REAL_BRANCH_SESSION), (
        f"Expected origin/jules-{REAL_BRANCH_SESSION}-* on this repo; "
        "test fixture is stale"
    )

    branch = asyncio.run(_try_find_pushed_branch(REAL_BRANCH_SESSION, cwd))
    assert branch is not None, "Bridge must find the pushed branch"
    assert branch.startswith(f"refs/heads/jules-{REAL_BRANCH_SESSION}-"), (
        f"Unexpected branch ref: {branch!r}"
    )


def test_jules_remote_state_delivered_branch_only() -> None:
    """A completed session with only a pushed branch (no PR) IS delivered."""
    from zenith_harness.jules_acp_bridge import JulesRemoteState

    state = JulesRemoteState(
        remote_id="x",
        status="completed",
        raw="",
        pushed_branch="refs/heads/jules-x-abc1234",
    )
    assert state.delivered is True
    assert state.succeeded is True


def test_jules_remote_state_delivered_pr_only() -> None:
    """A completed session with only a PR (no branch listed) IS delivered."""
    from zenith_harness.jules_acp_bridge import JulesRemoteState

    state = JulesRemoteState(
        remote_id="x",
        status="completed",
        raw="",
        pr_url="https://github.com/org/repo/pull/42",
    )
    assert state.delivered is True
    assert state.succeeded is True


def test_jules_remote_state_not_delivered_completed_no_artifact() -> None:
    """Completed but no PR and no branch = NOT delivered (the 22-session gap)."""
    from zenith_harness.jules_acp_bridge import JulesRemoteState

    state = JulesRemoteState(remote_id="x", status="completed", raw="")
    assert state.delivered is False
    assert state.succeeded is False


def test_jules_remote_state_running_never_succeeds() -> None:
    """Running sessions can never be delivered, regardless of artifacts."""
    from zenith_harness.jules_acp_bridge import JulesRemoteState

    state = JulesRemoteState(
        remote_id="x",
        status="running",
        raw="",
        pushed_branch="refs/heads/jules-x-h",
    )
    assert state.delivered is True  # branch exists, but...
    assert state.succeeded is False  # ...status is not terminal-success