# Solid Single PR Compromise (max beneficial)

## NARS Head (strict ≤10)

```narsese
<solid_PR --> max_value_upstream + jnorthrup + mailbox>. (1.0, 0.96)
<include --> {#13_stderr, #16_empty_contract, #4_hermes, #17_effort, #14_gates}>. (0.97, 0.93)
<include_jnorthrup --> {Jules_ACP, mailbox, attempts+fingerprints, NARS_strict_10line, priority_respawn, DRY_prompts}>. (0.96, 0.92)
<exclude --> {badge, reifier_bloat, multi-section_NARS, non_DRY_duplicates}>. (0.95, 0.91)
<mailbox --> cheap_robust_JSONL_git_root + NARS_envelope + 200char_body>. (0.98, 0.95)
NAL_spot: head -n10 this + NARS_10LINE_STRICT_CONTRACT. (0.90, 0.85)
```

## What goes in the single PR (to Intelligent-Internet/zenith)

**Must (use-driven + high scale value)**  
1. #13 terminal-reviewer stderr drain (already merged upstream — verify present)  
2. #16 empty-contract reject (already merged — verify)  
3. Jules ACP provider + bridge + e2e tests + priority_respawn  
4. Canonical agent mailbox (`.zenith/mailbox/`) — JSONL, git-root, NARS-anchored envelopes, path-safe  
5. Coalesced attempts + workspace fingerprints + tombstone sweep  
6. Strict 10-line NARS Therblig + anti-slop contract + Kolmogorov heuristic  
7. Per-role reasoning effort (from open #17)  
8. Gate checkpoints + skill validation (from open #14)  

**Should**  
- DRY prompt includes  
- Local project mode + build-SHA stale replace  
- Hermes already merged  

**Must not**  
- Multi-section NARS walls  
- Contract reifier / JSON contract bloat  
- Badge / marketing  
- Duplicate upstream code  

## Mailbox investigation (cheap + robust)

- Location: `<git-root>/.zenith/mailbox/` (symlink-safe via `find_git_root`)  
- Format: JSONL envelopes `{unix_ts, from_party, to_party, kind, nars[], body≤200}`  
- NARS-anchored (required non-empty nars list = scope discipline)  
- Mission mailbox + legacy per-session jules/*.jsonl + sessions.json index  
- Security: slug sanitization, path traversal reject  
- Tests: symlink survival, append + session index update  
- TODO still open: harvest facts from transcripts into downstream prompts  
- Verdict: cheap (append-only JSONL), robust (git-root, atomic index, filters), perfect for agent handoff without long conversations  

## Playbook for the solid PR
1. Branch from upstream main (or rebased jnorthrup tip).  
2. Cherry-pick / port only the Must list.  
3. Enforce NARS_10LINE_STRICT_CONTRACT on every head.  
4. Wire mailbox into orchestrator/worker handoffs (complete the TODOs).  
5. Full pytest + Jules e2e green.  
6. Single clean PR to Intelligent-Internet/zenith titled for the compromise.  

**Memory**: `zenith-solid-pr-v1` | `zenith-mailbox-v1` | `zenith-anti-slop-v1`
