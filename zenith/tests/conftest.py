"""Shared test fixtures for v5."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zenith_harness.config import HarnessConfig
from zenith_harness.controller import ProjectController
from zenith_harness.dispatcher import MockDispatcher, MockTerminalReviewer
from zenith_harness.models import TerminalReviewHandoff, WorkHandoff
from zenith_harness.server import create_orchestrator_server
from zenith_harness.storage import ProjectStore


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def harness_home(tmp_path: Path) -> Path:
    home = tmp_path / ".zenith"
    home.mkdir()
    return home


@pytest.fixture
def contract_dir(workspace: Path) -> Path:
    d = workspace / ".zenith" / "missions" / "mission-001" / "contract"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def config(harness_home: Path) -> HarnessConfig:
    """Default HarnessConfig for v5 tests.

    Tests that need a custom config should override this fixture.
    """
    from zenith_harness import config as _cfg_mod
    bundled = _cfg_mod.__file__.replace("__init__.py", "bundled")
    return HarnessConfig(
        bundled_dir=Path(bundled),
        harness_home=harness_home,
        projects_dir=harness_home / "projects",
        orchestrator_provider_name="claude",
        worker_provider_name="claude",
        worker_acp_command=None,
        validator_provider_name=None,
        validator_acp_command=None,
        terminal_reviewer_provider_name=None,
        terminal_reviewer_acp_command=None,
        max_parallel_nodes=1,
    )


@pytest.fixture
async def started_server(config: HarnessConfig, workspace: Path) -> tuple[Any, str]:
    """Spin up an orchestrator server with a started project.

    Returns (server, project_id). Tests patch the Jules functions they need
    and call server.call_tool directly.
    """
    def responder(req: Any) -> WorkHandoff:
        return WorkHandoff(node_id="w1", done=True, report="ok")

    controller = ProjectController(
        config,
        MockDispatcher(responder),
        MockTerminalReviewer(TerminalReviewHandoff(done=True, report="")),
    )
    server = create_orchestrator_server(config, controller)
    result = await server.call_tool(
        "start_project",
        {"brief": "Test mission", "workspace_dir": str(workspace)},
    )
    # server.call_tool may return a list-like; project_id is reachable
    # via ProjectStore.list_projects()[0].id
    pid = ProjectStore(config).list_projects()[0].id
    return server, pid
