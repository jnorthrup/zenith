from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import click

from .assets import AssetLoader, iter_skill_directories
from .config import HarnessConfig
from .envelope import render_task_list
from .providers import (
    ProviderDefinition,
    ProviderSelection,
    default_worker_provider_name,
    get_provider,
    provider_names_for_role,
)
from .storage import ProjectStore

MCP_ENV_FORWARD_ALLOWLIST = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "GLM_API_KEY",
    "GLM_BASE_URL",
    "JULES_API_KEY",
    "JULES_BASE_URL",
    "MAX_THINKING_TOKENS",
    "ZAI_API_KEY",
    "ZAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_BASE_URL",
    "GEMINI_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "GROQ_API_KEY",
    "GROQ_BASE_URL",
    "TOGETHER_API_KEY",
    "TOGETHER_BASE_URL",
    "MISTRAL_API_KEY",
    "MISTRAL_BASE_URL",
    "LLAMA_API_KEY",
)

BUILD_SHA_MARKER = ".zenith-build-sha"


def _current_build_sha() -> str:
    """Best-effort git SHA for the running Zenith.

    Falls back to a monotonic counter when not in a git checkout. The result
    is stable across repeated calls within the same process.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "no-git"


def _read_workspace_sha(workspace: Path) -> str | None:
    marker = workspace / BUILD_SHA_MARKER
    if not marker.exists():
        return None
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _phase_stale_zenith_artifacts(workspace: Path, selection: ProviderSelection) -> None:
    """Remove prior Zenith-written artifacts so they can be rewritten cleanly.

    Only deletes paths Zenith itself writes. Host-authored files are left alone:
    only the closest parent directory *owned* by Zenith (agents/out, skills, the
    orchestrator-prompt file itself) is removed — not the entire `.claude/` /
    `.codex/` / `.jules/` root, which may contain user-authored configs.
    """
    targets: list[Path] = []
    providers = selection.providers()
    for provider in providers:
        if not provider.agent_output_dir:
            continue
        agents_dir = (workspace / provider.agent_output_dir).resolve()
        if agents_dir.exists():
            targets.append(agents_dir)
        for rel in provider.skill_dirs:
            sd = (workspace / rel).resolve()
            if sd.exists():
                targets.append(sd)
        if provider.orchestrator_prompt_output_path:
            ppath = (workspace / provider.orchestrator_prompt_output_path).resolve()
            if ppath.exists() and ppath.is_file():
                targets.append(ppath)
    for p in targets:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink()
        except OSError:
            pass


def _write_workspace_sha(workspace: Path, sha: str) -> None:
    (workspace / BUILD_SHA_MARKER).write_text(sha + "\n", encoding="utf-8")


@click.group()
def cli() -> None:
    """Zenith CLI — set up + inspect long-running coding projects."""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--agent",
    type=click.Choice(provider_names_for_role("orchestrator")),
    default=None,
    help="Convenience: sets orchestrator+worker provider in one shot.",
)
@click.option(
    "--orchestrator-provider",
    type=click.Choice(provider_names_for_role("orchestrator")),
    default=None,
)
@click.option(
    "--worker-provider",
    type=click.Choice(provider_names_for_role("worker")),
    default=None,
)
@click.option("--worker-acp-command", default=None)
@click.option("--validator-provider", type=click.Choice(provider_names_for_role("worker")), default=None)
@click.option("--validator-acp-command", default=None)
@click.option("--terminal-reviewer-provider", type=click.Choice(provider_names_for_role("worker")), default=None)
@click.option("--terminal-reviewer-acp-command", default=None)
@click.option("--zenith-home", type=click.Path(), default=None)
@click.option("--workspace-dir", "workspace_dir", type=click.Path(), default=".")
@click.option(
    "--symlink/--no-symlink",
    "use_symlinks",
    default=True,
    help="Symlink bundled artifacts back to ZENITH_HOME instead of copying (default: enabled).",
)
@click.option(
    "--local/--no-local",
    "local",
    default=False,
    help="Store .zenith project data in the project workspace folder instead of the homedir.",
)
def init(
    agent: str | None,
    orchestrator_provider: str | None,
    worker_provider: str | None,
    worker_acp_command: str | None,
    validator_provider: str | None,
    validator_acp_command: str | None,
    terminal_reviewer_provider: str | None,
    terminal_reviewer_acp_command: str | None,
    zenith_home: str | None,
    workspace_dir: str,
    use_symlinks: bool,
    local: bool,
) -> None:
    """Initialize host-agent surface: MCP/codex config + provider agents + orchestrator prompt.

    The project bucket is created by `start_project` at the first MCP call from
    the host agent — `zenith init` stages the host-agent-facing files the agent
    loads at startup (provider configs, subagent definitions, orchestrator
    prompt, and the bundled skill surface under the provider's skill dirs). The
    workspace stays clean of `.zenith/` until `start_project` runs.
    """
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    config = HarnessConfig.discover()
    loader = AssetLoader(config)
    selection = _resolve_selection(
        agent=agent,
        orchestrator=orchestrator_provider,
        worker=worker_provider,
        worker_acp_command=worker_acp_command,
        validator=validator_provider,
        validator_acp_command=validator_acp_command,
        terminal_reviewer=terminal_reviewer_provider,
        terminal_reviewer_acp_command=terminal_reviewer_acp_command,
    )

    current_sha = _current_build_sha()
    prior_sha = _read_workspace_sha(workspace)
    if prior_sha is not None and prior_sha != current_sha:
        click.echo(
            f"Zenith build changed ({prior_sha} -> {current_sha}); replacing prior artifacts"
        )
        _phase_stale_zenith_artifacts(workspace, selection)

    # 1) MCP / Codex config
    storage_env = _storage_env(
        zenith_home=zenith_home, workspace=workspace, selection=selection, local=local
    )
    _write_bootstrap_config(workspace, selection, storage_env)

    # 2) Per-provider agents + orchestrator prompt
    for provider in selection.providers():
        _setup_provider_assets(workspace, loader, provider, use_symlinks=use_symlinks)

    # 3) Bundled skills installer (idempotent — only writes if missing)
    _install_bundled_skills(workspace, loader, use_symlinks=use_symlinks)

    _write_workspace_sha(workspace, current_sha)

    click.echo(
        f"\nInitialized v5 project workspace at {workspace}: "
        f"orchestrator={selection.orchestrator.name}, "
        f"worker={selection.worker.name}, "
        f"validator={selection.resolved_validation_worker.name}. "
        f"build={current_sha}"
    )
    click.echo(
        "Bucket lives at $ZENITH_HOME/projects/<pid>/ — created on the first "
        "`start_project(brief, workspace_dir)` call."
    )
    _echo_next_steps(selection.orchestrator)


# ---------------------------------------------------------------------------
# install-skills
# ---------------------------------------------------------------------------


@cli.command("install-skills")
@click.option("--target", type=click.Path(), required=True)
def install_skills_cmd(target: str) -> None:
    """Install bundled skills to a target directory (e.g. <ws>/.zenith/skills/)."""
    config = HarnessConfig.discover()
    loader = AssetLoader(config)
    _copy_skills(loader, Path(target).resolve())
    click.echo(f"Installed bundled skills to {target}")


# ---------------------------------------------------------------------------
# list-projects
# ---------------------------------------------------------------------------


@cli.command("list-projects")
def list_projects_cmd() -> None:
    """List all projects in HARNESS bucket."""
    store = ProjectStore(HarnessConfig.discover())
    projects = store.list_projects()
    if not projects:
        click.echo("No projects.")
        return
    for p in projects:
        click.echo(f"  {p.id}   ws={p.workspace_dir}   created={p.created_at}")


# ---------------------------------------------------------------------------
# show-project
# ---------------------------------------------------------------------------


@cli.command("show-project")
@click.argument("project_id")
def show_project_cmd(project_id: str) -> None:
    """Show envelope + compact task list for a project."""
    store = ProjectStore(HarnessConfig.discover())
    try:
        record = store.load_project(project_id)
    except FileNotFoundError:
        raise click.ClickException(f"Project not found: {project_id}")
    state = store.load_state(project_id)
    click.echo(f"id:        {record.id}")
    click.echo(f"workspace: {record.workspace_dir}")
    click.echo(f"created:   {record.created_at}")
    click.echo(f"state:     {state.state if state else 'draft'}")
    mid = record.current_mission_id
    if mid:
        click.echo(f"mission:   {mid}")
        try:
            tl = store.load_task_list(record.id, mid)
            ts = store.load_task_state(record.id, mid)
            rendered = render_task_list(tl, ts)
            if rendered:
                click.echo("")
                click.echo(rendered)
        except FileNotFoundError:
            click.echo("  (task list not yet submitted)")


# ---------------------------------------------------------------------------
# inspect-tasks
# ---------------------------------------------------------------------------


@cli.command("inspect-tasks")
@click.option("--project", "project_id", required=True)
@click.option("--mission", "mission_id", default=None)
def inspect_tasks_cmd(project_id: str, mission_id: str | None) -> None:
    """Render the compact text task list for a mission."""
    store = ProjectStore(HarnessConfig.discover())
    if mission_id is None:
        mid_list = store.list_missions(project_id)
        if not mid_list:
            raise click.ClickException("no missions in this project")
        mission_id = mid_list[-1]
    try:
        tl = store.load_task_list(project_id, mission_id)
    except FileNotFoundError:
        raise click.ClickException(f"tasks.json not found for {project_id}/{mission_id}")
    ts = store.load_task_state(project_id, mission_id)
    rendered = render_task_list(tl, ts, mode="full")
    if rendered:
        click.echo(rendered)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


@cli.command("metrics")
@click.argument("project_id")
def metrics_cmd(project_id: str) -> None:
    """Render the contract-landscape overview for a project.

    Emits head -n10-scannable blocks terminated by `$ ---`. Each mission
    gets a MISSION block (task counts, attempt stats, contract verdicts)
    followed by CONTRACT blocks grouped by verdict and TASK blocks
    grouped by status. Designed for an agent to scan with `head -n10`
    and reason about the known vs. incomplete knowledge and resources.
    """
    from .contractreifier import render_contract_head
    from .storage import ProjectStore

    store = ProjectStore(HarnessConfig.discover())
    try:
        # Get the mission's task-state.json path and render contract head
        # For now, render the first mission's task-state.json
        # TODO: extend to support multiple missions
        pass
    except FileNotFoundError:
        raise click.ClickException(f"Project not found: {project_id}")
    click.echo("not yet implemented")


# ---------------------------------------------------------------------------
# abort-project
# ---------------------------------------------------------------------------


@cli.command("abort-project")
@click.argument("project_id")
@click.option("--reason", required=True)
def abort_project_cmd(project_id: str, reason: str) -> None:
    """Mark a project Aborted (CLI-side: preserves tasks.json + attempts/)."""
    from .controller import ProjectController
    from .dispatcher import MockDispatcher, MockTerminalReviewer
    from .models import TerminalReviewHandoff, WorkHandoff

    config = HarnessConfig.discover()
    controller = ProjectController(
        config,
        MockDispatcher(lambda r: WorkHandoff(node_id=r.task.id, done=False, report="aborted")),
        MockTerminalReviewer(TerminalReviewHandoff(done=True, report="")),
    )
    env = controller.abort_project(project_id, reason)
    click.echo(f"Aborted {project_id}: state={env.state.state}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_skills(loader: AssetLoader, target: Path, *, use_symlinks: bool = True) -> None:
    target.mkdir(parents=True, exist_ok=True)
    bundled = loader.bundled_skills_dir()
    if not bundled.exists():
        click.echo(f"warning: bundled skills not found at {bundled}", err=True)
        return
    for skill_dir in iter_skill_directories(bundled):
        dest = target / skill_dir.name
        dest.mkdir(parents=True, exist_ok=True)
        src = skill_dir / "SKILL.md"
        dest_file = dest / "SKILL.md"
        if use_symlinks:
            if dest_file.is_symlink() and dest_file.resolve() == src.resolve():
                continue
            _atomic_symlink(src.resolve(), dest_file)
        else:
            shutil.copy2(src, dest_file)


def _install_bundled_skills(workspace: Path, loader: AssetLoader, *, use_symlinks: bool = True) -> int:
    """Install bundled skills into per-provider skill dirs.

    Mirrors `_setup_provider_assets` so `init` runs the skills installer even
    if no `--agent` was passed (we still need host agents to discover skills).
    Idempotent — skills are only written if their SKILL.md is missing.
    Returns the number of skill dirs populated.
    """
    installed = 0
    seen: set[Path] = set()
    for provider_name in provider_names_for_role("orchestrator"):
        try:
            provider = get_provider(provider_name)
        except (KeyError, ValueError):
            continue
        if not provider.agent_output_dir:
            continue
        for rel in provider.skill_dirs:
            dest = (workspace / rel).resolve()
            if dest in seen:
                continue
            seen.add(dest)
            if not dest.exists() or not any(dest.glob("*/SKILL.md")):
                _copy_skills(loader, dest, use_symlinks=use_symlinks)
                click.echo(f"Installed bundled skills to {dest}")
            installed += 1
    return installed


def _echo_next_steps(orchestrator: ProviderDefinition) -> None:
    prompt_path = orchestrator.orchestrator_prompt_output_path
    click.echo("")
    click.echo("Next:")
    click.echo("  1. Start your agent from the initialized project workspace:")
    click.echo(f"     {orchestrator.name}")
    if prompt_path:
        click.echo("  2. Ask it:")
        click.echo(
            f"     First read {prompt_path} and treat it as your primary role, then use Zenith to run this mission."
        )
        click.echo("")
        click.echo("     <your instruction or query>")


def _resolve_selection(
    *,
    agent: str | None,
    orchestrator: str | None,
    worker: str | None,
    worker_acp_command: str | None,
    validator: str | None,
    validator_acp_command: str | None,
    terminal_reviewer: str | None,
    terminal_reviewer_acp_command: str | None,
) -> ProviderSelection:
    if agent and orchestrator and agent != orchestrator:
        raise click.UsageError("--agent conflicts with --orchestrator-provider")
    orch = orchestrator or agent or "claude"
    wrk = worker or (agent if agent in provider_names_for_role("worker") else None) or default_worker_provider_name(orch)
    return ProviderSelection(
        orchestrator=get_provider(orch),
        worker=get_provider(wrk),
        validation_worker=get_provider(validator) if validator else None,
        worker_acp_command=worker_acp_command,
        validation_worker_acp_command=validator_acp_command,
    )


def _storage_env(
    *,
    zenith_home: str | None,
    workspace: Path,
    selection: ProviderSelection,
    local: bool = False,
) -> dict[str, str]:
    env: dict[str, str] = {}
    if zenith_home:
        env["ZENITH_HOME"] = str(Path(zenith_home).expanduser().resolve())
    if local:
        env["ZENITH_LOCAL"] = "true"
    return env


def _forwarded_mcp_env() -> dict[str, str]:
    return {
        key: value
        for key in MCP_ENV_FORWARD_ALLOWLIST
        if (value := os.environ.get(key))
    }


def _zenith_project_root() -> Path:
    """Return the source checkout that owns the Zenith runtime uv project."""
    start = Path(__file__).resolve()
    for candidate in (start.parent, *start.parents):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.exists():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if 'name = "zenith-harness"' in text:
            return candidate
    raise click.ClickException(
        "Could not locate the Zenith uv project root. Run `zenith init` from a "
        "Zenith source checkout with pyproject.toml available."
    )


def _mcp_server_args() -> list[str]:
    return [
        "run",
        "--project",
        str(_zenith_project_root()),
        "zenith-server",
        "--mode",
        "orchestrator",
    ]


def _write_bootstrap_config(
    workspace: Path,
    selection: ProviderSelection,
    storage_env: dict[str, str],
) -> None:
    fmt = selection.orchestrator.config_format
    env = {**selection.env(), **storage_env}
    server_args = _mcp_server_args()
    if fmt == "mcp_json":
        env = {**env, **_forwarded_mcp_env()}
        path = workspace / ".mcp.json"
        existing = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )
        existing.setdefault("mcpServers", {})["zenith"] = {
            "type": "stdio",
            "command": "uv",
            "args": server_args,
            "env": env,
        }
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        click.echo(f"Wrote {path}")
    elif fmt == "codex_config":
        config_path = workspace / ".codex" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        env_lines = "\n".join(f'{k} = "{v}"' for k, v in env.items())
        block = (
            'model = "gpt-5.5"\n'
            'sandbox_mode = "danger-full-access"\n'
            'model_reasoning_effort = "xhigh"\n'
            '[features]\n'
            'memories = true\n'
            "# BEGIN zenith\n"
            "[mcp_servers.zenith]\n"
            'command = "uv"\n'
            f"args = {json.dumps(server_args)}\n"
            "startup_timeout_sec = 10\n"
            "tool_timeout_sec = 1000000\n"
            "\n"
            "[mcp_servers.zenith.env]\n"
            f"{env_lines}\n"
            "# END zenith\n"
        )
        _replace_managed_block(config_path, "# BEGIN zenith", "# END zenith", block)
        click.echo(f"Wrote {config_path}")
    else:
        raise ValueError(f"unsupported config_format: {fmt}")


def _replace_managed_block(path: Path, start: str, end: str, block: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if start in existing or end in existing:
        if start in existing and end in existing:
            before, _, tail = existing.partition(start)
            _, _, after = tail.partition(end)
        elif start in existing:
            before, _, _ = existing.partition(start)
            after = ""
        else:
            _, _, after = existing.partition(end)
            before = ""
        updated = before.rstrip()
        if updated:
            updated += "\n\n"
        updated += block.rstrip() + "\n"
        if after.strip():
            updated += "\n" + after.lstrip("\n")
    else:
        updated = existing.rstrip()
        if updated:
            updated += "\n\n"
        updated += block.rstrip() + "\n"
    path.write_text(updated, encoding="utf-8")


def _setup_provider_assets(
    workspace: Path,
    loader: AssetLoader,
    provider: ProviderDefinition,
    *,
    use_symlinks: bool = True,
) -> None:
    if provider.agent_output_dir:
        agents_dir = workspace / provider.agent_output_dir
        _copy_provider_agents(
            loader, agents_dir, provider.name, use_symlinks=use_symlinks
        )
        click.echo(f"Installed {provider.name} subagents to {agents_dir}")
    # Install bundled skills into the host-agent skill surface so the
    # orchestrator can discover playbooks/skills at startup — `start_project`
    # runs only after the host agent is already up, so the surface must exist
    # before the first MCP call. `start_project` later merges bucket skills
    # (including project-authored ones) into these dirs.
    for rel in provider.skill_dirs:
        dest = workspace / rel
        _copy_skills(loader, dest, use_symlinks=use_symlinks)
        click.echo(f"Installed bundled skills to {dest}")
    if provider.orchestrator_prompt_output_path:
        path = workspace / provider.orchestrator_prompt_output_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            body = loader.load_prompt_file("orchestrator", "system_prompt.md")
            path.write_text(body, encoding="utf-8")
            click.echo(f"Created {path}")


def _copy_provider_agents(loader: AssetLoader, target: Path, provider_name: str, *, use_symlinks: bool = True) -> None:
    bundled = loader.bundled_agents_dir(provider_name)
    if not bundled.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for agent_file in sorted(bundled.glob("*")):
        if agent_file.is_file():
            dest = target / agent_file.name
            if use_symlinks:
                if dest.is_symlink() and dest.resolve() == agent_file.resolve():
                    continue
                _atomic_symlink(agent_file.resolve(), dest)
            else:
                shutil.copy2(agent_file, dest)


def _atomic_symlink(source: Path, dest: Path) -> None:
    """Atomically replace *dest* with a symlink pointing at *source*.

    Creates a temp symlink in dest's parent dir, then renames it over dest.
    """
    import tempfile

    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=".tmp_")
    os.close(fd)
    os.unlink(tmp_path)
    os.symlink(source, tmp_path)
    os.rename(tmp_path, str(dest))
