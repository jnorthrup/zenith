# Zenith

Zenith is a small MCP/ACP harness for running a coding agent as a multi-agent
orchestrator.

## Quick Run

Requirements:

- Python 3.11+
- `uv`
- Claude Code or Codex

Install from this folder:

```bash
uv sync
uv run zenith --help
```

Initialize a workspace:

```bash
# Claude Code
uv run zenith init --agent claude

# Or Codex
uv run zenith init --agent codex
```

Start your agent from the initialized workspace:

```bash
claude
# or
codex
```

Then ask the agent to read the generated orchestrator prompt:

```text
Read .claude/orchestrator_prompt.md and use Zenith to run this mission.

<your instruction or query>
```

For Codex, use:

```text
Read .codex/orchestrator_prompt.md and use Zenith to run this mission.

<your instruction or query>
```

## Development

```bash
uv run pytest
```
