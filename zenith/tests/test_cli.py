"""CLI integration tests — init / list-projects / show-project / install-skills."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from zenith_harness.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env(harness_home: Path, workspace: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("ZENITH_HOME", str(harness_home))
    monkeypatch.chdir(workspace)
    return {"ZENITH_HOME": str(harness_home)}


def _expected_mcp_server_args() -> list[str]:
    zenith_root = Path(__file__).resolve().parents[1]
    return [
        "run",
        "--project",
        str(zenith_root),
        "zenith-server",
        "--mode",
        "orchestrator",
    ]


class TestInit:
    def test_stages_host_agent_surface_only(
        self, runner: CliRunner, workspace: Path, env: dict[str, str]
    ) -> None:
        """`zenith init` writes MCP config + provider agents + orchestrator prompt
        but does NOT create the project bucket or workspace shims — those are
        created by `start_project` at the first MCP call."""
        result = runner.invoke(
            cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"]
        )
        assert result.exit_code == 0, result.output
        # Workspace stays clean of .zenith/ — bucket lives under ZENITH_HOME.
        assert not (workspace / ".zenith").exists()
        # No symlink shims either — start_project handles them.
        assert not (workspace / "AGENTS.md").exists()
        # MCP config + .claude/agents/ are written.
        assert (workspace / ".mcp.json").exists()
        mcp = json.loads((workspace / ".mcp.json").read_text())
        assert "zenith" in mcp["mcpServers"]
        server = mcp["mcpServers"]["zenith"]
        assert server["command"] == "uv"
        assert server["args"] == _expected_mcp_server_args()

    def test_init_does_not_touch_gitignore(
        self, runner: CliRunner, workspace: Path, env: dict[str, str]
    ) -> None:
        gitignore = workspace / ".gitignore"
        gitignore.write_text("node_modules/\n")
        original = gitignore.read_text()
        r = runner.invoke(
            cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"]
        )
        assert r.exit_code == 0, r.output
        assert gitignore.read_text() == original

    def test_idempotent(
        self, runner: CliRunner, workspace: Path, env: dict[str, str]
    ) -> None:
        for _ in range(2):
            r = runner.invoke(
                cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"]
            )
            assert r.exit_code == 0, r.output
        # .mcp.json preserved across reruns.
        assert (workspace / ".mcp.json").exists()

    def test_codex_writes_codex_config(
        self, runner: CliRunner, workspace: Path, env: dict[str, str]
    ) -> None:
        r = runner.invoke(cli, ["init", "--workspace-dir", str(workspace), "--agent", "codex"])
        assert r.exit_code == 0, r.output
        config_path = workspace / ".codex" / "config.toml"
        assert config_path.exists()
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        server = config["mcp_servers"]["zenith"]
        assert server["command"] == "uv"
        assert server["args"] == _expected_mcp_server_args()
        assert f"Initialized v5 project workspace at {workspace}" in r.output
        assert "Start your agent from the initialized project workspace" in r.output
        assert (
            "First read .codex/orchestrator_prompt.md and treat it as your primary role, "
            "then use Zenith to run this mission." in r.output
        )

    def test_claude_init_writes_runtime_validator_env_names(
        self, runner: CliRunner, workspace: Path, env: dict[str, str]
    ) -> None:
        r = runner.invoke(
            cli,
            [
                "init",
                "--workspace-dir",
                str(workspace),
                "--agent",
                "claude",
                "--validator-provider",
                "codex",
                "--validator-acp-command",
                "custom-validator-acp",
            ],
        )
        assert r.exit_code == 0, r.output

        mcp = json.loads((workspace / ".mcp.json").read_text())
        mcp_env = mcp["mcpServers"]["zenith"]["env"]
        assert mcp_env["ZENITH_VALIDATOR_PROVIDER"] == "codex"
        assert mcp_env["ZENITH_VALIDATOR_ACP_COMMAND"] == "custom-validator-acp"
        assert "ZENITH_VALIDATION_WORKER_PROVIDER" not in mcp_env
        assert "ZENITH_VALIDATION_WORKER_ACP_COMMAND" not in mcp_env

    def test_claude_init_forwards_only_allowed_model_env(
        self,
        runner: CliRunner,
        workspace: Path,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
        monkeypatch.setenv("ANTHROPIC_MODEL", "glm-5.2[1m]")
        monkeypatch.setenv("ZAI_API_KEY", "zai-test-key")
        monkeypatch.setenv("DATABASE_URL", "postgres://should-not-forward")

        r = runner.invoke(cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"])
        assert r.exit_code == 0, r.output

        mcp = json.loads((workspace / ".mcp.json").read_text())
        mcp_env = mcp["mcpServers"]["zenith"]["env"]
        assert mcp_env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert mcp_env["ANTHROPIC_MODEL"] == "glm-5.2[1m]"
        assert mcp_env["ZAI_API_KEY"] == "zai-test-key"
        assert "DATABASE_URL" not in mcp_env

    def test_jules_init_creates_missing_workspace_and_stages_prompt(
        self,
        runner: CliRunner,
        tmp_path: Path,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "new-jules-workspace"
        monkeypatch.setenv("JULES_API_KEY", "jules-test-key")

        r = runner.invoke(cli, ["init", "--workspace-dir", str(target), "--agent", "jules"])
        assert r.exit_code == 0, r.output
        assert target.exists()
        assert (target / ".jules" / "orchestrator_prompt.md").exists()

        mcp = json.loads((target / ".mcp.json").read_text())
        server = mcp["mcpServers"]["zenith"]
        assert server["command"] == "uv"
        assert server["args"] == _expected_mcp_server_args()
        assert server["env"]["ZENITH_ORCHESTRATOR_PROVIDER"] == "jules"
        assert server["env"]["ZENITH_WORKER_PROVIDER"] == "jules"
        assert server["env"]["JULES_API_KEY"] == "jules-test-key"

    def test_writes_build_sha_marker(
        self,
        runner: CliRunner,
        workspace: Path,
        env: dict[str, str],
    ) -> None:
        r = runner.invoke(
            cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"]
        )
        assert r.exit_code == 0, r.output
        marker = workspace / ".zenith-build-sha"
        assert marker.exists()
        contents = marker.read_text(encoding="utf-8").strip()
        assert contents
        assert "build=" in r.output and contents in r.output

    def test_replaces_artifacts_when_sha_changes(
        self,
        runner: CliRunner,
        workspace: Path,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First init writes current SHA
        r1 = runner.invoke(
            cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"]
        )
        assert r1.exit_code == 0, r1.output
        marker = workspace / ".zenith-build-sha"
        first_sha = marker.read_text(encoding="utf-8").strip()

        # Plant a stale host-authored file the replace must NOT delete
        keep = workspace / ".claude" / "housesettings.local.json"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text('{"user":"local"}', encoding="utf-8")

        # Inject foreign agents we expect replaced
        agents = workspace / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        stale_agent = agents / "stale-review.toml"
        stale_agent.write_text("# stale\n", encoding="utf-8")

        # Monkey-patch the build-SHA function so the second init sees a different SHA
        from zenith_harness import cli as cli_mod
        monkeypatch.setattr(
            cli_mod, "_current_build_sha", lambda: "deadbeef0001"
        )
        r2 = runner.invoke(
            cli, ["init", "--workspace-dir", str(workspace), "--agent", "claude"]
        )
        assert r2.exit_code == 0, r2.output
        assert "replacing prior artifacts" in r2.output
        assert stale_agent.exists() is False
        # Host-authored file under the provider dir is *not* deleted
        assert keep.exists()
        # Marker reflects the new SHA
        assert marker.read_text(encoding="utf-8").strip() == "deadbeef0001"


class TestBuildSha:
    def test_current_build_sha_returns_string(
        self, workspace: Path, env: dict[str, str]
    ) -> None:
        from zenith_harness.cli import _current_build_sha, _read_workspace_sha, _write_workspace_sha
        sha = _current_build_sha()
        assert isinstance(sha, str) and sha
        _write_workspace_sha(workspace, sha)
        assert _read_workspace_sha(workspace) == sha
        bogus = workspace / ".zenith-build-sha"
        bogus.write_text("\n", encoding="utf-8")
        assert _read_workspace_sha(workspace) is None


class TestListProjects:
    def test_empty(self, runner: CliRunner, env: dict[str, str]) -> None:
        r = runner.invoke(cli, ["list-projects"])
        assert r.exit_code == 0
        assert "No projects" in r.output

    def test_after_creation(
        self, runner: CliRunner, workspace: Path, harness_home: Path, env: dict[str, str]
    ) -> None:
        from zenith_harness.config import HarnessConfig
        from zenith_harness.storage import ProjectStore

        ProjectStore(HarnessConfig.discover()).create_project(
            "brief", workspace, project_id="proj-x"
        )
        r = runner.invoke(cli, ["list-projects"])
        assert "proj-x" in r.output


class TestShowProject:
    def test_unknown_id(self, runner: CliRunner, env: dict[str, str]) -> None:
        r = runner.invoke(cli, ["show-project", "ghost"])
        assert r.exit_code != 0
        assert "not found" in r.output.lower()


class TestMetrics:
    def test_metrics_cmd(
        self, runner: CliRunner, workspace: Path, env: dict[str, str]
    ) -> None:
        from zenith_harness.config import HarnessConfig
        from zenith_harness.storage import ProjectStore
        from zenith_harness.models import TaskList, Task, TaskStateFile, TaskStateEntry, ContractStateFile, ContractStateEntry

        store = ProjectStore(HarnessConfig.discover())
        store.create_project("brief", workspace, project_id="proj-x")
        
        mid = "mission-001"
        store.mission_runtime_dir("proj-x", mid).mkdir(parents=True, exist_ok=True)
        store.mission_dir("proj-x", mid).mkdir(parents=True, exist_ok=True)

        tl = TaskList(tasks=[
            Task(id="t1", type="work", targets=["c1"], body="work body", depends_on=[]),
            Task(id="t2", type="validate", targets=["c1"], body="validate body", depends_on=["t1"])
        ])
        store.save_task_list("proj-x", mid, tl)

        ts = TaskStateFile(tasks={
            "t1": TaskStateEntry(status="cleared", attempt_count=1, success_count=1),
            "t2": TaskStateEntry(status="pending", attempt_count=0, success_count=0)
        })
        store.save_task_state("proj-x", mid, ts)

        cs = ContractStateFile(items={
            "c1": ContractStateEntry(status="passed")
        })
        store.save_contract_state("proj-x", mid, cs)

        contract_dir = store.zenith_dir("proj-x") / mid / "contract"
        contract_dir.mkdir(parents=True, exist_ok=True)
        (contract_dir / "c1.md").write_text("- <A --> B>\n- <C --> D>\n", encoding="utf-8")

        r = runner.invoke(cli, ["metrics", "proj-x"])
        assert r.exit_code == 0, r.output
        assert "mission" in r.output
        assert "contract" in r.output
        assert "task" in r.output
        assert "$ ---" in r.output
        
        blocks = r.output.split("$ ---\n")
        assert len(blocks) >= 4
        
        # Verify block padding (exactly 10 lines per block)
        assert blocks[0].count("\n") == 10
        assert blocks[1].count("\n") == 10
        assert blocks[2].count("\n") == 10
        assert blocks[3].count("\n") == 10
        
        first_lines = blocks[0].split("\n")
        assert "type\":\"mission\"" in first_lines[0]
        
        second_lines = blocks[1].split("\n")
        assert "type\":\"contract\"" in second_lines[0]
        assert "<A --> B>" in second_lines[2]

