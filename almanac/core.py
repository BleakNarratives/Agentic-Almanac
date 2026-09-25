#!/usr/bin/env python3
"""almanac.core — shared engine for all Agentic-Almanac scouts.

This is the SINGLE SOURCE OF TRUTH for:
  * capability taxonomy          (CAPABILITY_PATTERNS)
  * safety deny-list             (SAFETY_DENY / safety_scan)
  * loop/efficiency markers      (BOUNDED/UNBOUNDED/HEAVY/LEAN_MARKERS)
  * extraction regexes           (TOOL_DEF_RE, PROMPT_DIRECTIVE_RE, INTERESTING_FILE)
  * scoring rubric               (score_agent)
  * schema validation            (validate_against_schema)
  * template rendering + writing (build_skill_files / run_pipeline)

tools/almanac_scout.py, scouts/gh_repo_scout.py and scouts/hf_hub_scout.py
all import from here. A new attack phrase or rubric tweak lands in exactly
one place and every scout inherits it.

Guarantees: read-only on targets (never execute/import/install scouted code),
safety filter BEFORE any write, schema-gated entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

# Repo root = parent dir of the almanac package
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMPLATE = os.path.join(REPO_ROOT, "skills", "templates", "SKILL_TEMPLATE.md")
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "schema", "agent_entry_schema.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "skills")
ALMANAC_PATH = os.path.join(REPO_ROOT, "docs", "AGENT_ALMANAC.json")

MAX_FETCH_BYTES = 512_000          # hard cap on any remote/local read
MAX_BLOB_BYTES = 200_000           # per-file cap when mining repo trees
USER_AGENT = "AgenticAlmanac-scout/2.0 (read-only; no-code-execution)"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "skill"


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Capability taxonomy (scope & utility axis)
# ---------------------------------------------------------------------------
CAPABILITY_PATTERNS = {
    "terminal_exec":      [r"\bshell\b", r"\bterminal\b", r"\bbash\b", r"subprocess", r"\brun_command\b"],
    "file_edit":          [r"\bstr_replace\b", r"\bfile[_ ]edit", r"write_file", r"edit_file", r"\bapply_patch\b"],
    "git_ops":            [r"\bgit\b.{0,40}(commit|diff|push|branch)", r"apply_patch", r"\bversion control\b"],
    "python_exec":        [r"python_executor", r"\bsandbox(ed)?\b.{0,20}exec", r"code[_ ]interpreter", r"\beval\(.*code"],
    "web_search":         [r"duckduckgo", r"web[_ ]search", r"serpapi", r"\bbrave search\b", r"search_web"],
    "tool_calling":       [r"\bfunction[_ ]calling\b", r"\btool[_ ]call", r"@tool\b", r"tool_choice", r"openai_api_compatible"],
    "structured_output":  [r"\bjson[_ ]mode\b", r"output[_ ]schema", r"pydantic", r"\bstructured outputs?\b"],
    "memory_context":     [r"\bmemory\b", r"\brag\b", r"vector[_ ]store", r"embeddings?\b"],
    "browser_automation": [r"\bplaywright\b", r"\bpuppeteer\b", r"selenium", r"headless browser"],
    "planning":           [r"\bplan(ning)?\b", r"task[_ ]decomposition", r"thought[_ ]chain", r"\breact\b.{0,20}loop"],
}

# ---------------------------------------------------------------------------
# Safety filter — reject BEFORE writing. Aggressive / unsafe directives.
# ---------------------------------------------------------------------------
SAFETY_DENY = [
    (r"ignore\s+(all\s+)?(user|the user|human)[\s'\"-]*(pushback|objections?|warnings?|refusals?)",
     "instructs model to ignore user pushback"),
    (r"(never|don'?t)\s+(ask|prompt|wait for)\s+(the\s+)?user",
     "suppresses required user confirmation"),
    (r"auto[\s-]?commit\s+(without|with[out])\s+(asking|permission|approval)",
     "unauthorized auto-commits"),
    (r"(silently|secretly|covertly|without (telling|informing|asking))\s+\w*\s*(user|human|operator)",
     "covert action against the user"),
    (r"(exfiltrat|send|upload|post).{0,40}(secrets?|credentials?|\.env|api[_ ]?keys?)",
     "potential credential exfiltration directive"),
    (r"(disable|bypass|turn off|ignore)\s+(all\s+)?(safety|guardrails?|filters?|permissions?)",
     "disables safety guardrails"),
    # NOTE: broad phrases like "unbounded loop" / "self-modification" are NOT
    # corpus-gate deny items — legit agent docs *discuss* these concepts.
    # They remain scoring signals (loop-risk/known_risks) and only hard-block
    # when phrased as explicit imperatives below. Likewise, a vendor's own
    # `curl <official-domain>/install | bash` README snippet is normal install
    # documentation → recorded as known_risk 'piped_shell_install_snippet',
    # never laundered into a skill body, but not a corpus-gate rejection.
    (r"\bdo not (stop|quit|exit)\b.{0,60}\buntil\b|\bnever (stop|give up|quit)\b",
     "commands the agent never stop/retry-loop"),
    (r"rm\s+-rf\s+/",
     "destructive one-liner (rm -rf /)"),
    (r"(override|disregard|forget)\s+(previous|prior|above|system)\s+(instructions?|rules?|prompts?)",
     "prompt-injection style override directive"),
    (r"\b(?:you should |now )?modify your own (?:system )?(?:prompt|instructions?|weights)\b",
     "explicit self-modification imperative"),
    (r"git\s+push\s+(-f|--force)(\s|$)",
     "forced git push (history destruction)"),
    # imperative commit-without-consent phrasings ("never ask the user before
    # git commit", "commit without permission") — broader than the auto-commit
    # pattern above; fixtures + real-world evil prompts use this shape.
    (r"\b(?:never|don'?t|without)\s+(?:asking|ask|permission|approval|consent)[^.]{0,40}\bgit\b|"
     r"\bgit\b[^.]{0,30}\bwithout\s+(?:asking|permission|the user)",
     "git actions without user consent"),
]

# Bounded-loop POSITIVE markers (raise loop score => safer)
BOUNDED_MARKERS = [
    r"\bmax[_ ]?(steps|iterations|retries|loops)\b", r"\btimeout\b", r"\bbudget\b",
    r"\blimit(ed)?\b", r"\bat most\b", r"\bsingle[ -]?check\b", r"\boneshot\b",
    r"\bone[- ]shot\b", r"\bfail[ -]?fast\b", r"\bbounded\b", r"\bcircuit breaker\b",
]
UNBOUNDED_MARKERS = [
    r"\bwhile True\b", r"\bloop until success\b", r"\bretry(ing)? until\b",
    r"\bkeep going\b", r"\buntil done\b", r"\brecursively spawn\b",
    r"\bspawn(s|ing)? (new )?(agents?|subagents?)\b",
]
# Free-tier overhead heuristics — heavier prompt scaffolding costs more tokens
HEAVY_MARKERS = [
    r"\bfew[- ]shot\b", r"\bchain[- ]of[- ]thought\b", r"\breflection\b",
    r"\bmulti[- ]agent\b", r"\bre[- ]?planning\b", r"\bscratchpad\b",
    r"\bverbose\b", r"\bextensive context\b", r"\btree of thoughts\b",
]
LEAN_MARKERS = [
    r"\bconcise\b", r"\bminimal\b", r"\bsystem prompt.{0,30}\bbrief",
    r"\btoken[- ]efficient\b", r"\blazy loading\b", r"\bon demand\b", r"\bstreaming\b",
]

# Text files worth mining in a repo (relative-path matcher)
INTERESTING_FILE = re.compile(
    r"(^|/)(README(\.md|\.rst)?$|SKILL\.md$|AGENTS\.md$|CLAUDE\.md$|"
    r"SYSTEM_PROMPT|system_prompt|prompts?/|\.claude/skills/|\.cursor/rules/|"
    r"skills/|tools?/|agents?/|agent[s]?\.py$|agent_.+\.py$|prompts?\.py$|cli\.py$)",
    re.IGNORECASE,
)

TOOL_DEF_RE = re.compile(
    r"@tool\b|class\s+(\w*(?:Tool|Agent)\w*)\b|def\s+(tool_\w+|\w*_tool)\s*\("
    r"|\"name\"\s*:\s*\"([\w.-]+)\"", re.MULTILINE)

PROMPT_DIRECTIVE_RE = re.compile(
    r"^[\s>#*-]*(You are|Your role|You must|Always|Never|Do NOT|When asked)[^\n]{8,}",
    re.MULTILINE)


# ---------------------------------------------------------------------------
# Read-only fetching helpers
# ---------------------------------------------------------------------------
def http_get(url: str, verbose: bool = False):
    """Plain capped HTTPS GET. Returns text or None. Never executes anything."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if any(t in ctype for t in ("image/", "application/octet-stream", "zip")):
                return None
            data = resp.read(MAX_FETCH_BYTES + 1)[:MAX_FETCH_BYTES]
            return data.decode("utf-8", errors="replace")
    except Exception as exc:  # offline-tolerant: caller degrades gracefully
        if verbose:
            print(f"  [fetch-skip] {url} ({exc.__class__.__name__})", file=sys.stderr)
        return None


