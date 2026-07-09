"""contractreifier — JSON with NARS contract for head -n10 scanning.

MECHANISM: The JSON file MUST be formatted with newlines between elements.
When you run `head -n10 contract.json`, you get the first 10 lines containing:
  - Line 1: {"id":"...","nars":[
  - Lines 2-10: NARS terms (spread across 8 lines, comma-separated)
  - Line 11+: ],"title":"...",...} (closes array + rest of JSON)

The file is valid JSON when read as a whole. head -n10 gives scannable summary.

NARS format (Pei Wang Non-Axiomatic Reasoning):
  - <term1 --> term2>  (inheritance)
  - <term1 <-> term2>  (similarity)  
  - <term1 ==> term2>  (implication)
  - {frequency, confidence} (truth value)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MAX_LINE_WIDTH = 280
MAX_LINES = 10


def _cap(line: str) -> str:
    """Truncate to MAX_LINE_WIDTH, preserving valid JSON string escapes."""
    if len(line) <= MAX_LINE_WIDTH:
        return line
    return line[:MAX_LINE_WIDTH - 1] + "…"


def _pack_nars_into_lines(nars: list[str]) -> list[str]:
    """Pack nars strings into lines ≤280 bytes, up to 2 lines (lines 2-3).
    
    Format:
    - Line 2: first half of NARS terms (with trailing comma)
    - Line 3: second half of NARS terms (no comma - continuation closes it)  
    - Lines 4-10: blank
    """
    if not nars:
        return []
    
    # Split into roughly 2 halves
    mid = (len(nars) + 1) // 2
    first_half = nars[:mid]
    second_half = nars[mid:]
    
    lines: list[str] = []
    current_line = ""
    
    # Process first half
    for i, nars_str in enumerate(first_half):
        encoded = json.dumps(nars_str, ensure_ascii=False)
        separator = "," if current_line else ""
        candidate = separator + encoded
        
        if len(current_line) + len(candidate) > MAX_LINE_WIDTH:
            if current_line:
                lines.append(current_line + ",")  # add trailing comma
                current_line = encoded
            else:
                lines.append(_cap(candidate) + ",")
                current_line = ""
        else:
            current_line += candidate
    
    if current_line:
        lines.append(current_line + ",")  # first half always has comma
    
    # Process second half (last, no comma needed)
    current_line = ""
    for i, nars_str in enumerate(second_half):
        encoded = json.dumps(nars_str, ensure_ascii=False)
        separator = "," if current_line else ""
        candidate = separator + encoded
        
        if len(current_line) + len(candidate) > MAX_LINE_WIDTH:
            if current_line:
                lines.append(current_line)  # no trailing comma on second line
                if len(lines) >= 2:
                    break
                current_line = encoded
            else:
                lines.append(_cap(candidate))
                if len(lines) >= 2:
                    break
                current_line = ""
        else:
            current_line += candidate
    
    if current_line and len(lines) < 2:
        lines.append(current_line)  # no trailing comma on last
    
    return lines[:2]


def _render_contract_lines(json_bytes: bytes, nars: list[str]) -> list[str]:
    """Helper to render augmented JSON lines with NARS starting on Line 1.
    
    Ensures that:
    - Line 1 contains both {"id":"...","nars":[ and the first NARS term.
    - Subsequent NARS terms are on Line 2, Line 3, etc.
    - Exact padding to 10 lines is maintained.
    - Line 11+ contains the closing bracket and remaining JSON properties.
    """
    data = json.loads(json_bytes)
    items = list(data.items())
    if not items:
        return [json.dumps(data, ensure_ascii=False)]

    id_key, id_value = items[0]
    id_json = json.dumps(id_value, ensure_ascii=False)

    lines: list[str] = []

    if nars:
        first_term = json.dumps(nars[0], ensure_ascii=False)
        has_more_nars = len(nars) > 1
        if has_more_nars:
            line1 = f'{{"{id_key}":{id_json},"nars":[{first_term},'
        else:
            line1 = f'{{"{id_key}":{id_json},"nars":[{first_term}'
        
        if len(line1) > MAX_LINE_WIDTH:
            line1 = _cap(line1)
        lines.append(line1)

        for i in range(1, len(nars)):
            term = json.dumps(nars[i], ensure_ascii=False)
            has_more = i < len(nars) - 1
            line = f'  {term},' if has_more else f'  {term}'
            if len(line) > MAX_LINE_WIDTH:
                line = _cap(line)
            lines.append(line)
    else:
        line1 = f'{{"{id_key}":{id_json},"nars":['
        if len(line1) > MAX_LINE_WIDTH:
            line1 = _cap(line1)
        lines.append(line1)

    while len(lines) < 10:
        lines.append("")

    rest_items = items[1:]
    if rest_items:
        rest_dict = dict(rest_items)
        rest_json = json.dumps(rest_dict, ensure_ascii=False, separators=(',', ':'))
        lines.append(f'],{rest_json[1:]}')
    else:
        lines.append(']}')

    return lines


def _inject_nars(json_bytes: bytes, nars: list[str]) -> str:
    """Inject nars array after the first field (id) in JSON for head -n10."""
    lines = _render_contract_lines(json_bytes, nars)
    return "\n".join(lines[:10])


def render_contract_head(json_path: Path, nars: list[str]) -> str:
    """Render first 10 lines of JSON with nars contract injected.
    
    Args:
        json_path: Path to existing valid JSON file
        nars: List of NARS contract strings to inject
        
    Returns:
        First 10 lines of augmented JSON (≤280 bytes each)
    """
    json_bytes = json_path.read_bytes()
    return _inject_nars(json_bytes, nars)


def render_full_with_contract(json_path: Path, nars: list[str]) -> str:
    """Render full JSON with nars contract injected, formatted for head -n10.
    
    This is valid JSON when read as a whole. head -n10 shows scannable summary.
    """
    json_bytes = json_path.read_bytes()
    lines = _render_contract_lines(json_bytes, nars)
    return "\n".join(lines)


def render_contract_continuation(json_path: Path, nars: list[str]) -> str:
    """Render JSON continuation starting from line 11."""
    json_bytes = json_path.read_bytes()
    lines = _render_contract_lines(json_bytes, nars)
    if len(lines) >= 11:
        return "\n".join(lines[10:])
    return ""


__all__ = [
    "render_contract_head",
    "render_full_with_contract",
    "MAX_LINE_WIDTH",
    "MAX_LINES",
]