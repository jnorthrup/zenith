import subprocess
from pathlib import Path
import json
import pytest
from zenith_harness.contractreifier import render_full_with_contract

def test_nars_summarization_head_n10(tmp_path: Path):
    # 1. Create a dummy JSON file
    json_path = tmp_path / "contract.json"
    data = {
        "id": "mission-xyz",
        "title": "A Test Mission",
        "description": "Verify NARS compliance"
    }
    json_path.write_text(json.dumps(data), encoding="utf-8")

    # 2. Define NARS terms
    nars_terms = [
        "<A --> B>",
        "<C --> D>",
        "<E --> F>"
    ]

    # 3. Call render_full_with_contract
    rendered = render_full_with_contract(json_path, nars_terms)
    
    # Write to file
    rendered_file = tmp_path / "rendered.json"
    rendered_file.write_text(rendered, encoding="utf-8")

    # 4. Use bash head -n10 to get the first 10 lines
    # Try 'ghead' first (GNU head), then fallback to 'head'
    try:
        proc = subprocess.run(
            ["ghead", "-n10", str(rendered_file)],
            capture_output=True,
            text=True,
            check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        proc = subprocess.run(
            ["head", "-n10", str(rendered_file)],
            capture_output=True,
            text=True,
            check=True
        )

    # 5. Assertions on the head -n10 output
    lines = proc.stdout.splitlines()
    assert len(lines) == 10, f"Expected exactly 10 lines, got {len(lines)}"

    # Line 1: {"id":"mission-xyz","nars":[
    assert lines[0].startswith('{"id":"mission-xyz","nars":[')

    # Lines 2-4: NARS terms (with indent and quotes)
    assert '"<A --> B>"' in lines[1]
    assert '"<C --> D>"' in lines[2]
    assert '"<E --> F>"' in lines[3]

    # Lines 5-10: Should be empty strings (padding to line 10)
    for i in range(4, 10):
        assert lines[i] == ""

    # 6. Read the whole file and verify it's valid JSON
    full_data = json.loads(rendered)
    assert full_data["id"] == "mission-xyz"
    assert full_data["nars"] == nars_terms
    assert full_data["title"] == "A Test Mission"
    assert full_data["description"] == "Verify NARS compliance"

    # 7. Check the continuation line (Line 11) starts with the closing array bracket
    full_lines = rendered.splitlines()
    assert len(full_lines) >= 11
    assert full_lines[10].startswith('],')
