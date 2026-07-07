from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _make_fake_jules(tmp_path: Path) -> Path:
    script = tmp_path / "fake-jules"
    script.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    counter = Path(os.environ[\"FAKE_JULES_COUNTER\"])
    terminal_status = os.environ.get(\"FAKE_JULES_TERMINAL_STATUS\", \"completed\")
    if argv[:2] == [\"remote\", \"new\"]:
        print(\"Session is created.\")
        print(\"Session ID: sess-123\")
        return 0
    if argv[:2] == [\"remote\", \"status\"]:
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        if count == 0:
            print(\"status: running\")
            return 0
        print(f\"status: {terminal_status}\")
        if terminal_status == \"completed\":
            print(\"PR: https://github.com/example/repo/pull/42\")
        return 0
    if argv[:2] == [\"remote\", \"pull\"]:
        print(\"PR: https://github.com/example/repo/pull/42\")
        return 0
    print(\"unknown args\", argv, file=sys.stderr)
    return 2


if __name__ == \"__main__\":
    raise SystemExit(main())
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _launch_bridge(tmp_path: Path, *, terminal_status: str) -> tuple[subprocess.Popen[str], Path]:
    handoff_path = tmp_path / "handoff.json"
    counter = tmp_path / "counter.txt"
    fake_jules = _make_fake_jules(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "JULES_BIN": str(fake_jules),
            "JULES_API_KEY": "",
            "FAKE_JULES_COUNTER": str(counter),
            "FAKE_JULES_TERMINAL_STATUS": terminal_status,
            "ZENITH_HANDOFF_PATH": str(handoff_path),
            "ZENITH_NODE_ID": "w1",
            "JULES_POLL_SECONDS": "0.01",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "zenith_harness.jules_acp_bridge"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, handoff_path


def _send_request(process: subprocess.Popen[str], request: dict) -> dict:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, f"bridge exited before response to request {request['method']}"
    return json.loads(line)


def _run_prompt(process: subprocess.Popen[str], tmp_path: Path) -> tuple[list[dict], dict]:
    initialize = _send_request(
        process,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
    )
    assert initialize["id"] == 1

    created = _send_request(
        process,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": str(tmp_path), "mcpServers": []},
        },
    )
    session_id = created["result"]["sessionId"]

    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "fix the bug"}],
                },
            }
        )
        + "\n"
    )
    process.stdin.flush()

    notifications: list[dict] = []
    response: dict | None = None
    for _ in range(40):
        line = process.stdout.readline()
        assert line, "bridge exited before session/prompt response"
        message = json.loads(line)
        if message.get("method") == "session/update":
            notifications.append(message)
            continue
        if message.get("id") == 3:
            response = message
            break
    assert response is not None
    return notifications, response


def test_jules_bridge_check_mode(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "zenith_harness.jules_acp_bridge", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0


def test_jules_bridge_prompt_success(tmp_path: Path):
    process, handoff_path = _launch_bridge(tmp_path, terminal_status="completed")
    try:
        notifications, prompt_response = _run_prompt(process, tmp_path)
        assert prompt_response["result"]["stopReason"] == "end_turn"
        assert notifications
        assert handoff_path.exists()
        handoff = json.loads(handoff_path.read_text())
        assert handoff["done"] is True
        assert "https://github.com/example/repo/pull/42" in handoff["report"]
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=20)


def test_jules_bridge_prompt_failure_writes_cannot_proceed(tmp_path: Path):
    process, handoff_path = _launch_bridge(tmp_path, terminal_status="failed")
    try:
        notifications, prompt_response = _run_prompt(process, tmp_path)
        assert prompt_response["result"]["stopReason"] == "end_turn"
        assert notifications
        assert handoff_path.exists()
        handoff = json.loads(handoff_path.read_text())
        assert handoff["done"] is False
        assert handoff["report"].startswith("cannot_proceed: ")
        assert "failed" in handoff["report"]
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=20)
