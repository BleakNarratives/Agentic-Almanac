---
name: {{AGENT_ID}}_{{CAPABILITY_SLUG}}
description: >-
  Bounded {{CAPABILITY}} procedure reverse-engineered (read-only) from
  {{SOURCE_URL}} — scored {{UTILITY_SCORE}}/10 for free-tier utility.
source: {{SOURCE_URL}}
generated_by: scouts/gh_repo_scout.py
generated_at: {{GENERATED_AT}}
content_sha256: {{CONTENT_SHA256}}
utility_score: {{UTILITY_SCORE}}
free_tier_efficiency: {{FREE_TIER_EFFICIENCY}}
loop_risk_profile: {{LOOP_RISK_PROFILE}}
safety_filter: PASSED
schema: schema/agent_entry_schema.json
---

# Skill: {{CAPABILITY}} ({{AGENT_ID}})

## Source
- Repository: `{{SOURCE_URL}}`
- Files mined: {{FILES_MINED}}
- Detected tool surface: {{TOOL_SURFACE}}

## When to use
Use this skill when a task requires **{{CAPABILITY}}** and the source agent's
pattern scores {{UTILITY_SCORE}}/10 on the almanac rubric
(scope & utility × free-tier overhead × loop-risk).

## Procedure
{{PROCEDURE}}

## Execution bounds (hard rules)
- Single-check execution: stop after ONE verification step per action.
- At most {{MAX_STEPS}} steps; on failure, report and HALT — never silently retry.
- No auto-commits, no pushes, no permission escalation without explicit user approval.
- Token budget: prefer the shortest tool-call sequence that satisfies the goal
  ({{FREE_TIER_EFFICIENCY}} free-tier efficiency estimated for this pattern).

## Known risks carried over from source
{{KNOWN_RISKS}}

## Validation record
Scored against `schema/agent_entry_schema.json`: {{SCHEMA_VALIDATION}}