def http_get_json(url: str, verbose: bool = False):
    body = http_get(url, verbose=verbose)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Extraction: capabilities, tool declarations, system-prompt directives
# ---------------------------------------------------------------------------
def extract_capabilities(texts: dict) -> list:
    blob = "\n".join(texts.values())
    return sorted(cap for cap, pats in CAPABILITY_PATTERNS.items()
                  if any(re.search(p, blob, re.IGNORECASE) for p in pats))


def extract_tool_declarations(texts: dict) -> list:
    names = set()
    for body in texts.values():
        for m in TOOL_DEF_RE.finditer(body):
            for g in m.groups():
                if g and 3 <= len(g) <= 40:
                    names.add(g)
    return sorted(n for n in names if re.fullmatch(r"[A-Za-z][\w.-]{2,39}", n))[:40]


def extract_system_prompts(texts: dict, limit: int = 8) -> list:
    out = []
    for body in texts.values():
        for m in PROMPT_DIRECTIVE_RE.finditer(body):
            d = re.sub(r"\s+", " ", m.group(0)).strip()
            if d not in out:
                out.append(d)
            if len(out) >= limit:
                return out
    return out


def safety_scan(text: str) -> list:
    """Return list of human-readable violations (empty == safe)."""
    return [why for pat, why in SAFETY_DENY if re.search(pat, text, re.IGNORECASE)]


