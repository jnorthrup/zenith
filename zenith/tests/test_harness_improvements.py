import asyncio
import os
import shutil
from pathlib import Path
from datetime import datetime, UTC, timedelta
import pytest

from zenith_harness.acp_runner import ACPNodeRunner, ACPClient, _port_lock, _allocated_ports
from zenith_harness.storage import ProjectStore, workspace_fingerprint
from zenith_harness.cli import _replace_managed_block, MCP_ENV_FORWARD_ALLOWLIST
from zenith_harness.providers import PROVIDERS, ORCHESTRATOR_PROVIDER_NAMES, WORKER_PROVIDER_NAMES
from zenith_harness.config import HarnessConfig


@pytest.mark.asyncio
async def test_concurrent_port_allocation():
    # Make sure to clear state before test
    async with _port_lock:
        _allocated_ports.clear()
        
    async def allocate_one():
        port = await ACPNodeRunner._allocate_free_port()
        return port

    # Run 10 allocations concurrently
    ports = await asyncio.gather(*(allocate_one() for _ in range(10)))
    
    # Ensure they are all unique and recorded in the set
    assert len(ports) == 10
    assert len(set(ports)) == 10
    async with _port_lock:
        for p in ports:
            assert p in _allocated_ports
            _allocated_ports.discard(p)


def test_workspace_fingerprint_pruning(tmp_path: Path):
    # Setup test workspace structure
    ws = tmp_path / "workspace"
    ws.mkdir()
    
    # Normal files
    (ws / "app.py").write_text("print('hello')", encoding="utf-8")
    
    # Excluded directory (.venv) with newer python files
    venv_dir = ws / ".venv"
    venv_dir.mkdir()
    venv_file = venv_dir / "lib.py"
    venv_file.write_text("print('venv')", encoding="utf-8")
    # Make venv file have a very new mtime
    os.utime(venv_file, (datetime.now().timestamp() + 1000, datetime.now().timestamp() + 1000))

    # Excluded directory (node_modules)
    node_dir = ws / "node_modules"
    node_dir.mkdir()
    node_file = node_dir / "index.js"
    node_file.write_text("console.log('node')", encoding="utf-8")
    os.utime(node_file, (datetime.now().timestamp() + 2000, datetime.now().timestamp() + 2000))

    # Regular file with normal mtime
    os.utime(ws / "app.py", (datetime.now().timestamp() - 1000, datetime.now().timestamp() - 1000))

    # Get fingerprint
    fp = workspace_fingerprint(ws)
    assert fp is not None
    # If pruning works, it should not include the mtimes of the .venv or node_modules files.
    # We can check that the fingerprint is different or we can mock/assert traversal wasn't hit.
    # To prove exclusions work, let's compare with a fingerprint where there are no venv/node_modules files.
    ws_clean = tmp_path / "workspace_clean"
    ws_clean.mkdir()
    (ws_clean / "app.py").write_text("print('hello')", encoding="utf-8")
    os.utime(ws_clean / "app.py", (datetime.now().timestamp() - 1000, datetime.now().timestamp() - 1000))
    
    fp_clean = workspace_fingerprint(ws_clean)
    assert fp == fp_clean  # They should be identical since the venv and node_modules modifications are ignored!


