---
name: smolagents_code_agent_web_search
description: Bounded web_search procedure reverse-engineered from https://github.com/huggingface/smolagents
source: https://github.com/huggingface/smolagents
generated_by: almanac_scout/1.0
generated_at: 2026-09-25T11:36:20Z
content_sha256: 0c9b164adb0bf73d
utility_score: 9.4
free_tier_efficiency: HIGH
loop_risk_profile: LOW
safety_filter: PASSED
---

# web_search skill (smolagents-code-agent)

Reverse-engineered procedure extracted (read-only) from `https://github.com/huggingface/smolagents`.
This draft is compliant with Whorl AGENTS.md §6 skill format and is
loadable by `src/skill/scout.py` once promoted from `skills/drafts/`.

## When to use
Use when a task requires `web_search` and the source agent `smolagents-code-agent` pattern scored 9.4/10.

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
- none detected
