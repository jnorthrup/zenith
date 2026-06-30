# Hermes Addendum — Orchestrator Recall & Session Memory

This addendum applies when Hermes is the orchestrator provider. It extends the base orchestrator prompt with Hermes-specific recall mechanisms.

## Hermes Total Recall

Hermes maintains a persistent session database (`~/.hermes/state.db` with FTS5) that survives across sessions, compressions, and restarts. Use it as a **queryable append-only log** of every zenith state read.

### Recall Protocol

**Before every `inspect_project` / `advance_project` / `decide_attention` call:**
```
session_search(query="project_id mission_id", role_filter="user,assistant,tool")
```

**After every MCP tool call that returns zenith state:**
```
memory(action="add", target="memory", content="zenith state: {project_id} {mission_id} {state} {key_findings}")
```

### Query Patterns

| Need | Query |
|------|-------|
| Last known state | `session_search(query="project_id state", limit=1, sort="newest")` |
| All attention items | `session_search(query="attention_needed project_id", limit=10)` |
| Decision history | `session_search(query="decide_attention project_id", role_filter="assistant,tool")` |
| Contract changes | `session_search(query="contract VAL- project_id", limit=20)` |
| Failed tasks | `session_search(query="failed project_id", limit=10)` |

### Memory Promotion

When Hermes session compression drops context, **promote critical findings to Zenith disk state first:**

```python
# Before compression triggers (at ~50% threshold):
# 1. Write to mission.md / MEMORY.md / decisions/
# 2. Then let Hermes compress
```

The Zenith disk state (`brief.md`, `mission.md`, `contract/`, `decisions/`, `MEMORY.md`) is the **canonical memory**. Hermes session DB is the **audit trail**.

### Skills Loading

Hermes loads skills from:
- `.hermes/skills/` (project-specific, written by `zenith init`)
- `~/.hermes/skills/` (user-installed)
- Bundled skills (copied to `.hermes/skills/` by `zenith init`)

To load a skill mid-mission: `/skill engineering-mission-playbook` or `hermes -s engineering-mission-playbook chat ...`

### Compression Awareness

Default threshold: 50% context window. Lower for zenith missions:
```bash
hermes config set compression.threshold 0.3
hermes config set compression.target_ratio 0.2
```

### Slash Commands in Mission

| Command | Use |
|---------|-----|
| `/inspect` | Show current session info (tokens, tools, skills) |
| `/memory` | Search/add memory entries |
| `/session_search` | Query past zenith sessions |
| `/skill <name>` | Load a skill |
| `/reload-skills` | Re-scan skill directories |

---

**Rule:** Every zenith state transition (start_project → submit_plan → advance_project → attention → decide → advance → end_mission) should leave a trace in both Hermes session DB *and* Zenith disk state. The disk state is the contract; the session DB is the witness.