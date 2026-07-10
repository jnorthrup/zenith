# Mission: DRY Original Zenith + jnorthrup Benefits + Alternate Author PREs + Issues Steer

**Reference checkout**: `mission/dry-integrate-jnorthrup-benefits` @ `3b2b213` (jnorthrup tip e2e)  
**Parent/upstream base**: Intelligent-Internet/zenith@`feb1d622`  
**Goal**: Produce a single DRY, production-grade Zenith that absorbs high-value jnorthrup deltas while remaining a clean original, incorporating open/merged alternate-author PRs and steering via open issues.

## NARS Therblig Head (1-10 lines for SOTA/flash)

```narsese
<mission:dry_integrate --> continuous_improvement_harness>. (1.0, 0.96)
<jnorthrup_benefits --> {NARS_Therblig, Kolmogorov_heuristic, Jules_ACP_full, coalesced_attempts+fingerprints+tombstones, local_project_mode, DRY_prompts, priority_respawn, e2e_Jules_tests, gcloud_auth, markdown_contracts}>. (0.97, 0.93)
<alternate_PREs --> {#17 reasoning_effort, #14 gates+skills, #13 stderr_drain (merged), #16 empty_contract (merged), hermes (#4 merged), polish (#15)}>. (0.95, 0.90)
<issues_steer --> {#20 observability, #8 Win11 pipes, #7 multi-model_bench, #11 hooks/Ponytail, #12 resume}>. (0.92, 0.87)
<kolmogorov >1page ==> install_nars_summary(memory:"zenith-dry-v1")>. (0.91, 0.86)
NAL_spot: head -n10 this block before any worker/validator turn. (0.89, 0.84)
```

## Gap Analysis (upstream vs jnorthrup tip)

| Gap | Upstream (feb1d622) | jnorthrup tip (3b2b213) | Value | Action for DRY |
|-----|---------------------|-------------------------|-------|----------------|
| Context compression | None | NARS Therblig Index + Kolmogorov heuristic + 10-line head-scannable | **High** (token + living specs) | Keep + promote to core skill/playbook |
| Jules provider | None | Full ACP bridge, REST/CLI, converse, sessions, e2e tests, priority_respawn | **High** (multi-agent remote) | Keep; DRY entrypoints |
| Attempt accounting | Basic | Coalesced counters + workspace fingerprint + tombstone sweep | **High** (scale + hygiene) | Keep |
| Local/project mode | Limited | Symlinks, build-SHA stale replace, local project | Med-High | Keep |
| Prompts | Per-role | DRY includes + conditional Jules strip | Med | Keep |
| Reasoning effort | Hardcoded xhigh | Per-role env (also open #17) | **High** cost lever | Align with #17; keep |
| Contracts | Markdown | Markdown restored (NARS reifier removed) | Good | Keep pure MD + optional NARS head |
| Empty-contract / stderr | Fixed by #16/#13 | Inherited | High (already in base) | No-op |
| Perf N+1 / acyclic | Baseline | Jules PRs (deque, local cache, batch attempts) | Med-High | Keep if not already | 

**Net gap**: Upstream is clean core + recent structural invariants. jnorthrup adds operational scale (Jules, attempts, fingerprints), context compression (NARS), and cost knobs. DRY target = upstream cleanliness + selective jnorthrup value + open PREs.

## Upstream PR Consumption Rate

- **Window**: 2026-05-08 → 2026-07-04 (~8 weeks)
- **Total PRs observed**: 11 (1,2,4,5,13-19)
- **Merged**: 7 (#1 README, #2 v0.1, #4 hermes@jnorthrup, #5 agentic-install@fire17, #13 stderr@miroslavb, #15 polish@PhungVanDuy, #16 empty-contract@Quigleybits)
- **Closed unmerged**: 1 (#19 badge)
- **Open**: 3 (#14 gates@sw1pp3r, #17 effort@Quigleybits, #18 Codex-default@damngoodinspect)
- **Rate**: ~0.9 PRs merged / week early; slowed after v0.1 + polish. High merge rate on **use-driven** fixes (#13, #16) and core features (#4, #5); lower on marketing (#19). Open queue growing (cost + validation hardening).

## Use-emergent vs Attempted-upgrade PRs

**Use / failure-mode driven (highest value)**:
- #13 terminal stderr drain — live 0/3 end_mission failures
- #16 empty-contract rejection — structural hole allowing vacuous missions
- Related issues: #8 Win11 pipes, #12 resume, #20 observability

**Upgrade / polish / preference (good value)**:
- #4 hermes (jnorthrup) — multi-provider completeness
- #5 agentic install prompt — onboarding
- #15 CI/license/community — hygiene for external
- #17 per-role reasoning effort — cost control (open, high value)
- #14 gate checkpoints + skill validation — hardening (open)
- #18 Codex default — preference (open, lower priority)

**Low value**:
- #19 MseeP badge (closed)

**jnorthrup-internal Jules PRs** (1-6): pure perf + NARS + health — all value for scale; already absorbed in tip.

## Value Ranking (integrate first)
1. NARS + Kolmogorov heuristic (context)  
2. Jules ACP full + e2e + priority_respawn  
3. Coalesced attempts / fingerprints / tombstones  
4. Per-role reasoning effort (sync #17)  
5. Gate/skill validation (#14)  
6. Observability hooks (steer #20) + resume (#12) + Win11 pipes (#8)  
7. Multi-model bench data (#7)  
8. DRY prompt includes + local mode  

## Assigned Playbook (Engineering Mission)

**Orchestrator**: Read this mission + NARS head first. Use Zenith itself.

**Contract assertions** (write under mission/contract/):
- C1: All high-value jnorthrup deltas present and DRY (no duplication of upstream).
- C2: Open alternate PREs (#14, #17) either merged or cleanly rebased/adapted; #18 optional.
- C3: Issues #20, #8, #12, #7, #11 have either fixes or tracked tasks with targets.
- C4: NARS Therblig Index + Kolmogorov heuristic installed as first-class (skill or contract head renderer).
- C5: Tests (incl. Jules e2e) green; no contractreifier regression.
- C6: README/docs updated with DRY integration notes + agentic install still works.

**Task list sketch**:
1. work: Rebase/align tip onto latest upstream if needed; cherry-pick only value.
2. work: Promote NARS_ZENITH_THERBLIG_INDEX.md + heuristic into zenith/skills or core.
3. work: Ensure Jules provider + priority_respawn + attempt accounting are clean modules.
4. work: Port/adapt #17 reasoning effort + #14 gates if not already stronger.
5. work: Add observability stubs (#20) and Win11/resume diagnostics (#8/#12).
6. validate: Full pytest + Jules live probes + empty-contract + stderr drain regressions.
7. terminal: Review for DRY-ness, no bloat, confidence (f,c) on living specs.

**Generic heuristic**: Any wall-of-text >1 page → compress to Narsese 1-10 lines + memory label `zenith-dry-v1` / `mission-{{id}}-v1`.

## Next Actions
- Orchestrator: `submit_plan` with contracts above.
- Workers: implement per C1–C6.
- On completion: open PR from this branch to Intelligent-Internet/zenith (or keep as jnorthrup production review base).

**Memory labels installed**:
- `zenith-gap-analysis-2026-07-10`
- `zenith-pr-consumption-rate`
- `zenith-dry-integrate-mission-v1`
