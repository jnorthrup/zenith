"""v5 MCP server. 3 modes: orchestrator / worker / terminal-reviewer.

See docs/v5/08-mcp-surface.md. Tool-surface isolation is structural:
each mode registers a disjoint tool set on its own MCP server.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field, ValidationError as PydanticValidationError

from .config import HarnessConfig
from .controller import ProjectController, ToolError
from .dispatcher import NodeDispatcher, TerminalReviewer
from .models import (
    Decision,
    TaskList,
    TerminalReviewHandoff,
    ValidateHandoff,
    ValidationItem,
    WorkHandoff,
)
from .jules_acp_bridge import atomic_write_json

logger = logging.getLogger(__name__)

# Voting/consensus tuning. Per project policy: S/N ratio 3 or 5 for voting,
# 4 is optimal for non-voting consensus. Cap any fan-out batch at 5 so a
# tie never deadlocks a vote (5→majority 3, 3→unanimous). Recency gate:
# a Jules session is eligible for cross-session interaction only if it has
# reached terminal state AND delivered (PR or branch). Quota window bounds
# how long an already-closed session remains "recent" for the orchestrator
# to cite or vote against.
JULES_VOTING_MAX_FANOUT = int(os.environ.get("JULES_VOTING_MAX_FANOUT", "5"))
JULES_VOTING_OPTIMAL = int(os.environ.get("JULES_VOTING_OPTIMAL", "4"))
JULES_RECENCY_HOURS = float(os.environ.get("JULES_RECENCY_HOURS", "24"))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_orchestrator_server(
    config: HarnessConfig,
    controller: ProjectController | None = None,
) -> FastMCP:
    """7 orchestrator tools, registered on a stdio MCP server."""
    if controller is None:
        from .dispatcher import MockDispatcher, MockTerminalReviewer

        controller = ProjectController(
            config,
            MockDispatcher(
                lambda r: WorkHandoff(node_id=r.task.id, done=False, report="no ACP wired")
            ),
            MockTerminalReviewer(TerminalReviewHandoff(done=True, report="")),
        )

    mcp = FastMCP(
        name="zenith",
        instructions=(
            "Mission orchestration harness. Mode: orchestrator. "
            "14 tools: start_project, submit_plan, advance_project, "
            "end_mission, decide_attention, inspect_project, abort_project, "
            "jules_ensure_auth, jules_launch_task, jules_dispatch_batch, "
            "jules_poll_batch, jules_converse, jules_bijective_sync, "
            "jules_list_sessions. "
            "Lifecycle: plan with submit_plan, run with advance_project, "
            "request closure with end_mission, resolve attention with decide_attention, "
            "then call advance_project again."
        ),
    )
    _register_orchestrator_tools(mcp, controller)
    return mcp


def create_worker_server() -> FastMCP:
    """1 worker tool. Configured at runtime via env: ZENITH_NODE_TYPE,
    ZENITH_NODE_ID, ZENITH_HANDOFF_PATH.
    """
    mcp = FastMCP(
        name="zenith-worker",
        instructions=(
            "Worker MCP server. Mode: worker. 1 tool: end_node. "
            "Call exactly once before exiting."
        ),
    )
    _register_worker_tools(mcp)
    return mcp


def create_terminal_reviewer_server() -> FastMCP:
    """1 reviewer tool. Configured via env: ZENITH_TERMINAL_REVIEW_PATH."""
    mcp = FastMCP(
        name="zenith-terminal-reviewer",
        instructions=(
            "Runtime closure-check MCP server. 1 tool: submit_terminal_review. "
            "Call exactly once with the structured gap list."
        ),
    )
    _register_terminal_reviewer_tools(mcp)
    return mcp


# ---------------------------------------------------------------------------
# Orchestrator tools
# ---------------------------------------------------------------------------


def _register_orchestrator_tools(mcp: FastMCP, controller: ProjectController) -> None:
    # Per-project lock around mutating controller calls. The thread hop in each
    # tool prevents event-loop blocking, but two same-project tool calls could
    # otherwise race on disk state (attention, attempts, task-state, tasks).
    # docs/v5/07-runtime-architecture.md §9 declares concurrent same-project
    # operations undefined behavior; this lock serializes them defensively
    # without requiring host coordination. `inspect_project` is read-only and
    # stays uncontended.
    project_locks: dict[str, asyncio.Lock] = {}
    locks_guard = asyncio.Lock()

    async def _project_lock(project_id: str) -> asyncio.Lock:
        async with locks_guard:
            lock = project_locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                project_locks[project_id] = lock
            return lock

    @mcp.tool(
        name="start_project",
        description=(
            "Create a new long-running project rooted at the workspace. "
            "Use when the user describes a goal that needs planning and decomposition. "
            "Writes brief.md, creates harness state, returns the envelope with "
            "state=mission_planning."
        ),
    )
    async def start_project(
        brief: Annotated[str, Field(description="The user's ask in prose; goes to brief.md")],
        workspace_dir: Annotated[
            str, Field(description="Absolute path to the user's workspace.")
        ],
    ) -> dict[str, Any]:
        # No per-project lock: project_id does not exist until the call returns.
        try:
            return _to_payload(
                await asyncio.to_thread(controller.start_project, brief, workspace_dir)
            )
        except ToolError as exc:
            return _to_payload(exc)

    @mcp.tool(
        name="submit_plan",
        description=(
            "Submit the current mission's contract-backed task list. Before calling, "
            "write every targeted contract/<id>.md file under the mission contract "
            "directory. Runtime validates a non-empty contract, task shape, "
            "depends_on resolution, acyclicity, coverage (each assertion has exactly "
            "one non-superseded work task). On success state becomes "
            "mission_running; call advance_project to dispatch work."
        ),
    )
    async def submit_plan(
        project_id: Annotated[str, Field(description="Project id from start_project.")],
        task_list: Annotated[
            TaskList,
            Field(
                description=(
                    "Mission task list (tasks: list[Task] with depends_on)."
                )
            ),
        ],
    ) -> dict[str, Any]:
        async with await _project_lock(project_id):
            try:
                return _to_payload(
                    await asyncio.to_thread(controller.submit_plan, project_id, task_list)
                )
            except ToolError as exc:
                return _to_payload(exc)

    @mcp.tool(
        name="advance_project",
        description=(
            "Drive the runtime forward. BLOCKING — may run for many minutes while "
            "workers dispatch according to runtime scheduling. "
            "Call whenever state is mission_running. "
            "Returns when attention is needed, no runnable task work remains, or "
            "`max_steps` exhausts. It does not request mission closure; call "
            "end_mission when you intend to close after task work is quiescent. "
            "If it returns still mission_running with runnable work, call it again."
        ),
    )
    async def advance_project(
        project_id: Annotated[str, Field(description="Project id.")],
        max_steps: Annotated[
            int | None, Field(default=None, description="Optional cap on step() calls.")
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        async with await _project_lock(project_id):
            try:
                return _to_payload(
                    await asyncio.to_thread(controller.advance_project, project_id, max_steps)
                )
            except ToolError as exc:
                return _to_payload(exc)

    @mcp.tool(
        name="end_mission",
        description=(
            "Request runtime mission closure. Call only when state is mission_running "
            "and you believe task work is complete/quiescent. This tool does not "
            "dispatch workers. If work or gates are still runnable, returns "
            "mission_not_ready_to_close; call advance_project first. If closure "
            "passes, state becomes done. If closure finds gaps, state becomes "
            "attention_needed with a closure report."
        ),
    )
    async def end_mission(
        project_id: Annotated[str, Field(description="Project id.")],
    ) -> dict[str, Any]:
        async with await _project_lock(project_id):
            try:
                return _to_payload(
                    await asyncio.to_thread(controller.end_mission, project_id)
                )
            except ToolError as exc:
                return _to_payload(exc)

    @mcp.tool(
        name="decide_attention",
        description=(
            "Resolve all open AttentionItem(s). Every open item must be covered by "
            "exactly one Decision. Use retry only for transient node_failed attempts; "
            "use patch for changed work, missing assertions, failed validation, "
            "over-broad scope, or task-list adaptation. Patches must pass structural "
            "validation. After a valid decide, state usually returns to "
            "mission_running; call advance_project to dispatch more work."
        ),
    )
    async def decide_attention(
        project_id: Annotated[str, Field(description="Project id.")],
        decisions: Annotated[
            list[Decision], Field(description="One Decision per open attention item.")
        ],
    ) -> dict[str, Any]:
        async with await _project_lock(project_id):
            try:
                return _to_payload(
                    await asyncio.to_thread(controller.decide_attention, project_id, decisions)
                )
            except ToolError as exc:
                return _to_payload(exc)
            except PydanticValidationError as exc:
                # FastMCP boundary: convert Pydantic error to ToolError with repair prescriptions.
                # Maps Pydantic error locations to actionable fix codes.
                repair_codes = _pydantic_validation_repair_codes(exc)
                return {
                    "error": "decide_attention_validation_failed",
                    "message": str(exc),
                    "details": repair_codes,
                }

    @staticmethod
    def _pydantic_validation_repair_codes(exc: PydanticValidationError) -> list[str]:
        """Map Pydantic error locations to fix prescriptions.

        Each returned string is an actionable code + instruction that tells the
        caller exactly what to fix in the Decision dict they submitted.
        """
        codes = []
        for err in exc.errors():
            loc = ".".join(str(l) for l in err["loc"])
            msg = err["msg"]
            inp = err.get("input", "")
            code = err.get("code", "unknown")

            # Gate body must be empty string
            if "body" in loc and "gate" in loc and msg.startswith("String should have at most"):
                codes.append(
                    f"fix: set gate body to empty string '' (gates cannot have body content)"
                )
                continue

            # Gate skill must be None
            if "skill" in loc and "gate" in loc and "required" in msg.lower():
                codes.append(
                    f"fix: set skill=null for gates (gates cannot have a skill)"
                )
                continue

            # Work/validate tasks need non-empty body
            if "body" in loc and msg.startswith("String should have at least"):
                task_type = "work" if "work" in loc else "validate"
                codes.append(
                    f"fix: {task_type} task needs non-empty body string "
                    f"(min length 1, found '{inp}' for field '{loc}')"
                )
                continue

            # Work/validate tasks need skill
            if "skill" in loc and ("required" in msg.lower() or msg.startswith("Field required")):
                task_type = "work" if "work" in loc else "validate"
                codes.append(
                    f"fix: {task_type} task needs a skill field "
                    f"(e.g. skill='engineering-mission-playbook', found '{inp}' for '{loc}')"
                )
                continue

            # decisions list item — Decision object validation
            if loc.startswith("decisions"):
                # Try to give item-level guidance
                item_idx = None
                for i, l in enumerate(err["loc"]):
                    if isinstance(l, int) and l >= 0:
                        item_idx = l
                        break
                if item_idx is not None:
                    if "item_id" in loc:
                        codes.append(
                            f"fix[decision[{item_idx}]]: item_id is required "
                            f"(found '{inp}' at '{loc}')"
                        )
                    elif "action" in loc:
                        codes.append(
                            f"fix[decision[{item_idx}]]: action must be one of: "
                            f"continue | patch | retry | next_mission | abort "
                            f"(found '{inp}' at '{loc}')"
                        )
                    elif "patch" in loc:
                        if msg.startswith("Field required"):
                            codes.append(
                                f"fix[decision[{item_idx}]]: patch is required when action=patch "
                                f"(omit patch field when action=continue/retry/next_mission/abort)"
                            )
                        else:
                            codes.append(
                                f"fix[decision[{item_idx}].patch]: {msg} "
                                f"(input={inp}, loc={loc})"
                            )
                    elif "justification" in loc:
                        codes.append(
                            f"fix[decision[{item_idx}].justification]: {msg} "
                            f"(found '{inp}', set to non-empty string)"
                        )
                    else:
                        codes.append(
                            f"fix[decision[{item_idx}].{loc.replace('decisions.','')}]: "
                            f"{msg} (input={inp})"
                        )
                else:
                    codes.append(f"fix: {msg} at '{loc}' (input={inp}, code={code})")
                continue

            # Fallback
            codes.append(f"fix: {msg} at '{loc}' (input={inp}, code={code})")

        return codes if codes else [f"fix: Pydantic validation failed: {exc}"]

    @mcp.tool(
        name="inspect_project",
        description=(
            "Pure read of current state, full task-list view, and open attention. "
            "No state change. Use when waking with no specific tool call in mind."
        ),
    )
    async def inspect_project(
        project_id: Annotated[str, Field(description="Project id.")],
    ) -> dict[str, Any]:
        try:
            return _to_payload(
                await asyncio.to_thread(controller.inspect_project, project_id)
            )
        except ToolError as exc:
            return _to_payload(exc)

    @mcp.tool(
        name="abort_project",
        description=(
            "Cancel the current mission and project. Marks state=Aborted with the "
            "supplied reason. Preserves tasks.json + attempts/ + decisions/ for forensics."
        ),
    )
    async def abort_project(
        project_id: Annotated[str, Field(description="Project id.")],
        reason: Annotated[str, Field(description="Why we are aborting.")],
    ) -> dict[str, Any]:
        async with await _project_lock(project_id):
            try:
                return _to_payload(
                    await asyncio.to_thread(controller.abort_project, project_id, reason)
                )
            except ToolError as exc:
                return _to_payload(exc)

    @mcp.tool(
        name="jules_ensure_auth",
        description=(
            "Ensure Jules is authenticated via OAuth. Triggers browser login flow if needed. "
            "NOT GREEDY: Call this explicitly before first Jules operation if auth status is unknown. "
            "After calling, verify authenticated status in returned state."
        ),
    )
    async def jules_ensure_auth() -> dict[str, Any]:
        try:
            from .jules_acp_bridge import ensure_jules_authenticated
            authenticated = ensure_jules_authenticated()
            return {
                "authenticated": authenticated,
                "message": "Jules authenticated" if authenticated else "Jules login required - check browser for OAuth flow",
            }
        except Exception as exc:
            return {"error": "jules_auth_failed", "message": str(exc)}

    @mcp.tool(
        name="jules_launch_task",
        description=(
            "Create a Jules remote session for a Zenith task and return immediately. "
            "Zenith owns the in-repo mailbox under .zenith/mailbox, so Hermes does not "
            "have to hold a long-running Jules conversation. Circle back with "
            "jules_bijective_sync and jules_converse."
        ),
    )
    async def jules_launch_task(
        project_id: Annotated[str, Field(description="Project id.")],
        prompt: Annotated[str, Field(description="Imperative Jules task prompt. Must ask for a PR or branch-producing change.")],
        task_id: Annotated[str | None, Field(description="Optional Zenith task id to bind to the Jules session.")] = None,
        mission_id: Annotated[str | None, Field(description="Optional Zenith mission id; defaults to current mission when running.")] = None,
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            if mission_id is None:
                from .models import MissionRunning
                proj_state = controller.store.load_state(project_id)
                mission_id = proj_state.mission_id if isinstance(proj_state, MissionRunning) else "mcp"
            if task_id is None:
                import time
                task_id = f"jules-{int(time.time())}"
            from .jules_acp_bridge import launch_jules_bijective, load_session_store
            remote_id, state = await launch_jules_bijective(
                prompt_text=prompt,
                cwd=cwd,
                task_id=task_id,
                project_id=project_id,
                mission_id=mission_id,
            )
            session = load_session_store(cwd).get(remote_id, {})
            # Get live quota metrics
            from .jules_acp_bridge import check_jules_quota
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                active, headroom = loop.run_until_complete(check_jules_quota(cwd))
            except Exception:
                active, headroom = 0, 4
            finally:
                loop.close()
            # Get rolling 24h quota
            daily_quota = controller.store.config.jules_quota_per_24h
            dispatches_24h = controller.store.jules_dispatches_in_last_24h(project_id)
            return {
                "remote_id": remote_id,
                "task_id": task_id,
                "mission_id": mission_id,
                "status": state.status,
                "is_terminal": state.is_terminal,
                "succeeded": state.succeeded,
                "mailbox_path": session.get("mailbox_path"),
                "repo_root": session.get("repo_root"),
                "quota": {
                    "active_sessions": active,
                    "concurrency_headroom": headroom,
                    "dispatches_last_24h": dispatches_24h,
                    "rolling_headroom": max(0, daily_quota - dispatches_24h),
                    "daily_quota": daily_quota,
                },
                "note": "Non-blocking launch: Zenith mailbox owns the Jules conversation; circle back via jules_bijective_sync.",
            }
        except Exception as exc:
            return {"error": "jules_launch_task_failed", "message": str(exc)}

    @mcp.tool(
        name="jules_dispatch_batch",
        description=(
            "Dispatch a batch of Jules sessions in one SMTP-analog turn. "
            "Same primitive as jules_launch_task, gathered in parallel via asyncio.gather. "
            "Returns one record per task_id in input order. Each record carries its own "
            "remote_id on success or an error message on failure."
        ),
    )
    async def jules_dispatch_batch(
        project_id: Annotated[str, Field(description="Project id.")],
        items: Annotated[
            list[dict[str, Any]],
            Field(description="List of {task_id, prompt} dicts. task_id optional; auto-generated when missing."),
        ],
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            from .models import MissionRunning
            from .jules_acp_bridge import launch_jules_bijective, load_session_store

            proj_state = controller.store.load_state(project_id)
            default_mission_id = proj_state.mission_id if isinstance(proj_state, MissionRunning) else "mcp"
            import time as _time

            async def _one(item: dict[str, Any]) -> dict[str, Any]:
                task_id = item.get("task_id") or f"jules-{int(_time.time() * 1000)}-{id(item)}"
                prompt = item.get("prompt", "")
                mission_id = item.get("mission_id", default_mission_id)
                try:
                    remote_id, state = await launch_jules_bijective(
                        prompt_text=prompt,
                        cwd=cwd,
                        task_id=task_id,
                        project_id=project_id,
                        mission_id=mission_id,
                    )
                    session = load_session_store(cwd).get(remote_id, {})
                    return {
                        "task_id": task_id,
                        "remote_id": remote_id,
                        "mission_id": mission_id,
                        "status": state.status,
                        "is_terminal": state.is_terminal,
                        "mailbox_path": session.get("mailbox_path"),
                    }
                except Exception as exc:
                    return {"task_id": task_id, "error": str(exc)}

            results = await asyncio.gather(*(_one(it) for it in items))
            warning = None
            if len(items) > JULES_VOTING_MAX_FANOUT:
                warning = (
                    f"Batch size {len(items)} exceeds voting cap "
                    f"JULES_VOTING_MAX_FANOUT={JULES_VOTING_MAX_FANOUT}. "
                    f"Consider 3 or 5 for voting, {JULES_VOTING_OPTIMAL} for non-voting consensus."
                )
                logger.warning(warning)

            # Get live quota metrics
            from .jules_acp_bridge import check_jules_quota
            try:
                active, headroom = await check_jules_quota(cwd)
            except Exception:
                active, headroom = 0, 4
            # Get rolling 24h quota
            daily_quota = controller.store.config.jules_quota_per_24h
            dispatches_24h = controller.store.jules_dispatches_in_last_24h(project_id)

            payload: dict[str, Any] = {
                "results": results,
                "count": len(results),
                "quota": {
                    "active_sessions": active,
                    "concurrency_headroom": headroom,
                    "dispatches_last_24h": dispatches_24h,
                    "rolling_headroom": max(0, daily_quota - dispatches_24h),
                    "daily_quota": daily_quota,
                },
            }
            if warning:
                payload["warning"] = warning
            return payload
        except Exception as exc:
            return {"error": "jules_dispatch_batch_failed", "message": str(exc)}

    @mcp.tool(
        name="jules_poll_batch",
        description=(
            "Single-shot batch poll of N Jules sessions. Read-side counterpart to "
            "jules_dispatch_batch: returns one record per remote_id in input order, "
            "each carrying status, succeeded, pr_url, pushed_branch, mailbox_path. "
            "Non-blocking — one status check per session, no poll loops. "
            "Use this for cross-Jules consensus / voting: fan out via jules_dispatch_batch, "
            "collect verdicts via jules_poll_batch."
        ),
    )
    async def jules_poll_batch(
        project_id: Annotated[str, Field(description="Project id.")],
        remote_ids: Annotated[
            list[str],
            Field(description="List of Jules remote session ids to poll."),
        ],
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            from .jules_acp_bridge import check_jules_status, load_session_store

            store = load_session_store(cwd)

            async def _one(remote_id: str) -> dict[str, Any]:
                try:
                    state = await check_jules_status(remote_id, cwd)
                    session = store.get(remote_id, {})
                    return {
                        "remote_id": remote_id,
                        "status": state.status,
                        "is_terminal": state.is_terminal,
                        "succeeded": state.succeeded,
                        "pr_url": state.pr_url,
                        "pushed_branch": state.pushed_branch,
                        "mailbox_path": session.get("mailbox_path"),
                    }
                except Exception as exc:
                    return {"remote_id": remote_id, "error": str(exc)}

            results = await asyncio.gather(*(_one(rid) for rid in remote_ids))

            # Recency gate: a session is "ready_for_interaction" only if it
            # has reached terminal state AND delivered (PR or branch). The
            # eligible set is sorted by completion time, newest first, with
            # sessions older than JULES_RECENCY_HOURS excluded from the
            # "ready" count even if they passed the recency gate.
            import time as _time
            now = _time.time()
            recency_cutoff = now - (JULES_RECENCY_HOURS * 3600)
            for r in results:
                if "error" in r:
                    r["ready_for_interaction"] = False
                    r["recency_status"] = "error"
                    continue
                ready = bool(r.get("is_terminal")) and bool(r.get("succeeded"))
                session = store.get(r.get("remote_id", ""), {})
                completed_at = session.get("updated_at") or session.get("created_at") or 0
                in_window = completed_at >= recency_cutoff
                r["ready_for_interaction"] = ready and in_window
                if not ready:
                    r["recency_status"] = "not_terminal_or_undelivered"
                elif not in_window:
                    r["recency_status"] = "expired"
                else:
                    r["recency_status"] = "ready"
                r["completed_at"] = completed_at

            # Rank eligible sessions newest-first by completion time.
            eligible = sorted(
                (r for r in results if r.get("ready_for_interaction")),
                key=lambda r: r.get("completed_at", 0),
                reverse=True,
            )
            ready_count = sum(1 for r in results if r.get("ready_for_interaction"))

            warning = None
            if len(remote_ids) > JULES_VOTING_MAX_FANOUT:
                warning = (
                    f"Batch size {len(remote_ids)} exceeds voting cap "
                    f"JULES_VOTING_MAX_FANOUT={JULES_VOTING_MAX_FANOUT}. "
                    f"Consider 3 or 5 for voting, {JULES_VOTING_OPTIMAL} for non-voting consensus."
                )
                logger.warning(warning)

            payload: dict[str, Any] = {
                "results": results,
                "count": len(results),
                "ready_count": ready_count,
                "ready_ranked": [r["remote_id"] for r in eligible],
            }
            if warning:
                payload["warning"] = warning
            return payload
        except Exception as exc:
            return {"error": "jules_poll_batch_failed", "message": str(exc)}

    @mcp.tool(
        name="jules_converse",
        description=(
            "Send a follow-up message to an existing Jules remote session. "
            "Non-blocking: forwards message to Jules REST API and returns immediately. "
            "Jules chats but can't be waited on — circle back later via jules_bijective_sync. "
            "Use for latent goal telegraphing at mating points."
        ),
    )
    async def jules_converse(
        project_id: Annotated[str, Field(description="Project id.")],
        remote_id: Annotated[str, Field(description="Jules remote session/task ID.")],
        message: Annotated[str, Field(description="Follow-up prompt for Jules.")],
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            from .jules_acp_bridge import send_jules_message, check_jules_status, load_session_store
            await send_jules_message(remote_id, message, cwd)
            # Non-blocking: single status check, no poll loop
            state = await check_jules_status(remote_id, cwd)
            session = load_session_store(cwd).get(remote_id, {})
            return {
                "remote_id": remote_id,
                "sent": True,
                "status": state.status,
                "pr_url": state.pr_url,
                "succeeded": state.succeeded,
                "mailbox_path": session.get("mailbox_path"),
                "note": "Non-blocking — Jules chats but can't be waited on. Circle back via jules_bijective_sync.",
            }
        except Exception as exc:
            return {
                "error": "jules_converse_failed",
                "message": str(exc),
            }

    @mcp.tool(
        name="jules_bijective_sync",
        description=(
            "Bijective sync between Jules remote state and Zenith mission state. "
            "Maps Jules running/queued ↔ Zenith mission_running; "
            "Jules awaiting_user_feedback ↔ Zenith attention_needed with needs_orchestrator_answer=True "
            "(the orchestrator should call jules_converse with an answer or let the time window expire); "
            "Jules completed-with-PR → Zenith decide_attention with PR merge route; "
            "Jules failed OR completed-without-PR → Zenith decide_attention with action=patch for debt mitigation. "
            "Tracks mating contracts at opposite timeline ends for intercourse support. "
            "NOT GREEDY: NARS (Non-Axiomatic Reasoning System) promotion to Jules landscape only triggers on terminal state."
        ),
    )
    async def jules_bijective_sync(
        project_id: Annotated[str, Field(description="Project id.")],
        remote_id: Annotated[str, Field(description="Jules remote session/task ID.")],
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            from .jules_acp_bridge import check_jules_status, promote_nars_to_jules_landscape, load_session_store
            state = await check_jules_status(remote_id, cwd)
            session = load_session_store(cwd).get(remote_id, {})

            # Bijection routing:
            # - awaiting_user_feedback → orchestrator_answer_required
            # - running/queued/pending/active → mission_running
            # - completed-with-PR → decide_attention (pr_merge)
            # - completed-without-PR or failed → decide_attention (patch debt)
            if state.needs_orchestrator_answer:
                zenith_state = "attention_needed"
                debt_route = "orchestrator_answer"
            elif state.normalized_status in ("running", "queued", "pending", "active"):
                zenith_state = "mission_running"
                debt_route = "wait"
            elif state.succeeded:
                zenith_state = "decide_attention"
                debt_route = "pr_merge"
            else:
                zenith_state = "decide_attention"
                debt_route = "patch"

            # NOT GREEDY: Only promote NARS (Non-Axiomatic Reasoning System) when Jules reaches terminal state
            nars_promoted: list[str] = []
            if state.is_terminal:
                from .models import MissionRunning
                proj_state = controller.store.load_state(project_id)
                if isinstance(proj_state, MissionRunning):
                    mission_id = proj_state.mission_id
                    nars_promoted = promote_nars_to_jules_landscape(
                        project_id, mission_id, cwd
                    )

            return {
                "remote_id": remote_id,
                "jules_status": state.status,
                "jules_normalized": state.normalized_status,
                "pr_url": state.pr_url,
                "pushed_branch": state.pushed_branch,
                "delivered": state.delivered,
                "succeeded": state.succeeded,
                "is_terminal": state.is_terminal,
                "needs_orchestrator_answer": state.needs_orchestrator_answer,
                "clarification_question": state.description if state.needs_orchestrator_answer else None,
                "zenith_mapped_state": zenith_state,
                "debt_mitigation_route": debt_route,
                "mating_contracts": state.delivered,
                "nars_promoted": nars_promoted,
                "mailbox_path": session.get("mailbox_path"),
                "repo_root": session.get("repo_root"),
            }
        except Exception as exc:
            return {"error": "jules_bijective_sync_failed", "message": str(exc)}

    @mcp.tool(
        name="jules_list_sessions",
        description=(
            "List all recorded Jules sessions in the workspace backlog. "
            "Returns a dictionary of remote_id to session metadata (task_id, project_id, mission_id, created_at)."
        ),
    )
    async def jules_list_sessions(
        project_id: Annotated[str, Field(description="Project id.")],
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            from .jules_acp_bridge import load_session_store
            sessions = await asyncio.to_thread(load_session_store, cwd)
            return {"sessions": sessions}
        except Exception as exc:
            return {"error": "jules_list_sessions_failed", "message": str(exc)}

    @mcp.tool(
        name="mission_mail",
        description=(
            "Send or read mail within a mission's NARS-anchored mailbox. "  # NARS = Non-Axiomatic Reasoning System
            "Slug = mission id. The first line of the mailbox file is the "
            "contract header (markdown summary); subsequent lines are events. "
            "Every event must carry NARS terms (Non-Axiomatic Reasoning System logic); body is capped at 200 chars. "
            "Use to implement Jules-to-Jules post-PR discussion on shared NARS (Non-Axiomatic Reasoning System) contracts."
        ),
    )
    async def mission_mail(
        project_id: Annotated[str, Field(description="Project id.")],
        slug: Annotated[str, Field(description="Mission id (the contract slug).")],
        action: Annotated[str, Field(description="Action: 'send' or 'read'.")],
        # Send params
        from_party: Annotated[str, Field(description="Sender: remote_id or 'orchestrator'.")] | None = None,
        to_party: Annotated[str, Field(description="Recipient: remote_id, 'orchestrator', or '*' for broadcast.")] | None = None,
        nars: Annotated[list[str], Field(description="NARS terms (Non-Axiomatic Reasoning System logic; required for send).")] | None = None,
        body: Annotated[str, Field(description="Body text (max 200 chars for send).")] | None = None,
        kind: Annotated[str, Field(description="Event kind: 'open', 'round', 'consensus', 'status'.")] = "round",
        # Read params
        last_n: Annotated[int, Field(description="Number of recent events to read (default 1).")] = 1,
    ) -> dict[str, Any]:
        try:
            cwd = str(controller.store.workspace_dir(project_id))
            from .jules_acp_bridge import append_mission_mailbox, load_session_store, read_mission_mailbox

            if action == "send":
                if not from_party or not to_party or not nars:
                    return {"error": "missing_fields", "message": "send requires from_party, to_party, nars"}
                if body is None:
                    body = ""
                if len(body) > 200:
                    return {"error": "body_too_long", "message": "body exceeds 200 char limit"}
                # Only conversational or orchestrator can send mail
                if from_party != "orchestrator":
                    store = load_session_store(cwd)
                    session = store.get(from_party, {})
                    state = session.get("state", "launched")
                    if state != "conversational":
                        return {"error": "not_conversational", "message": f"session {from_party} is {state}, must be conversational to send mail"}
                path = await asyncio.to_thread(
                    append_mission_mailbox,
                    cwd,
                    slug,
                    from_party=from_party,
                    to_party=to_party,
                    kind=kind,
                    nars=nars,
                    body=body,
                )
                return {"sent": True, "mailbox_path": str(path), "slug": slug}

            elif action == "read":
                events = await asyncio.to_thread(
                    read_mission_mailbox,
                    cwd,
                    slug,
                    last_n=last_n,
                )
                return {"slug": slug, "events": events, "count": len(events)}

            else:
                return {"error": "invalid_action", "message": f"unknown action: {action}"}
        except Exception as exc:
            return {"error": "mission_mail_failed", "message": str(exc)}

# ---------------------------------------------------------------------------
# Worker tool
# ---------------------------------------------------------------------------


def _register_worker_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="end_node",
        description=(
            "Assigned-session runtime handoff. Report completion for the assigned "
            "work or validation task. Call exactly "
            "once before exiting. After this call, do not invoke any other tools — the "
            "session is finished. For validation tasks, include `items` (one per "
            "assigned contract target) and the aggregate `passed`."
        ),
    )
    async def end_node(
        done: Annotated[
            bool,
            Field(description="True if the assigned work or validation audit completed."),
        ],
        report: Annotated[str, Field(description="Free-form handoff report. Severity tags are authoring discipline, not runtime triggers.")],
        request_attention: Annotated[
            bool,
            Field(
                default=False,
                description="Set True to return this completed task's raw report to the orchestrator before the task list continues.",
            ),
        ] = False,
        items: Annotated[
            list[ValidationItem] | None,
            Field(
                default=None,
                description="Validation task only: one entry per assigned contract target with per-item passed verdict.",
            ),
        ] = None,
        passed: Annotated[
            bool | None,
            Field(
                default=None,
                description="Validation task only: aggregate True iff every items[].passed.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        node_type = os.environ.get("ZENITH_NODE_TYPE", "work")
        handoff_path = os.environ.get("ZENITH_HANDOFF_PATH")
        if not handoff_path:
            raise RuntimeError("ZENITH_HANDOFF_PATH not set in worker env")
        node_id = os.environ.get("ZENITH_NODE_ID")
        if not node_id:
            raise RuntimeError("ZENITH_NODE_ID not set in worker env")

        handoff: WorkHandoff | ValidateHandoff
        if node_type == "validate":
            handoff = ValidateHandoff(
                node_id=node_id,
                done=done,
                report=report,
                items=items or [],
                passed=bool(passed) if passed is not None else all(i.passed for i in (items or [])),
                request_attention=request_attention,
            )
        else:
            handoff = WorkHandoff(
                node_id=node_id,
                done=done,
                report=report,
                request_attention=request_attention,
            )
        atomic_write_json(handoff_path, handoff.model_dump(mode="json"))
        return {
            "recorded": True,
            "message": "Session complete, your job is done now; do not call further tools and just end your job now.",
        }


# ---------------------------------------------------------------------------
# Terminal-reviewer tool
# ---------------------------------------------------------------------------


def _register_terminal_reviewer_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="submit_terminal_review",
        description=(
            "Runtime-closure-check-only. Submit the mission closure result. "
            "`done=true` means clean enough to close; `done=false` returns the raw "
            "closure report to the orchestrator. Call exactly once."
        ),
    )
    async def submit_terminal_review(
        done: Annotated[
            bool,
            Field(description="True only when the mission should close as done."),
        ],
        report: Annotated[
            str,
            Field(description="Raw closure report for the orchestrator."),
        ] = "",
    ) -> dict[str, Any]:
        path = os.environ.get("ZENITH_TERMINAL_REVIEW_PATH")
        if not path:
            raise RuntimeError(
                "ZENITH_TERMINAL_REVIEW_PATH not set in terminal-reviewer env"
            )
        review = TerminalReviewHandoff(done=done, report=report)
        atomic_write_json(path, review.model_dump(mode="json"))
        return {
            "recorded": True,
            "message": "Terminal review submitted; do not call further tools.",
        }


# ---------------------------------------------------------------------------
# Envelope payload helper
# ---------------------------------------------------------------------------


def _to_payload(env_or_err) -> dict[str, Any]:
    if isinstance(env_or_err, ToolError):
        return {
            "error": env_or_err.code,
            "message": env_or_err.message,
            "details": [str(d) for d in (env_or_err.details or [])],
        }
    return env_or_err.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Zenith MCP Server (v5)")
    parser.add_argument(
        "--mode",
        choices=["orchestrator", "worker", "terminal-reviewer"],
        default="orchestrator",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)  # 0 → ephemeral
    parser.add_argument("--orchestrator-provider", default=None)
    parser.add_argument("--worker-provider", default=None)
    args = parser.parse_args()

    # Apply CLI overrides to environment before config discovery
    if args.orchestrator_provider:
        os.environ["ZENITH_ORCHESTRATOR_PROVIDER"] = args.orchestrator_provider
    if args.worker_provider:
        os.environ["ZENITH_WORKER_PROVIDER"] = args.worker_provider

    if args.mode == "orchestrator":
        config = HarnessConfig.discover()
        # Production: wire the ACP dispatcher from acp_runner.
        dispatcher: NodeDispatcher
        reviewer: TerminalReviewer
        try:
            from .acp_runner import ACPNodeDispatcher, ACPTerminalReviewer  # noqa: PLC0415

            dispatcher = ACPNodeDispatcher(config)
            reviewer = ACPTerminalReviewer(config)
        except Exception as exc:  # pragma: no cover — diagnostic
            logger.warning("Falling back to no-op dispatcher: %s", exc)
            from .dispatcher import MockDispatcher, MockTerminalReviewer  # noqa: PLC0415

            dispatcher = MockDispatcher(
                lambda r: WorkHandoff(
                    node_id=r.task.id, done=False, report="no ACP runtime available"
                )
            )
            reviewer = MockTerminalReviewer(TerminalReviewHandoff(done=True, report=""))
        controller = ProjectController(config, dispatcher, reviewer)
        server = create_orchestrator_server(config, controller)
    elif args.mode == "worker":
        server = create_worker_server()
    else:
        server = create_terminal_reviewer_server()

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            stateless_http=True,
            uvicorn_config={"timeout_graceful_shutdown": 5},
        )


__all__ = [
    "create_orchestrator_server",
    "create_worker_server",
    "create_terminal_reviewer_server",
    "main",
]
