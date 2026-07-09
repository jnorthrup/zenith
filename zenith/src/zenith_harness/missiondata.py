"""Mission data collection — reads on-disk JSON state for contract rendering.

This module handles reading task-state.json, contract-state.json, tasks.json
and producing the data structures used by contractreifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    ContractStateFile,
    ProjectState,
    Task,
    TaskList,
    TaskStateFile,
)
from .storage import AttemptRecord, ProjectStore


# -----------------------------------------------------------------------
# Data containers — sourced from on-disk JSON
# -----------------------------------------------------------------------


@dataclass(frozen=True)
class TaskMetrics:
    task_id: str
    task_type: str
    status: str
    targets: list[str]
    depends_on: list[str]
    skill: str | None
    attempt_count: int = 0
    success_count: int = 0
    last_done_at: str | None = None
    last_attempt: str | None = None
    priority_respawn: bool = False


@dataclass(frozen=True)
class ContractMetrics:
    assertion_id: str
    verdict: str  # pending | passed | failed
    owner_task_id: str | None = None
    owner_status: str | None = None


@dataclass(frozen=True)
class MissionMetrics:
    mission_id: str
    state_label: str
    tasks: list[TaskMetrics] = field(default_factory=list)
    contracts: list[ContractMetrics] = field(default_factory=list)
    total_attempts: int = 0
    total_successes: int = 0
    task_counts: dict[str, int] = field(default_factory=dict)
    has_closeout: bool = False
    closeout_path: str | None = None


# -----------------------------------------------------------------------
# Collection — reads existing disk state, writes nothing
# -----------------------------------------------------------------------


def collect_mission_metrics(
    store: ProjectStore,
    project_id: str,
    mission_id: str,
    *,
    state_label: str = "unknown",
) -> MissionMetrics:
    """Aggregate the mission's on-disk state into a MissionMetrics snapshot.

    Reads:
    - tasks.json (TaskList)
    - task-state.json (TaskStateFile — attempt_count, success_count, etc.)
    - contract-state.json (ContractStateFile — per-assertion verdicts)
    - attempts/ directory (for total attempt count)
    """
    try:
        tl = store.load_task_list(project_id, mission_id)
    except FileNotFoundError:
        tl = TaskList(tasks=[])

    task_state = store.load_task_state(project_id, mission_id)
    contract_state = store.load_contract_state(project_id, mission_id)

    owner_by_assertion: dict[str, str] = {}
    for task in tl.tasks:
        if task.type == "work" and task_state.status_of(task.id) != "superseded":
            for tgt in task.targets:
                owner_by_assertion.setdefault(tgt, task.id)

    task_metrics: list[TaskMetrics] = []
    task_counts: dict[str, int] = {}
    total_attempts = 0
    total_successes = 0

    for task in tl.tasks:
        status = task_state.status_of(task.id)
        task_counts[status] = task_counts.get(status, 0) + 1
        entry = task_state.tasks.get(task.id)
        ac, sc = task_state.attempt_stats(task.id)
        total_attempts += ac
        total_successes += sc
        task_metrics.append(
            TaskMetrics(
                task_id=task.id,
                task_type=task.type,
                status=status,
                targets=list(task.targets),
                depends_on=list(task.depends_on),
                skill=task.skill,
                attempt_count=ac,
                success_count=sc,
                last_done_at=entry.last_done_at if entry else None,
                last_attempt=entry.last_attempt if entry else None,
                priority_respawn=task_state.priority_respawn_of(task.id),
            )
        )

    contract_metrics: list[ContractMetrics] = []
    for aid in sorted(contract_state.items.keys()):
        cs_entry = contract_state.items[aid]
        owner_id = owner_by_assertion.get(aid)
        owner_status = task_state.status_of(owner_id) if owner_id else None
        contract_metrics.append(
            ContractMetrics(
                assertion_id=aid,
                verdict=cs_entry.status,
                owner_task_id=owner_id,
                owner_status=owner_status,
            )
        )

    closeout = store.mission_dir(project_id, mission_id) / "closeout.md"
    has_closeout = closeout.exists()

    return MissionMetrics(
        mission_id=mission_id,
        state_label=state_label,
        tasks=task_metrics,
        contracts=contract_metrics,
        total_attempts=total_attempts,
        total_successes=total_successes,
        task_counts=task_counts,
        has_closeout=has_closeout,
        closeout_path=str(closeout) if has_closeout else None,
    )


__all__ = [
    "TaskMetrics",
    "ContractMetrics",
    "MissionMetrics",
    "collect_mission_metrics",
]