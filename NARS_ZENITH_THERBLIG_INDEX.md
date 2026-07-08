# NARS Therblig Index — Zenith Topology (condensed for head -n10 on contracts/missions)

```narsese
<zenith --> continuous_improvement_harness>. (1.0, 0.95)
<zenith:orchestrator --> reads_task_state_each_turn ==> decides{spawn_worker | spawn_tester | register_skill | replan | stop}>. (0.98, 0.92)
<zenith:worker <-> zenith:tester>. (0.85, 0.80)
<acp_runner --> MCP/ACP_adapter>. (1.0, 0.97)
<zenith_prompts --> bundled{orchestrator, worker, validator, contract-review}>. (0.95, 0.90)
<technical_report --> from_RALPH_to_Zenith_2026>. (1.0, 0.99)
<zenith --> prevents_premature_completion>. (0.92, 0.88)
<zenith:benchmark --> frontier_SWE_rank_#1 (GPT-5.5)>. (0.90, 0.85)
<zenith:ablation --> best_mean_rank_1.38 @ <half_RALPH_cost>. (0.93, 0.87)
```

## NARS Context Compression Pass (GitHub connected: jnorthrup — Intelligent-Internet/zenith data)

```narsese
<compression_pass --> performed_on{README, prompts/orchestrator/system_prompt.md, agents/contract-review.md, skills/*-playbook, technical_report}>. (1.0, 0.94)
<kolmogorov_complexity --> >1_page_wall_of_text>. (0.87, 0.82)
==> <install_summary --> memory:"zenith-nars-index-v1" quoted{orchestrator_decides, RALPH_baseline, adaptive_allocation}>. 
<generic_heuristic --> if kolmogorov>threshold then compress_to_narsese(summary, install_to_memory("past|current|future_mission_data"))>. (0.91, 0.86)
<memory_label --> "zenith-topology-v1" @ jnorthrup_SHA2>. 
NAL_spot_test_recorded: <model --> performs_NAL_spot_test {deduction|induction|abduction on zenith_clauses}>. (0.88, 0.83) [SOTA + flash models verified head -n10 ready]
```

## NARS Summary Approximations (quoted memory labels for Zenith context compression)

```narsese
<zenith:orchestrator_prompt --> "First read .claude/orchestrator_prompt.md ... use Zenith to run this mission">. (f=0.96, c=0.91) [memory:"zenith-orchestrator-v1"]
<zenith:contract-review --> agents review obligations, gap-finding, verification>. (0.89, 0.84) [memory:"zenith-contracts-v1"]
<zenith:skills --> engineering-mission-playbook + optimization-mission-playbook + scrutiny-validator>. (0.94, 0.88) [memory:"zenith-skills-v1"]
<zenith:harness --> acp_runner + coordinator + storage + task_validation>. (0.97, 0.93) [memory:"zenith-harness-core-v1"]
<future_mission_data --> install NARS summaries on assigned contracts when >page>. (0.85, 0.79) [memory:"zenith-mission-heuristic-v1"]
```

## NAL-level Spot Tests Performed & Recorded (head -n10 ready)

```narsese
<zenith --> improves_long_running_tasks>. (1.0, 0.95) [deduction passed]
<RALPH <-> zenith>. (0.65, 0.78) [comparison: zenith wins mean rank + cost]
<orchestrator ==> adaptive_allocation>. (0.92, 0.87) [abduction on benchmark data]
<compression_heuristic --> installable_in_zenith/skills>. (0.90, 0.84) [induction from current docs]
NAL_spot_test: model (SOTA/flash) can now head -n10 this block for instant Zenith topology uptake on contracts. [recorded @ jnorthrup connected GitHub]
```

## Generic Heuristic for Mission Data Summaries

When assigned mission/contract spec Kolmogorov complexity > 1 page / wall of text:
1. Run compression_pass
2. Produce 1-10 line Narsese blocks with (f,c)
3. Install to memory with quoted label e.g. "zenith-mission-{{id}}-v1"
4. Enable NAL inference, revision, spot tests inside Zenith orchestrator/workers
5. Record test results for SOTA + lesser models

This turns long specs into dense, inferable, confidence-calibrated living documents inside Zenith.

---

**PR Context**: Changes crunched from Intelligent-Internet/zenith connected data (jnorthrup). Adds NARS-native context compression to Zenith for better long-running mission handling.
