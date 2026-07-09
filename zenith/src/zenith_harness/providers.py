from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderName = str
ConfigFormat = Literal["mcp_json", "codex_config"]



@dataclass(frozen=True)
class ProviderDefinition:
    name: ProviderName
    skill_dirs: tuple[str, ...] = ()
    skill_alias_dirs: tuple[str, ...] = ()
    config_format: ConfigFormat = "mcp_json"
    default_worker_acp_command: str | None = None
    agent_output_dir: str | None = None
    orchestrator_prompt_output_path: str | None = None
    acp_supports_system_prompt: bool = True
    acp_runtime_mode: str | None = None

    def effort_flags(self, effort: str | None) -> str:
        """Return provider-specific CLI flags for reasoning effort.

        Returns empty string if the provider does not support it or is ambivalent.
        """
        if not effort:
            return ""
        if self.name == "codex":
            return f' -c model_reasoning_effort="{effort}"'
        return ""


@dataclass(frozen=True)
class ProviderSelection:
    orchestrator: ProviderDefinition
    worker: ProviderDefinition
    validation_worker: ProviderDefinition | None = None
    worker_acp_command: str | None = None
    validation_worker_acp_command: str | None = None

    @property
    def resolved_worker_acp_command(self) -> str | None:
        return self.worker_acp_command or self.worker.default_worker_acp_command

    @property
    def resolved_validation_worker(self) -> ProviderDefinition:
        return self.validation_worker or self.worker

    @property
    def resolved_validation_worker_acp_command(self) -> str | None:
        if self.validation_worker_acp_command:
            return self.validation_worker_acp_command
        if self.resolved_validation_worker.name != self.worker.name:
            return (
                self.resolved_validation_worker.default_worker_acp_command
                or self.resolved_worker_acp_command
            )
        return (
            self.resolved_worker_acp_command
            or self.resolved_validation_worker.default_worker_acp_command
        )

    def env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "ZENITH_ORCHESTRATOR_PROVIDER": self.orchestrator.name,
            "ZENITH_WORKER_PROVIDER": self.worker.name,
        }
        if self.resolved_worker_acp_command:
            env["ZENITH_WORKER_ACP_COMMAND"] = self.resolved_worker_acp_command
        if self.resolved_validation_worker.name != self.worker.name:
            env["ZENITH_VALIDATOR_PROVIDER"] = self.resolved_validation_worker.name
        validation_command = self.resolved_validation_worker_acp_command
        if (
            validation_command != self.resolved_worker_acp_command
            and validation_command is not None
        ):
            env["ZENITH_VALIDATOR_ACP_COMMAND"] = validation_command
        return env

    def skill_install_dirs(self) -> tuple[str, ...]:
        return _dedupe_paths(
            self.orchestrator.skill_dirs
            + self.worker.skill_dirs
            + self.resolved_validation_worker.skill_dirs
        )

    def skill_alias_dirs(self) -> tuple[str, ...]:
        return _dedupe_paths(
            self.orchestrator.skill_alias_dirs
            + self.worker.skill_alias_dirs
            + self.resolved_validation_worker.skill_alias_dirs
        )

    def providers(self) -> tuple[ProviderDefinition, ...]:
        providers = (
            self.orchestrator,
            self.worker,
            self.resolved_validation_worker,
        )
        ordered: list[ProviderDefinition] = []
        seen: set[str] = set()
        for provider in providers:
            if provider.name in seen:
                continue
            seen.add(provider.name)
            ordered.append(provider)
        return tuple(ordered)


def _dedupe_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return tuple(ordered)


PROVIDERS: dict[ProviderName, ProviderDefinition] = {
    "claude": ProviderDefinition(
        name="claude",
        skill_dirs=(".claude/skills", ".agents/skills"),
        skill_alias_dirs=(".claude/skills", ".agents/skills"),
        config_format="mcp_json",
        default_worker_acp_command="claude-agent-acp",
        agent_output_dir=".claude/agents",
        orchestrator_prompt_output_path=".claude/orchestrator_prompt.md",
        acp_runtime_mode="bypassPermissions",
    ),
    "codex": ProviderDefinition(
        name="codex",
        skill_dirs=(".codex/skills", ".agents/skills"),
        skill_alias_dirs=(".codex/skills", ".agents/skills"),
        config_format="codex_config",
        default_worker_acp_command="codex-acp",
        agent_output_dir=".codex/agents",
        orchestrator_prompt_output_path=".codex/orchestrator_prompt.md",
        acp_supports_system_prompt=False,
    ),
    "hermes": ProviderDefinition(
        name="hermes",
        skill_dirs=(".hermes/skills", ".agents/skills"),
        skill_alias_dirs=(".hermes/skills", ".agents/skills"),
        config_format="mcp_json",
        default_worker_acp_command="hermes acp",
        agent_output_dir=".hermes/agents",
        orchestrator_prompt_output_path=".hermes/orchestrator_prompt.md",
        acp_supports_system_prompt=True,
        acp_runtime_mode=None,
    ),
    "jules": ProviderDefinition(
        name="jules",
        skill_dirs=(".jules/skills", ".agents/skills"),
        skill_alias_dirs=(".jules/skills", ".agents/skills"),
        config_format="mcp_json",
        default_worker_acp_command="jules-acp-bridge",
        agent_output_dir=".jules/agents",
        orchestrator_prompt_output_path=".jules/orchestrator_prompt.md",
        acp_supports_system_prompt=True,
        acp_runtime_mode=None,
    ),
}

ORCHESTRATOR_PROVIDER_NAMES: tuple[str, ...] = tuple(PROVIDERS.keys())
WORKER_PROVIDER_NAMES: tuple[str, ...] = tuple(PROVIDERS.keys())



def get_provider(name: str) -> ProviderDefinition:
    try:
        return PROVIDERS[name]  # type: ignore[index]
    except KeyError as exc:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown provider '{name}'. Supported providers: {supported}") from exc


def provider_names_for_role(role: Literal["orchestrator", "worker"]) -> tuple[str, ...]:
    if role == "orchestrator":
        return ORCHESTRATOR_PROVIDER_NAMES
    return WORKER_PROVIDER_NAMES


def default_worker_provider_name(orchestrator_provider_name: str) -> str:
    if orchestrator_provider_name in PROVIDERS:
        return orchestrator_provider_name
    return "claude"


def default_validator_provider_name(worker_provider_name: str | None, explicit_worker: bool) -> str:
    """Default validator: use local provider to avoid Jules validation failures.
    
    When worker is explicitly set to a remote provider (jules), default validator
    to local execution so read-only validation can call end_node() directly.
    """
    if explicit_worker and worker_provider_name != "claude":
        # Worker is remote (jules) - use local validator to avoid remote validation slop
        return "claude"
    return "claude"