def test_sweep_stale_attempts_timestamp_parsing(tmp_path: Path):
    # We want to test sweep_stale_attempts with real records
    harness_home = tmp_path / "zenith"
    config = HarnessConfig(
        bundled_dir=tmp_path,
        harness_home=harness_home,
        projects_dir=harness_home / "projects",
        orchestrator_provider_name="claude",
        worker_provider_name="claude",
        worker_acp_command=None,
        validator_provider_name=None,
        validator_acp_command=None,
        terminal_reviewer_provider_name=None,
        terminal_reviewer_acp_command=None,
    )
    store = ProjectStore(config)
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    store.create_project("brief", ws_dir, project_id="p1")
    
    # Write a mock attempt file with filesafe timestamp
    # 8 days ago
    old_dt = datetime.now(UTC) - timedelta(days=8)
    old_ts = old_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z").replace(":", "-")
    
    # 2 days ago
    recent_dt = datetime.now(UTC) - timedelta(days=2)
    recent_ts = recent_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z").replace(":", "-")
    
    old_attempt_json = store.attempt_path("p1", "mission-001", old_ts, "w1")
    old_attempt_json.parent.mkdir(parents=True, exist_ok=True)
    old_attempt_json.write_text('{"node_id": "w1", "done": false, "report": "old"}', encoding="utf-8")
    
    recent_attempt_json = store.attempt_path("p1", "mission-001", recent_ts, "w1")
    recent_attempt_json.write_text('{"node_id": "w1", "done": false, "report": "recent"}', encoding="utf-8")
    
    # Sweep attempts with max_age_days = 7
    tombstoned = store.sweep_stale_attempts("p1", "mission-001", max_age_days=7, max_per_node=5)
    assert tombstoned == 1
    
    # The old one should be tombstoned
    assert not old_attempt_json.exists()
    assert recent_attempt_json.exists()


@pytest.mark.asyncio
async def test_custom_permissions_handler():
    client = ACPClient(process=None, working_dir="/")
    
    # 1. Prefer allow_session
    params1 = {
        "options": [
            {"optionId": "once", "kind": "allow_once"},
            {"optionId": "session", "kind": "allow_session"},
            {"optionId": "always", "kind": "allow_always"},
        ]
    }
    res1 = await client._dispatch("session/request_permission", params1)
    assert res1["outcome"]["optionId"] == "session"
    
    # 2. Prefer allow_always if allow_session is missing
    params2 = {
        "options": [
            {"optionId": "once", "kind": "allow_once"},
            {"optionId": "always", "kind": "allow_always"},
        ]
    }
    res2 = await client._dispatch("session/request_permission", params2)
    assert res2["outcome"]["optionId"] == "always"

    # 3. Fallback to allow_once
    params3 = {
        "options": [
            {"optionId": "once", "kind": "allow_once"},
        ]
    }
    res3 = await client._dispatch("session/request_permission", params3)
    assert res3["outcome"]["optionId"] == "once"


def test_replace_managed_block_partial_markers(tmp_path: Path):
    path = tmp_path / "config.toml"
    
    # Case 1: Start marker only
    path.write_text("model = 'gpt-4'\n# BEGIN zenith\nold_config = 1\n", encoding="utf-8")
    _replace_managed_block(path, "# BEGIN zenith", "# END zenith", "# BEGIN zenith\nnew_config = 2\n# END zenith")
    content = path.read_text(encoding="utf-8")
    assert "old_config" not in content
    assert "new_config = 2" in content
    # Should only have one BEGIN and one END
    assert content.count("# BEGIN zenith") == 1
    assert content.count("# END zenith") == 1

    # Case 2: End marker only
    path.write_text("model = 'gpt-4'\nold_config = 1\n# END zenith\nsome_other = 3", encoding="utf-8")
    _replace_managed_block(path, "# BEGIN zenith", "# END zenith", "# BEGIN zenith\nnew_config = 2\n# END zenith")
    content = path.read_text(encoding="utf-8")
    assert "old_config" not in content
    assert "new_config = 2" in content
    assert content.count("# BEGIN zenith") == 1
    assert content.count("# END zenith") == 1


def test_providers_registry_names():
    assert "claude" in ORCHESTRATOR_PROVIDER_NAMES
    assert "claude" in WORKER_PROVIDER_NAMES
    assert len(ORCHESTRATOR_PROVIDER_NAMES) == len(PROVIDERS)
    assert len(WORKER_PROVIDER_NAMES) == len(PROVIDERS)


def test_forward_allowlist_expanded():
    assert "OPENAI_API_KEY" in MCP_ENV_FORWARD_ALLOWLIST
    assert "GEMINI_API_KEY" in MCP_ENV_FORWARD_ALLOWLIST
    assert "DEEPSEEK_API_KEY" in MCP_ENV_FORWARD_ALLOWLIST
