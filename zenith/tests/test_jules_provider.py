"""Jules provider ACP subprocess behavior tests."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

from zenith_harness.acp_runner import _acp_subprocess_env, _augment_acp_command
from zenith_harness.providers import get_provider


def test_acp_subprocess_env_jules(monkeypatch) -> None:
    monkeypatch.setenv("JULES_API_KEY", "test-jules-key")

    env = _acp_subprocess_env(get_provider("jules"))

    assert env["JULES_API_KEY"] == "test-jules-key"


def test_acp_subprocess_env_jules_no_codex_vars() -> None:
    env = _acp_subprocess_env(get_provider("jules"))

    assert "CODEX_SANDBOX" not in env


def test_augment_acp_command_jules() -> None:
    assert _augment_acp_command("jules-acp-bridge", get_provider("jules")) == "jules-acp-bridge"


def test_jules_default_acp_command_matches_entrypoint() -> None:
    """providers.py default_worker_acp_command must match installed entrypoint name."""
    provider = get_provider("jules")
    assert provider.default_worker_acp_command == "jules-acp-bridge"


def test_bridge_retries_transient_cli_error() -> None:
    """Bridge should retry up to MAX_TRANSIENT_RETRIES on transient non-zero CLI exit."""
    import zenith_harness.jules_acp_bridge as bridge

    call_count = 0

    async def fake_run_command(args, cwd=None, timeout=120):
        nonlocal call_count
        if "status" in args or "list" in args:
            call_count += 1
            if call_count <= 2:
                # transient failure: non-zero exit with a non-unknown-subcommand message
                return (1, "", "temporary 503 from upstream")
            # third call: success
            return (0, "status: completed\nPR: https://github.com/example/repo/pull/99", "")
        # jules remote new
        return (0, "session id: abc123", "")

    # Reset transient count state between test runs
    if hasattr(bridge._poll_jules_cli, "_transient_count"):
        del bridge._poll_jules_cli._transient_count  # type: ignore[attr-defined]

    with patch.object(bridge, "_run_command", side_effect=fake_run_command), \
         patch.object(bridge, "TRANSIENT_BACKOFF_S", 0.0):
        state = asyncio.run(bridge._poll_jules_cli("abc123", "/tmp"))

    assert state.succeeded, f"expected succeeded, got status={state.status!r}"
    assert "https://github.com/example/repo/pull/99" in (state.pr_url or ""), f"expected PR URL, got {state.pr_url!r}"
    assert call_count == 3, f"expected 3 status calls (2 transient + 1 success), got {call_count}"

