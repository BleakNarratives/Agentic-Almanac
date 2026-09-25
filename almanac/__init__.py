"""almanac — shared, read-only engine for the Agentic-Almanac scouts.

Single source of truth for: capability taxonomy, safety deny-list, scoring
rubric, extraction regexes, schema validation, template rendering, and the
SKILL.md write pipeline. Imported by tools/almanac_scout.py,
scouts/gh_repo_scout.py and scouts/hf_hub_scout.py so that a new attack
pattern or rubric change lands in exactly ONE place.

Guarantees inherited by all consumers:
  * READ-ONLY on scouted targets (plain capped HTTPS GETs / local walks;
    target code is never executed, imported, or installed).
  * SAFETY FILTER BEFORE WRITE (reject aggressive directives pre-disk).
  * SCHEMA-GATED writes against schema/agent_entry_schema.json.
"""

from almanac.core import (  # noqa: F401
    REPO_ROOT,
    MAX_FETCH_BYTES,
    USER_AGENT,
    CAPABILITY_PATTERNS,
    SAFETY_DENY,
    BOUNDED_MARKERS,
    UNBOUNDED_MARKERS,
    HEAVY_MARKERS,
    LEAN_MARKERS,
    INTERESTING_FILE,
    TOOL_DEF_RE,
    PROMPT_DIRECTIVE_RE,
    now_iso,
    slug,
    content_sha,
    http_get,
    http_get_json,
    extract_capabilities,
    extract_tool_declarations,
    extract_system_prompts,
    safety_scan,
    score_agent,
    validate_against_schema,
    render_template,
    build_skill_files,
    make_entry,
    run_pipeline,
)