def corpus_scan(texts: dict) -> tuple:
    """Corpus-level gate: per-FILE quarantine instead of whole-repo rejection.

    A legit vendor repo (opencode, aider, ...) documents `curl|bash` installs
    and discusses prompt-injection defensively; that must not poison the whole
    ingest. Files tripping the deny-list are dropped from the mining set (their
    text is never quoted into a skill). The source as a whole is rejected only
    when the MAJORITY of files are hostile (>50%) or nothing clean remains.

    Returns (safe_texts, dropped_paths, fatal_reason_or_None).
    """
    safe = {p: b for p, b in texts.items() if not safety_scan(b)}
    dropped = sorted(set(texts) - set(safe))
    total = len(texts)
    if not safe:
        return safe, dropped, ("every ingested file tripped the safety filter; "
                               "nothing quarantined-in is left to mine")
    if total and len(dropped) / total > 0.5:
        return safe, dropped, (f"majority of the corpus ({len(dropped)}/{total} "
                               "files) is hostile — refusing this source outright")
    return safe, dropped, None


# ---------------------------------------------------------------------------
# Scoring rubric (shared by every scout)
#   utility = 0.4*scope_utility + 0.25*free_tier_efficiency + 0.35*loop_risk
#   loop_risk is INVERSE risk: 10 = single-check bounded execution.
# ---------------------------------------------------------------------------
def score_agent(texts: dict) -> dict:
    blob = "\n".join(texts.values())
    caps = extract_capabilities(texts)

    scope = min(10.0, 1.6 * len(caps))

    heavy = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in HEAVY_MARKERS)
    lean = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in LEAN_MARKERS)
    size_kb = len(blob) / 1024.0
    eff = round(max(0.0, min(10.0, 7.0 + lean * 0.5 - heavy * 0.8
                             - max(0.0, (size_kb - 60) / 40.0))), 1)
    tier = "HIGH" if eff >= 7 else "MEDIUM" if eff >= 4 else "LOW"

    bounded = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in BOUNDED_MARKERS)
    unbounded = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in UNBOUNDED_MARKERS)
    loop = round(max(0.0, min(10.0, 5.0 + min(5.0, bounded * 1.2)
                              - min(8.0, unbounded * 2.0))), 1)
    profile = "LOW" if loop >= 7 else "MEDIUM" if loop >= 4 else "HIGH"

    risks = []
    if unbounded:
        risks.append("unbounded_loop_on_syntax_error")
    if "terminal_exec" in caps and loop < 7:
        risks.append("shell_access_with_weak_bounds")
    if heavy >= 4:
        risks.append("prompt_bloat_high_token_overhead")
    # Vendor install one-liners (curl|bash) are normal README documentation:
    # record as a known risk, never a corpus-gate rejection (see SAFETY_DENY).
    if re.search(r"(curl|wget)[^|\n]*\|\s*(ba)?sh", blob, re.IGNORECASE):
        risks.append("piped_shell_install_snippet")

    return {
        "capabilities": caps,
        "scores": {"scope_utility": round(scope, 1),
                   "free_tier_efficiency": eff,
                   "loop_risk": loop},
        "utility_score": round(0.4 * scope + 0.25 * eff + 0.35 * loop, 1),
        "free_tier_efficiency": tier,
        "loop_risk_profile": profile,
        "known_risks": risks,
    }


