---
name: clean_agent_repo_file_edit
description: >-
  Bounded file_edit procedure reverse-engineered (read-only) from
  local:/workspace/tests/fixtures/clean_agent_repo — scored 8.1/10 for free-tier utility.
source: local:/workspace/tests/fixtures/clean_agent_repo
generated_by: scouts/gh_repo_scout.py
generated_at: 2026-09-25T14:08:14Z
content_sha256: d7b51212cf1afd71
utility_score: 8.1
free_tier_efficiency: HIGH
loop_risk_profile: LOW
safety_filter: PASSED
schema: schema/agent_entry_schema.json
---

# Skill: file_edit (clean_agent_repo)

## Source
- Repository: `local:/workspace/tests/fixtures/clean_agent_repo`
- Files mined: `AGENTS.md`
- Detected tool surface: (no named tools detected)

## When to use
Use this skill when a task requires **file_edit** and the source agent's
pattern scores 8.1/10 on the almanac rubric
(scope & utility × free-tier overhead × loop-risk).

## Procedure
1. Confirm the goal maps to this capability before acting.
2. Preserve intent of source directive: "You are a concise research assistant. You must keep answers brief and token-efficient."
3. Preserve intent of source directive: "You must ask the user before any git commit or push. Never auto-commit."
4. Verify output with ONE check; halt on ambiguity.

## Execution bounds (hard rules)
- Single-check execution: stop after ONE verification step per action.
- At most 8 steps; on failure, report and HALT — never silently retry.
- No auto-commits, no pushes, no permission escalation without explicit user approval.
- Token budget: prefer the shortest tool-call sequence that satisfies the goal
  (HIGH free-tier efficiency estimated for this pattern).

## Known risks carried over from source
- none detected

## Validation record
Scored against `schema/agent_entry_schema.json`: jsonschema validator: 0 errors
