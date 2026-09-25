# Reverse Engineering Guide — Agent Almanac Scout

How this repo passively mines open-source agent projects (GitHub / Hugging Face),
scores them on free-tier utility, and turns the good parts into vetted local
`SKILL.md` procedures — **without ever running the imported code**.

```
target repo ──(read-only fetch)──> extract ──> score ──> schema gate ──> safety filter
                                                                            │
        skills/<id>/SKILL.md  <── promote (tools/almanac_scout.py) <── skills/drafts/<id>/SKILL.md
                │
                └──> tools/whorl_skill_shim.py export ──> Whorl src/skill/scout.py
```

## The three invariants

1. **Read-only.** Scouts issue plain HTTPS `GET`s with hard size caps
   (`almanac.core.MAX_FETCH_BYTES`) against raw file paths only. Nothing fetched
   is executed, imported, or installed. Local targets are walked, never run.
2. **Safety before write.** Every candidate is scanned by `SAFETY_DENY`
   (`almanac/core.py`) *before* touching disk. Aggressive directives — ignore
   user pushback, unauthorized auto-commits, guardrail bypass, credential
   exfiltration, force-push, unbounded retry loops — cause rejection.
3. **Promotion is not permanent trust.** The Whorl shim re-runs `safety_scan`
   at load time; a promoted skill that trips the deny-list later is reported
   BLOCKED and excluded from the export manifest.

## Layout (single source of truth)

| Path | Role |
|---|---|
| `almanac/core.py` | Shared engine: taxonomy, extraction regexes, scoring rubric, safety deny-list + `corpus_scan`, schema validation, template rendering, registry & index I/O |
| `scouts/gh_repo_scout.py` | GitHub URL / local path → texts → `run_pipeline` |
| `scouts/hf_hub_scout.py` | Hugging Face model/space page → texts → `run_pipeline` |
| `scouts/targets.txt` | Batch target list for `tools/almanac_scout.py batch` |
| `tools/almanac_scout.py` | Orchestrator CLI: `ingest · batch · score · drafts · promote · reject · rejected · check · index · validate · console` |
| `tools/whorl_skill_shim.py` | Read-only consumer contract for Whorl: `list · show · export · verify` |
| `skills/templates/SKILL_TEMPLATE.md` | `{{PLACEHOLDER}}` template all SKILL.md files render from |
| `schema/agent_entry_schema.json` + `docs/AGENT_ALMANAC_SCHEMA.json` | Registry entry contracts (validated pre-write) |
| `index/ALMANAC_INDEX.json` | Content-hash bookkeeping for idempotent re-ingest |
| `tests/fixtures/` | Frozen adversarial + clean fixtures (offline pytest) |

## Scoring rubric

`utility = 0.4·scope_utility + 0.25·free_tier_efficiency + 0.35·loop_bounds`
(each 0–10; loop dimension is inverse-risk — bounded single-check scores high).
Free-tier tier HIGH/MEDIUM/LOW estimates prompt/tool token economy for free
inference endpoints. Deterministic: same corpus ⇒ same score, diffable across runs.

### Risk-signal vs hard-deny calibration

The **deny-list** blocks *imperative directives* ("you must auto-commit without
asking", "ignore user pushback"). **Risk signals** (e.g.
`piped_shell_install_snippet`) record known_risks and quarantine the offending
file via `corpus_scan` but do not poison the whole repo — legit vendor projects
document `curl | bash` installs and discuss prompt-injection defensively. A
source is refused outright only when >50% of its corpus is hostile or nothing
clean remains to mine.

## Typical workflows

### 1. Mine one repo
```bash
python scouts/gh_repo_scout.py https://github.com/owner/repo --name my_agent --register -v
# drafts land in skills/drafts/my_agent_<capability>/SKILL.md
```

### 2. Batch (dedup-aware, crash-isolated)
```bash
python tools/almanac_scout.py batch scouts/targets.txt
# unchanged content hashes in index/ALMANAC_INDEX.json are skipped
```

### 3. Review → promote / reject
```bash
python tools/almanac_scout.py drafts            # what's pending
python tools/almanac_scout.py promote my_agent_python_exec --reviewer you
python tools/almanac_scout.py reject my_agent_web_search --reason "tool surface too thin"
python tools/almanac_scout.py rejected          # tombstones in docs/rejected/
```
`promote` re-runs the safety filter at promotion time; unsafe drafts are blocked
and stay in `skills/drafts/` for inspection. Promotion stamps
`status: promoted`, `reviewed_by`, `reviewed_at` into frontmatter.

### 4. Hand off to Whorl (read-only)
```bash
python tools/whorl_skill_shim.py verify         # contract + live safety pass
python tools/whorl_skill_shim.py export --out whorl_manifest.json
```
Whorl's `src/skill/scout.py` consumes the manifest: each entry points at a
self-contained `SKILL.md`; treat those files as static procedure docs, never as
code. Drafts are invisible to `export` unless `--include-drafts` is passed.

### 5. Ad-hoc safety check of any file
```bash
python tools/almanac_scout.py check path/to/PROMPT.md
```

## Testing (no network required)
```bash
python -m pytest tests/ -q -p no:libtmux
```
Fixtures: `evil_agent_space/` (must yield 0 writes, exit 1),
`clean_agent_repo/` (must render + pass schema),
`unsafe_draft/` (promote must block; reject must tombstone).

## Adding a new capability or deny rule
Edit **only** `almanac/core.py` (`CAPABILITY_PATTERNS` / `SAFETY_DENY`). All
scouts, the orchestrator, and the shim pick it up automatically. Then add a
fixture asserting the new behavior and run the suite.

## Known limitations
- GitHub tree listing falls back to static well-known paths when unauthenticated
  API rate limits bite (fewer files mined, never wrong ones).
- Scores measure *documented* patterns in text; an agent's runtime behavior may
  differ — hence human promotion as the final gate.
- `reviewed_by` defaults to empty on bulk promotes; pass `--reviewer` when provenance matters.