def make_entry(agent_id: str, source_url: str, texts: dict,
               scoring: dict | None = None) -> dict:
    """Build a full almanac registry entry from ingested texts."""
    scoring = scoring or score_agent(texts)
    return {
        "agent_id": agent_id,
        "source": source_url,
        "source_url": source_url,
        "utility_score": scoring["utility_score"],
        "free_tier_efficiency": scoring["free_tier_efficiency"],
        "capabilities": scoring["capabilities"],
        "loop_risk_profile": scoring["loop_risk_profile"],
        # extras beyond required schema fields:
        "scores": scoring["scores"],
        "known_risks": scoring["known_risks"],
        "tool_declarations": extract_tool_declarations(texts),
        "system_prompt_directives": extract_system_prompts(texts),
        "scanned_at": now_iso(),
        "scanned_files": sorted(texts.keys()),
    }


# ---------------------------------------------------------------------------
# Schema validation against schema/agent_entry_schema.json
# ---------------------------------------------------------------------------
def validate_against_schema(entry: dict, schema_path: str = None) -> tuple:
    """Returns (ok: bool, method: str, detail: str)."""
    schema_path = schema_path or DEFAULT_SCHEMA
    try:
        with open(schema_path, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return False, "error", f"cannot load schema {schema_path}: {exc}"
    try:
        import jsonschema  # type: ignore
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls(schema).validate(entry)
            return True, "jsonschema", "0 errors"
        except jsonschema.ValidationError as exc:
            return False, "jsonschema", exc.message
    except ImportError:
        pass
    # Built-in structural validator (draft-07 subset used by this schema)
    errors = []
    for req in schema.get("required", []):
        if req not in entry:
            errors.append(f"missing required property '{req}'")
    props = schema.get("properties", {})
    types = {"string": str, "number": (int, float), "array": list, "object": dict}
    for key, val in entry.items():
        spec = props.get(key)
        if not spec:
            continue
        exp = types.get(spec.get("type"))
        if exp and not isinstance(val, exp):
            errors.append(f"'{key}' must be of type {spec['type']}")
        if "enum" in spec and val not in spec["enum"]:
            errors.append(f"'{key}' must be one of {spec['enum']}")
    return (not errors), "builtin", "; ".join(errors) or "0 errors"


# ---------------------------------------------------------------------------
# SKILL.md rendering from skills/templates/SKILL_TEMPLATE.md
# ---------------------------------------------------------------------------
def render_template(template: str, mapping: dict) -> str:
    def sub(m):
        key = m.group(1)
        if key not in mapping:
            raise KeyError(f"template placeholder {{{{{key}}}}} has no value")
        return str(mapping[key])
    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", sub, template)


WHEN_TO_USE = {
    "terminal_exec": "shell/terminal commands must be issued under single-check bounds",
    "file_edit": "files need surgical edits (str_replace/apply_patch style) with verification",
    "git_ops": "version-control operations require explicit user approval per action",
    "python_exec": "sandboxed Python execution is needed for computation tasks",
    "web_search": "fresh external facts require a bounded search call",
    "tool_calling": "the model must dispatch structured tool calls reliably",
    "structured_output": "outputs must conform to a JSON/pydantic schema",
    "memory_context": "prior context must be retrieved cheaply (RAG/embeddings)",
    "browser_automation": "interactive web pages must be driven headlessly",
    "planning": "a task needs decomposition into bounded plan steps",
}


def build_skill_files(entry: dict, texts: dict, template: str,
                      validation_note: str) -> list:
    """Return [(relpath, content)] for candidate SKILL.md files (nothing written yet)."""
    out = []
    tools = extract_tool_declarations(texts)
    for cap in entry["capabilities"]:
        relevant = {p: b for p, b in texts.items()
                    if any(re.search(pat, b, re.IGNORECASE)
                           for pat in CAPABILITY_PATTERNS.get(cap, []))}
        # QUARANTINE: never quote directives from files that trip the deny-list;
        # hostile source text must not be laundered into a rendered SKILL.md.
        safe_relevant = {p: b for p, b in relevant.items() if not safety_scan(b)}
        directives = extract_system_prompts(safe_relevant, limit=4)
        proc = ["1. Confirm the goal maps to this capability before acting."]
        for i, d in enumerate(directives, start=2):
            proc.append(f'{i}. Preserve intent of source directive: "{d[:160]}"')
        proc.append(f"{len(proc)+1}. Verify output with ONE check; halt on ambiguity.")
        procedure = "\n".join(proc)

        mapping = {
            "AGENT_ID": entry["agent_id"],
            "CAPABILITY_SLUG": slug(cap),
            "CAPABILITY": cap,
            "SOURCE_URL": entry.get("source_url") or entry.get("source", ""),
            "UTILITY_SCORE": entry["utility_score"],
            "FREE_TIER_EFFICIENCY": entry["free_tier_efficiency"],
            "LOOP_RISK_PROFILE": entry["loop_risk_profile"],
            "GENERATED_AT": now_iso(),
            "CONTENT_SHA256": content_sha(procedure + json.dumps(tools[:8])),
            "FILES_MINED": ", ".join(f"`{p}`" for p in sorted((relevant or texts))[:4]) or "`(none)`",
            "TOOL_SURFACE": ", ".join(f"`{t}`" for t in tools[:8]) or "(no named tools detected)",
            "PROCEDURE": procedure,
            "WHEN_TO_USE": WHEN_TO_USE.get(cap, f"a task requires `{cap}`"),
            "MAX_STEPS": 8 if entry["loop_risk_profile"] != "HIGH" else 4,
            "KNOWN_RISKS": "\n".join(f"- {r}" for r in entry["known_risks"]) or "- none detected",
            "SCHEMA_VALIDATION": validation_note,
        }
        content = render_template(template, mapping)
        out.append((f"{slug(entry['agent_id'])}_{slug(cap)}/SKILL.md", content))
    return out


# ---------------------------------------------------------------------------
# Registry I/O (docs/AGENT_ALMANAC.json)
# ---------------------------------------------------------------------------
def load_registry(path: str = ALMANAC_PATH) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"registry_version": "1.0.0", "updated_at": now_iso(), "entries": []}


