---
name: smolagents_ghs_memory_context
description: >-
  Bounded memory_context procedure reverse-engineered (read-only) from
  https://github.com/huggingface/smolagents — scored 7.5/10 for free-tier utility.
source: https://github.com/huggingface/smolagents
generated_by: scouts/gh_repo_scout.py
generated_at: 2026-09-25T11:43:01Z
content_sha256: 1a01486480d82b1d
utility_score: 7.5
free_tier_efficiency: LOW
loop_risk_profile: LOW
safety_filter: PASSED
schema: schema/agent_entry_schema.json
---

# Skill: memory_context (smolagents_ghs)

## Source
- Repository: `https://github.com/huggingface/smolagents`
- Files mined: `README.md`, `docs/source/en/reference/agents.md`, `docs/source/ko/reference/agents.md`, `examples/plan_customization/README.md`
- Detected tool surface: `AgentAudio`, `AgentImage`, `AgentText`, `AgentType`, `CodeAgent`, `ManagedAgentPromptTemplate`, `MultiStepAgent`, `ToolCallingAgent`

## When to use
Use this skill when a task requires **memory_context** and the source agent's
pattern scores 7.5/10 on the almanac rubric
(scope & utility × free-tier overhead × loop-risk).

## Procedure
1. Confirm the goal maps to this capability before acting.
2. Preserve intent of source directive: "> You must have `gradio` installed to use the UI. Please run `pip install 'smolagents[gradio]'` if it's not the case."
3. Preserve intent of source directive: "You are an expert assistant who can solve any task using code blobs. You will be given a task to solve as best you can."
4. Preserve intent of source directive: "You are a world expert at analyzing a situation to derive facts, and plan accordingly towards solving a task."
5. Preserve intent of source directive: "You are a world expert at analyzing a situation, and plan accordingly towards solving a task."
6. Verify output with ONE check; halt on ambiguity.

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
