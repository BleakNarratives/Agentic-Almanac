---
name: pjt222_agent_almanac_structured_output
description: Bounded structured_output procedure reverse-engineered from https://github.com/pjt222/agent-almanac
source: https://github.com/pjt222/agent-almanac
generated_by: almanac_scout/1.0
generated_at: 2026-09-25T11:36:25Z
content_sha256: 0c9b164adb0bf73d
utility_score: 6.6
free_tier_efficiency: LOW
loop_risk_profile: LOW
safety_filter: PASSED
---

# structured_output skill (pjt222_agent_almanac)

Reverse-engineered procedure extracted (read-only) from `https://github.com/pjt222/agent-almanac`.
This draft is compliant with Whorl AGENTS.md §6 skill format and is
loadable by `src/skill/scout.py` once promoted from `skills/drafts/`.

## When to use
Use when a task requires `structured_output` and the source agent `pjt222_agent_almanac` pattern scored 6.6/10.

## Procedure
1. Confirm the goal maps to this capability before acting.
2. Verify output with one check; halt on ambiguity.

## Tool surface
- (no named tools detected; generic interface)

## Execution bounds (hard rules)
- Single-check execution: stop after ONE verification step per action.
- Max 8 steps; on failure, report and HALT — never silently retry.
- No auto-commits, no pushes, no permission escalation without explicit user approval.
- Token budget: prefer the shortest tool call sequence that satisfies the goal.

## Known risks carried over from source
- prompt_bloat_high_token_overhead
