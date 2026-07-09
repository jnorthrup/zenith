# Zenith Harness Architecture & Integration Specification

This document specifies the technical design, protocols, and implementation standards for the Zenith Harness codebase, focusing on its non-Anthropic enhancements, Jules integration, OAuth mechanics, and NARS contract representation.

---

## 1. Zenith Harness Architecture Overview

The Zenith Harness is an autonomous workspace orchestration agent designed to run multi-agent workflows. It coordinates planning, implementation, and quality assurance through role-specialized agents.

### 1.1 Core Agent Roles
1. **Orchestrator**: Maintains the high-level task topology, reads charter specifications (`mission.md`), generates atomic, falsifiable contracts under `contract/<ID>.md`, and manages attention transitions.
2. **Worker**: Performs direct codebase modifications, runs tests, self-verifies progress, and logs changes to `MEMORY.md`.
3. **Validator**: Independently validates the worker's changes against the assertions in the contract using objective verification commands, yielding success/failure verdicts.
4. **Terminal Reviewer**: Conducts a final black-box evaluation of the user's initial requirements against the state of the workspace, completely isolated from intermediate files (such as `.zenith/` and contract folders).

---

## 2. Jules Bijective Integration

Jules acts as the parallel, remote-capable executor for task implementations in Zenith. When Jules is enabled, the worker task triggers a bijective (parallel) execution model.

```
                  +---------------------------+
                  |  Worker Node Execution    |
                  +-------------+-------------+
                                |
                +---------------+---------------+
                |                               |
        (Bijective Path)               (Local LLM Path)
                v                               v
    +-----------------------+       +-----------------------+
    | Jules OAuth check     |       |                       |
    |  `TokenManager`       |       |                       |
    +-----------+-----------+       |                       |
                |                   |                       |
                v                   |                       |
    +-----------------------+       |                       |
    | Spawns Jules Remote   |       | Spawns local worker   |
    |  and Local LLM agent  |       | with lazy NARS        |
    |  in parallel          |       | contract injection    |
    +-----------+-----------+       +-----------+-----------+
                |                               |
                v                               v
    +-----------------------+                   |
    | Polls Jules terminal  |                   |
    | state & pulls PR URL  |                   |
    +-----------+-----------+                   |
                |                               |
                v                               v
    +-----------------------+                   |
    | Promotes contracts to |                   |
    | NARS head-n10 format  |                   |
    +-----------+-----------+                   |
                |                               |
                +---------------+---------------+
                                |
                                v
                    +-----------------------+
                    | Task Completion &     |
                    | Work Log Update       |
                    +-----------------------+
```

### 2.1 Parallel Execution Lifecycle
- Spawns both the Jules remote worker (via the Jules CLI/API) and the local LLM agent (e.g. Claude) in parallel.
- Polls Jules for status updates using the `jules remote status <session_id>` CLI subcommand or the REST GET task endpoint.
- Once a terminal state is reached, retrieves the pull request URL using `jules remote pull --session <session_id>`.
- Merges the reports and determines overall task completion.

---

## 3. Jules OAuth Authentication Flow

Authentication with the Jules API is managed dynamically by the `TokenManager` subsystem in `src/zenith_harness/jules_acp_bridge.py`.

### 3.1 Authentication Precedence
1. **Explicit API Key**: If the `JULES_API_KEY` environment variable is non-empty, it is used directly as the auth credential for REST HTTP requests.
2. **OAuth Fallback**: If no API key is present:
   - It checks for the `jules` and `gcloud` binaries on the system path.
   - It executes `jules login --no-launch-browser` to refresh credentials without interactive prompt blockages.
   - It executes `gcloud auth print-access-token` to retrieve the current Bearer token.
   - Inject the retrieved token into HTTP request headers: `Authorization: Bearer <token>`.

### 3.2 Token Refresh Mechanics
If a REST request returns a `401 Unauthorized` status code, the `TokenManager` forces a refresh using `gcloud auth print-access-token` and retries the HTTP request once. Subsequent failures or 5xx server issues trigger an exponential backoff retry flow up to a configurable maximum of 3 retries.

---

## 4. NARS Contract Representation

Contracts in Zenith are structured as markdown documents (`contract/<ID>.md`). To make these files scannable and readable by external parsers, they are augmented with Pei Wang's Non-Axiomatic Reasoning System (NARS) statement logic.

### 4.1 Specification of head -n10 Summarization
A valid Zenith contract JSON representation must adhere to a strict newline structure designed for scanning with the Unix utility `head -n10` or `ghead -n10`:
- **Line 1**: `{ "id": "...", "nars": [`
- **Lines 2-10**: Individual NARS logic statements (e.g., `"<A --> B>"` representing inheritance, or `<A ==> B>` representing implication) formatted as valid JSON strings with optional trailing commas. If there are fewer than 9 NARS terms, lines 4–10 are padded with empty strings to guarantee line alignment.
- **Line 11+**: Closing array bracket `],` followed by the remainder of the JSON fields (such as `title`, `description`, etc.) and the final closing brace `}`.

### 4.2 Code Hook Logic
- **Lazy Conversion (Non-Jules)**: When the local LLM agent is dispatched, `_build_template_vars` invokes `ensure_contract_nars` to look up the `.json` contract metadata, generate the `## NARS` header, and rewrite the `.md` file to append NARS statements.
- **Sync Promotion (Jules)**: Upon Jules completion, `promote_nars_to_jules_landscape` reads the contract targets and writes the NARS terms in the strict 10-line format directly into the Jules landscape directory (`.zenith/jules_contracts/`).
