"""Tests for the canonical in-repo Jules mailbox.

The mailbox lives at <git-root>/.zenith/mailbox/ so it survives regardless of
where zenith agents live or symlinks point to. Zenith FSM owns the mailbox —
Hermes fires events and circles back; it never holds a long Jules conversation.
"""
from __future__ import annotations

import json
from pathlib import Path

from zenith_harness.jules_acp_bridge import (
    JulesRemoteState,
    _append_jules_mailbox,
    _find_git_root,
    _jules_mailbox_path,
    _load_session_store,
    _save_jules_session,
    _session_store_path,
)


def test_mailbox_uses_canonical_repo_root_through_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    link = tmp_path / "repo-link"
    link.symlink_to(repo, target_is_directory=True)

    _save_jules_session(str(link), "123", "task-1", "project-1", "mission-1")

    assert _find_git_root(str(link)) == repo.resolve()
    assert _session_store_path(str(link)) == repo.resolve() / ".zenith" / "mailbox" / "jules_sessions.json"
    assert _jules_mailbox_path(str(link), "123") == repo.resolve() / ".zenith" / "mailbox" / "jules" / "123.jsonl"

    store = _load_session_store(str(link))
    assert store["123"]["repo_root"] == str(repo.resolve())
    assert store["123"]["mailbox_path"].endswith(".zenith/mailbox/jules/123.jsonl")


def test_append_mailbox_records_events_and_updates_session_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    _save_jules_session(str(repo), "abc/123", "task-1", "project-1", "mission-1")

    # outbound message
    _append_jules_mailbox(
        str(repo), "abc/123", "message", direction="outbound", body="open a PR",
    )
    # inbound status
    _append_jules_mailbox(
        str(repo),
        "abc/123",
        "status",
        direction="inbound",
        status="completed",
        normalized_status="completed",
        pr_url="https://github.com/org/repo/pull/1",
        delivered=True,
        raw="Completed with PR",
    )

    mailbox = repo.resolve() / ".zenith" / "mailbox" / "jules" / "abc_123.jsonl"
    records = [json.loads(line) for line in mailbox.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["message", "status"]
    assert records[0]["direction"] == "outbound"
    assert records[1]["pr_url"] == "https://github.com/org/repo/pull/1"

    # session index reflects latest status
    store = _load_session_store(str(repo))
    assert store["abc/123"]["status"] == "completed"
    assert store["abc/123"]["pr_url"] == "https://github.com/org/repo/pull/1"
