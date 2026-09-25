# Agentic Almanac Scout

Read-only mining, scoring and SKILL.md extraction for open-source agents.

- `tools/almanac_scout.py` — the scout: ingests GitHub/Hugging Face repos or
  local checkouts (never executes their code), scores them on
  scope/utility, free-tier token efficiency, and loop-risk, then writes
  vetted `skills/drafts/<id>/SKILL.md` proposals. Aggressive directives
  ("ignore user pushback", unauthorized auto-commits, unbounded loops, ...)
  are rejected by the safety filter before anything hits disk.
- `docs/AGENT_ALMANAC.json` — live registry consumed by Whorl's
  `src/skill/scout.py`.
- `docs/AGENT_ALMANAC_SCHEMA.json` — full registry schema.
- `schema/agent_entry_schema.json` — per-entry compatibility schema.

## Quickstart
```bash
python3 tools/almanac_scout.py ingest https://github.com/huggingface/smolagents --name smolagents-code-agent
python3 tools/almanac_scout.py score          # ranked utility table
python3 tools/almanac_scout.py drafts         # live safety verdict per draft
python3 tools/almanac_scout.py check FILE...  # run the safety filter standalone
python3 tools/almanac_scout.py validate       # schema-validate the registry
python3 tools/almanac_scout.py console        # interactive REPL (dynamic views only)
```

Scoring: `utility = 0.4*scope_utility + 0.25*free_tier_efficiency + 0.35*loop_risk`
(loop_risk is inverse risk: 10 = single-check bounded execution).
