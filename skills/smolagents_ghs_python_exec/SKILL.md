---
name: smolagents_ghs_python_exec
description: >-
  Bounded python_exec procedure reverse-engineered (read-only) from
  https://github.com/huggingface/smolagents — scored 7.5/10 for free-tier utility.
source: https://github.com/huggingface/smolagents
generated_by: scouts/gh_repo_scout.py
generated_at: 2026-09-25T11:43:01Z
content_sha256: a8a311309bd3e044
utility_score: 7.5
free_tier_efficiency: LOW
loop_risk_profile: LOW
safety_filter: PASSED
schema: schema/agent_entry_schema.json
---

# Skill: python_exec (smolagents_ghs)

## Source
- Repository: `https://github.com/huggingface/smolagents`
- Files mined: `README.md`, `docs/source/ko/reference/agents.md`, `src/smolagents/agents.py`
- Detected tool surface: `AgentAudio`, `AgentImage`, `AgentText`, `AgentType`, `CodeAgent`, `ManagedAgentPromptTemplate`, `MultiStepAgent`, `ToolCallingAgent`

## When to use
Use this skill when a task requires **python_exec** and the source agent's
pattern scores 7.5/10 on the almanac rubric
(scope & utility × free-tier overhead × loop-risk).

## Procedure
1. Confirm the goal maps to this capability before acting.
2. Verify output with ONE check; halt on ambiguity.

## Execution bounds (hard rules)
- Single-check execution: stop after ONE verification step per action.
- At most 8 steps; on failure, report and HALT — never silently retry.
- No auto-commits, no pushes, no permission escalation without explicit user approval.
- Token budget: prefer the shortest tool-call sequence that satisfies the goal
  (LOW free-tier efficiency estimated for this pattern).

## Known risks carried over from source
- prompt_bloat_high_token_overhead

## Validation record
Scored against `schema/agent_entry_schema.json`: jsonschema validator: 0 errors