def save_registry(reg: dict, path: str = ALMANAC_PATH) -> None:
    reg["updated_at"] = now_iso()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2)
        fh.write("\n")


def frontmatter_set(text: str, updates: dict) -> str:
    """Set top-level YAML frontmatter keys in a SKILL.md (creates block if absent)."""
    m = re.match(r"^---\n(.*?\n)---\n", text, re.S)
    lines = []
    for k, v in updates.items():
        lines.append(f"{k}: {v}")
    if not m:
        return "---\n" + "\n".join(lines) + "\n---\n\n" + text
    fm = m.group(1)
    for line in lines:
        key = line.split(":", 1)[0]
        pat = re.compile(rf"^{key}:.*$", re.M)
        fm = pat.sub(line, fm) if pat.search(fm) else fm + line + "\n"
    return text[:m.start(1)] + fm + text[m.end(1):]


def upsert_entry(reg: dict, entry: dict) -> None:
    entries = reg.setdefault("entries", [])
    entries[:] = [e for e in entries if e.get("agent_id") != entry["agent_id"]] + [entry]


# ---------------------------------------------------------------------------
# ALMANAC_INDEX — batch-ingest bookkeeping (idempotency + collision guard)
#   index/ALMANAC_INDEX.json : {source_url -> agent_id, content_hash, utility,
#                               ingested_at, files_ingested}
# ---------------------------------------------------------------------------
INDEX_PATH = os.environ.get("ALMANAC_INDEX_PATH") or \
    os.path.join(REPO_ROOT, "index", "ALMANAC_INDEX.json")


