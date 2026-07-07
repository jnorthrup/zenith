import tempfile
from pathlib import Path
from zenith_harness.storage import ProjectStore, workspace_fingerprint
from zenith_harness.models import TaskStateEntry, TaskStateFile, MissionRunning
from zenith_harness.config import HarnessConfig
from datetime import datetime, timedelta

# Test workspace_fingerprint
with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp)
    (ws / 'test.py').write_text('print(1)')
    fp1 = workspace_fingerprint(ws)
    assert len(fp1) == 16
    fp2 = workspace_fingerprint(ws)
    assert fp1 == fp2
    print('✓ workspace_fingerprint stable for unchanged dir')

# Test TaskStateEntry coalesced counters
entry = TaskStateEntry()
assert entry.attempt_count == 0
assert entry.success_count == 0
entry.attempt_count += 1
entry.attempt_count += 1
entry.success_count += 1
assert entry.attempt_count == 2
assert entry.success_count == 1
print('✓ TaskStateEntry coalesced counters work')

# Test TaskStateFile.record_attempt
tsf = TaskStateFile()
tsf.record_attempt('task-1', '2026-07-07T10-00-00Z', success=False)
tsf.record_attempt('task-1', '2026-07-07T10-05-00Z', success=True)
tsf.record_attempt('task-1', '2026-07-07T10-10-00Z', success=True)
assert tsf.tasks['task-1'].attempt_count == 3
assert tsf.tasks['task-1'].success_count == 2
assert tsf.tasks['task-1'].last_done_at is not None
attempts, successes = tsf.attempt_stats('task-1')
assert attempts == 3 and successes == 2
print('✓ TaskStateFile.record_attempt and attempt_stats work')

# Test sweep_stale_attempts with mock data
with tempfile.TemporaryDirectory() as tmp:
    config = HarnessConfig.discover()
    # Can't set projects_dir directly on frozen instance
    # Use default config which uses ZENITH_HOME env
    store = ProjectStore(config)
    record = store.create_project('test project', tmp)
    pid = record.id
    store.save_state(pid, MissionRunning(mission_id='mission-001'))
    
    # Create some mock attempts
    now = datetime.now()
    
    # Old attempt (8 days ago)
    old_ts = (now - timedelta(days=8)).strftime('%Y-%m-%dT%H-%M-%SZ')
    attempt_dir = store.attempts_runtime_dir(pid, 'mission-001')
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / f'{old_ts}__task-old.json').write_text('{"node_id": "task-old", "done": true}')
    
    # New attempts (3 within cap)
    for i in range(3):
        ts = (now - timedelta(hours=i)).strftime('%Y-%m-%dT%H-%M-%SZ')
        (attempt_dir / f'{ts}__task-new.json').write_text('{"node_id": "task-new", "done": true}')
    
    # 4th new attempt (exceeds max_per_node=3)
    ts4 = (now - timedelta(hours=4)).strftime('%Y-%m-%dT%H-%M-%SZ')
    (attempt_dir / f'{ts4}__task-new.json').write_text('{"node_id": "task-new", "done": true}')
    
    # Run sweep
    tombstoned = store.sweep_stale_attempts(pid, 'mission-001', max_age_days=7, max_per_node=3)
    assert tombstoned >= 2  # old one + 4th new one
    print(f'✓ sweep_stale_attempts tombstoned {tombstoned} attempts')

print('All new functionality verified!')