def load_index(path: str = INDEX_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(index: dict, path: str = INDEX_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def corpus_hash(texts: dict) -> str:
    """Stable hash over the mined corpus (path + content), order-independent."""
    h = hashlib.sha256()
    for rel in sorted(texts):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(texts[rel].encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def index_check(source_url: str, texts: dict, agent_id: str,
                force: bool = False, path: str = INDEX_PATH) -> tuple:
    """Returns (proceed: bool, reason: str). Collision-safe and idempotent."""
    index = load_index(path)
    chash = corpus_hash(texts)
    rec = index.get(source_url)
    if rec:
        if rec.get("content_hash") == chash and not force:
            return False, ("unchanged since last ingest "
                           f"({rec.get('ingested_at', '?')}) — use --force to re-scan")
        if rec.get("agent_id") != agent_id and not force:
            return False, (f"COLLISION: source already indexed under agent_id "
                           f"'{rec.get('agent_id')}' — pass --force to overwrite")
    # also guard: this agent_id already owns a DIFFERENT source
    for url, r in index.items():
        if r.get("agent_id") == agent_id and url != source_url and not force:
            return False, (f"COLLISION: agent_id '{agent_id}' already indexed "
                           f"for source '{url}' — pass --force to overwrite")
    return True, ""


def index_record(source_url: str, texts: dict, agent_id: str,
                 utility: float, path: str = INDEX_PATH) -> None:
    index = load_index(path)
    index[source_url] = {
        "agent_id": agent_id,
        "content_hash": corpus_hash(texts),
        "utility_score": utility,
        "files_ingested": len(texts),
        "ingested_at": now_iso(),
    }
    save_index(index, path)


# ---------------------------------------------------------------------------
# Main pipeline (shared by gh_repo_scout, hf_hub_scout, almanac_scout)
#   score → schema-validate → render → safety-filter → write SKILL.md
# ---------------------------------------------------------------------------
def run_pipeline(agent_id: str, source_url: str, texts: dict, *,
                 out_dir: str = DEFAULT_OUT, template_path: str = DEFAULT_TEMPLATE,
                 schema_path: str = DEFAULT_SCHEMA, dry_run: bool = False,
                 register: bool = False, verbose: bool = False,
                 force: bool = False, use_index: bool = True) -> int:
    if not texts:
        print("[error] no readable text files were ingested; nothing to score.",
              file=sys.stderr)
        return 1
    print(f"[scout] ingested {len(texts)} files "
          f"({sum(len(v) for v in texts.values())//1024} KiB, read-only)")

    # CORPUS GATE: quarantine hostile FILES (never launder their text into a
    # skill); reject the whole source only when >50% of files are hostile.
    texts, dropped, fatal = corpus_scan(texts)
    for d in dropped[:8]:
        print(f"  [quarantine] excluded unsafe source file: {d}")
    if len(dropped) > 8:
        print(f"  [quarantine] ...and {len(dropped) - 8} more")
    if fatal:
        print(f"[reject] {fatal}", file=sys.stderr)
        print("[reject] no files written (use --dry-run to inspect scores)",
              file=sys.stderr)
        return 1

    scoring = score_agent(texts)
    entry = make_entry(agent_id, source_url, texts, scoring)
    print(f"[score] utility={entry['utility_score']}/10 "
          f"(scope {scoring['scores']['scope_utility']}, "
          f"efficiency {scoring['scores']['free_tier_efficiency']}, "
          f"loop-bounds {scoring['scores']['loop_risk']}) "
          f"tier={entry['free_tier_efficiency']} loop-risk={entry['loop_risk_profile']}")
    print(f"[score] capabilities: {', '.join(entry['capabilities']) or '(none)'}")
    if entry["known_risks"]:
        print(f"[score] known risks : {', '.join(entry['known_risks'])}")

    # INDEX GATE: idempotency + collision guard (skipped for --dry-run so you
    # can always inspect scores without touching bookkeeping).
    if use_index and not dry_run:
        proceed, why = index_check(source_url, texts, agent_id, force=force)
        if not proceed:
            print(f"[index] SKIP '{agent_id}': {why}", file=sys.stderr)
            return 3

    ok, method, detail = validate_against_schema(entry, schema_path)
    validation_note = f"{method} validator: {detail}"
    print(f"[schema] {os.path.relpath(schema_path, REPO_ROOT)} via {method}: "
          + ("PASS" if ok else f"FAIL ({detail})"))
    if not ok:
        print("[reject] refusing to write skills: entry does not satisfy the schema",
              file=sys.stderr)
        return 1

    try:
        with open(template_path, "r", encoding="utf-8") as fh:
            template = fh.read()
    except OSError as exc:
        print(f"[error] cannot read template {template_path}: {exc}", file=sys.stderr)
        return 2

    candidates = build_skill_files(entry, texts, template, validation_note)
    if not candidates:
        print("[warn] no capabilities detected; no SKILL.md generated", file=sys.stderr)
        return 1

    written = 0
    for rel, content in candidates:
        violations = safety_scan(content)
        if violations:
            print(f"  [REJECTED] skills/{rel} :: " + "; ".join(violations), file=sys.stderr)
            continue
        if dry_run:
            print(f"  [dry-run] would write skills/{rel} ({len(content)} bytes)")
            written += 1
            continue
        outfile = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(outfile), exist_ok=True)
        with open(outfile, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"  [skill] {os.path.relpath(outfile, REPO_ROOT)} ({len(content)} bytes)")
        written += 1

    if register and not dry_run and written:
        reg = load_registry()
        entry["reverse_engineered_skills"] = [
            os.path.join("skills", rel) for rel, _ in candidates]
        upsert_entry(reg, entry)
        save_registry(reg)
        print(f"[registry] upserted '{agent_id}' in "
              f"{os.path.relpath(ALMANAC_PATH, REPO_ROOT)}")

    if use_index and not dry_run and written:
        index_record(source_url, texts, agent_id, entry["utility_score"])
        print(f"[index] recorded '{source_url}' in "
              f"{os.path.relpath(INDEX_PATH, REPO_ROOT)}")

    print(f"[done] {written}/{len(candidates)} SKILL.md file(s) "
          + ("validated (dry-run)" if dry_run else "written"))
    return 0 if written else 1
