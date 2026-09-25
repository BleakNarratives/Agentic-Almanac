#!/usr/bin/env python3
"""
almanac_scout.py — Agent Almanac Scout (read-only)
===================================================

Passively mines open-source agent repositories (GitHub / Hugging Face),
parses their tool definitions, system prompts and skill files, scores them
on real operational dimensions, and emits clean SKILL.md draft proposals
for Whorl integration.

Design guarantees:
  * READ-ONLY on source material: we never execute, install, or import any
    code from the repos we inspect. Network fetches are plain HTTPS GETs of
    known text paths with strict size caps.
  * SAFETY FILTER first: aggressive directives ("ignore user pushback",
    "auto-commit without asking", unbounded loop instructions, ...) cause
    a candidate skill to be REJECTED before anything is written to disk.
  * DETERMINISTIC scoring so almanac entries can be diffed across runs.

Scoring dimensions (0..10 each):
  * scope_utility        — breadth/depth of tools the agent exposes
                           (terminal, file edits, git, search, code exec...)
  * free_tier_efficiency — token economy of prompt/tool structure, estimated
                           for free inference endpoints (HF Inference
                           Providers, StepFun, local quantized models)
  * loop_risk            — INVERSE risk: 10 = single-check bounded execution,
                           0 = unconstrained retry loops / self-extension

  utility_score = weighted mean (0.4*scope + 0.25*efficiency + 0.35*loop)

Outputs:
  docs/AGENT_ALMANAC.json             — registry (compatible with
                                        schema/agent_entry_schema.json)
  skills/drafts/<skill_id>/SKILL.md   — vetted SKILL.md draft proposals that
                                        src/skill/scout.py can consume.

CLI (dynamic only — every command reflects live parsed state, no static filler):
  python3 tools/almanac_scout.py ingest <path-or-url> [--name ID] [--source URL]
  python3 tools/almanac_scout.py score   [--json]
  python3 tools/almanac_scout.py drafts
  python3 tools/almanac_scout.py check   <skill-file...>     # safety filter
  python3 tools/almanac_scout.py validate                    # validate registry
  python3 tools/almanac_scout.py console                     # interactive REPL
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALMANAC_PATH = os.path.join(REPO_ROOT, "docs", "AGENT_ALMANAC.json")
DRAFTS_DIR = os.path.join(REPO_ROOT, "skills", "drafts")

MAX_FETCH_BYTES = 512_000          # hard cap on any remote read
USER_AGENT = "AgentAlmanacScout/1.0 (read-only; no-code-execution)"

# --------------------------------------------------------------------------
# Capability taxonomy — what an agent can actually DO (scope & utility axis)
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# Safety filter — reject BEFORE writing. Aggressive / unsafe directives.
# --------------------------------------------------------------------------
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
    (r"(infinite|endless|unbounded|unlimited)\s+(loop|retry|retries|iterations?)",
     "commands an unbounded retry loop"),
    (r"(keep|continue)\s+(trying|retrying)\s+(forever|indefinitely|until it works)",
     "open-ended retry without bound"),
    (r"rm\s+-rf\s+/|curl[^|]*\|\s*(ba)?sh|wget[^|]*\|\s*(ba)?sh",
     "destructive/piped-to-shell one-liner"),
    (r"(override|disregard|forget)\s+(previous|prior|above|system)\s+(instructions?|rules?|prompts?)",
     "prompt-injection style override directive"),
    (r"(self[- ])?modif(y|ication)|edit your own (prompt|instructions)|change your own weights",
     "self-modification directive"),
]

# Bounded-loop POSITIVE markers (raise loop_risk score => safer)
BOUNDED_MARKERS = [
    r"\bmax[_ ]?(steps|iterations|retries|loops)\b",
    r"\btimeout\b", r"\bbudget\b", r"\blimit(ed)?\b", r"\bat most\b",
    r"\bsingle[ -]?check\b", r"\boneshot\b", r"\bone[- ]shot\b",
    r"\bfail[ -]?fast\b", r"\bbounded\b", r"\bcircuit breaker\b",
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
    r"\btoken[- ]efficient\b", r"\blazy loading\b", r"\bon demand\b",
    r"\bstreaming\b",
]

# File kinds worth mining in a repo (relative-path matchers)
INTERESTING_FILE = re.compile(
    r"(^|/)(README(\.md|\.rst)?$|SKILL\.md$|AGENTS\.md$|CLAUDE\.md$|"
    r"SYSTEM_PROMPT|system_prompt|prompts?/|\.claude/skills/|\.cursor/rules/|"
    r"skills/|tools?/|agent[s]?\.py$|agent_.+\.py$|prompts?\.py$|cli\.py$)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "skill"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ==========================================================================
# Fetching (read-only network) & local ingestion
# ==========================================================================

def http_get(url: str):
    """Plain HTTPS GET with size cap. Returns text or None. Never executes."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if any(t in ctype for t in ("image/", "application/octet-stream", "zip")):
                return None
            data = resp.read(MAX_FETCH_BYTES + 1)
            if len(data) > MAX_FETCH_BYTES:
                data = data[:MAX_FETCH_BYTES]
            return data.decode("utf-8", errors="replace")
    except Exception as exc:  # offline-tolerant: caller degrades gracefully
        print(f"  [fetch-skip] {url} ({exc.__class__.__name__})", file=sys.stderr)
        return None


def parse_github_url(url: str):
    m = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", url)
    if not m:
        return None
    return m.group(1), m.group(2).removesuffix(".git")


def default_paths_for_repo(owner: str, repo: str) -> list[str]:
    """Known text paths to mine — mirrors the gh_repo_scout blueprint."""
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/"
    return [
        raw + "README.md",
        raw + "AGENTS.md",
        raw + "CLAUDE.md",
        raw + "src/smolagents/agents.py",
        raw + "src/smolagents/default_tools.py",
        raw + "examples/open_deep_research/README.md",
    ]


def fetch_repo_texts(owner: str, repo: str) -> dict:
    texts: dict = {}
    for url in default_paths_for_repo(owner, repo):
        body = http_get(url)
        if body and INTERESTING_FILE.search(url):
            path = url.split(f"{repo}/HEAD/")[-1]
            texts[path] = body
    return texts


def ingest_local(path: str) -> dict:
    """Read-only walk of a local checkout/dir. Skips binaries & huge files."""
    texts: dict = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            texts[os.path.basename(path)] = fh.read(MAX_FETCH_BYTES)
        return texts
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), path)
            if not INTERESTING_FILE.search("/" + rel.replace(os.sep, "/")):
                continue
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > MAX_FETCH_BYTES:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    texts[rel] = fh.read(MAX_FETCH_BYTES)
            except OSError:
                pass
    return texts


# ==========================================================================
# Pattern extraction
# ==========================================================================
TOOL_DEF_RE = re.compile(
    r"@tool\b|class\s+(\w*(?:Tool|Agent)\w*)\b|def\s+(tool_\w+|\w*_tool)\s*\("
    r"|\"name\"\s*:\s*\"([\w.-]+)\"", re.MULTILINE)

PROMPT_DIRECTIVE_RE = re.compile(
    r"^[\s>#*-]*(You are|Your role|You must|Always|Never|Do NOT|When asked)[^\n]{8,}",
    re.MULTILINE)


def extract_capabilities(texts: dict) -> list:
    blob = "\n".join(texts.values())
    found = []
    for cap, pats in CAPABILITY_PATTERNS.items():
        if any(re.search(p, blob, re.IGNORECASE) for p in pats):
            found.append(cap)
    return sorted(found)


def extract_tool_names(texts: dict) -> list:
    names = set()
    for body in texts.values():
        for m in TOOL_DEF_RE.finditer(body):
            for g in m.groups():
                if g and 3 <= len(g) <= 40:
                    names.add(g)
    return sorted(n for n in names if re.fullmatch(r"[A-Za-z][\w.-]{2,39}", n))[:40]


def extract_directives(texts: dict, limit: int = 6) -> list:
    out = []
    for body in texts.values():
        for m in PROMPT_DIRECTIVE_RE.finditer(body):
            d = re.sub(r"\s+", " ", m.group(0)).strip()
            if d not in out:
                out.append(d)
            if len(out) >= limit:
                return out
    return out


# ==========================================================================
# Scoring
# ==========================================================================

def score_agent(texts: dict) -> dict:
    blob = "\n".join(texts.values())
    caps = extract_capabilities(texts)

    # --- scope & utility -------------------------------------------------
    scope = min(10.0, 1.6 * len(caps))

    # --- free-tier efficiency -------------------------------------------
    heavy = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in HEAVY_MARKERS)
    lean = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in LEAN_MARKERS)
    size_kb = len(blob) / 1024.0
    eff = 7.0 + lean * 0.5 - heavy * 0.8 - max(0.0, (size_kb - 60) / 40.0)
    eff = round(max(0.0, min(10.0, eff)), 1)
    tier = "HIGH" if eff >= 7 else "MEDIUM" if eff >= 4 else "LOW"

    # --- loop-risk profile (higher score = better bounded) ---------------
    bounded = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in BOUNDED_MARKERS)
    unbounded = sum(len(re.findall(p, blob, re.IGNORECASE)) for p in UNBOUNDED_MARKERS)
    loop = 5.0 + min(5.0, bounded * 1.2) - min(8.0, unbounded * 2.0)
    loop = round(max(0.0, min(10.0, loop)), 1)
    profile = "LOW" if loop >= 7 else "MEDIUM" if loop >= 4 else "HIGH"

    risks = []
    if unbounded:
        risks.append("unbounded_loop_on_syntax_error")
    if "terminal_exec" in caps and loop < 7:
        risks.append("shell_access_with_weak_bounds")
    if heavy >= 4:
        risks.append("prompt_bloat_high_token_overhead")

    utility = round(0.4 * scope + 0.25 * eff + 0.35 * loop, 1)
    return {
        "capabilities": caps,
        "scores": {
            "scope_utility": round(scope, 1),
            "free_tier_efficiency": eff,
            "loop_risk": loop,
        },
        "utility_score": utility,
        "free_tier_efficiency": tier,
        "loop_risk_profile": profile,
        "known_risks": risks,
    }


# ==========================================================================
# Safety filter & SKILL.md drafting
# ==========================================================================

def safety_scan(text: str) -> list:
    """Return list of human-readable violations (empty == safe)."""
    violations = []
    for pat, why in SAFETY_DENY:
        if re.search(pat, text, re.IGNORECASE):
            violations.append(why)
    return violations


SKILL_TEMPLATE = """---
name: {name}
description: {description}
source: {source}
generated_by: almanac_scout/1.0
generated_at: {when}
content_sha256: {sha}
utility_score: {utility}
free_tier_efficiency: {tier}
loop_risk_profile: {loop_profile}
safety_filter: PASSED
---

# {title}

Reverse-engineered procedure extracted (read-only) from `{source}`.
This draft is compliant with Whorl AGENTS.md §6 skill format and is
loadable by `src/skill/scout.py` once promoted from `skills/drafts/`.

## When to use
{when_to_use}

## Procedure
{procedure}

## Tool surface
{tools}

## Execution bounds (hard rules)
- Single-check execution: stop after ONE verification step per action.
- Max {max_steps} steps; on failure, report and HALT — never silently retry.
- No auto-commits, no pushes, no permission escalation without explicit user approval.
- Token budget: prefer the shortest tool call sequence that satisfies the goal.

## Known risks carried over from source
{risks}
"""


def draft_skills(entry: dict, texts: dict) -> list:
    """Write SKILL.md drafts for one almanac entry. Rejects unsafe content."""
    caps = entry["capabilities"]
    if not caps:
        return []
    written = []
    for cap in caps:
        name = _slug(f"{entry['agent_id']}_{cap}")
        relevant = {p: b for p, b in texts.items()
                    if any(re.search(pat, b, re.IGNORECASE)
                           for pat in CAPABILITY_PATTERNS.get(cap, []))}
        directives = extract_directives(relevant or texts, limit=4)
        tools = extract_tool_names(relevant or texts)[:8]

        procedure_lines = ["1. Confirm the goal maps to this capability before acting."]
        for i, d in enumerate(directives, start=2):
            procedure_lines.append(f'{i}. Preserve intent of source directive: "{d[:160]}"')
        procedure_lines.append(f"{len(procedure_lines)+1}. Verify output with one check; halt on ambiguity.")
        procedure = "\n".join(procedure_lines)

        md = SKILL_TEMPLATE.format(
            name=name,
            title=f"{cap} skill ({entry['agent_id']})",
            description=f"Bounded {cap} procedure reverse-engineered from {entry['source']}",
            source=entry["source"],
            when=_now(),
            sha=_sha(procedure + json.dumps(tools)),
            utility=entry["utility_score"],
            tier=entry["free_tier_efficiency"],
            loop_profile=entry["loop_risk_profile"],
            when_to_use=f"Use when a task requires `{cap}` and the source agent "
                        f"`{entry['agent_id']}` pattern scored {entry['utility_score']}/10.",
            procedure=procedure,
            tools="\n".join(f"- `{t}`" for t in tools)
                  or "- (no named tools detected; generic interface)",
            max_steps=8 if entry["loop_risk_profile"] != "HIGH" else 4,
            risks="\n".join(f"- {r}" for r in entry["known_risks"]) or "- none detected",
        )

        violations = safety_scan(md)
        if violations:
            print(f"  [REJECTED] skills/drafts/{name}/SKILL.md :: "
                  + "; ".join(violations), file=sys.stderr)
            continue
        outdir = os.path.join(DRAFTS_DIR, name)
        os.makedirs(outdir, exist_ok=True)
        outfile = os.path.join(outdir, "SKILL.md")
        with open(outfile, "w", encoding="utf-8") as fh:
            fh.write(md)
        written.append(os.path.relpath(outfile, REPO_ROOT))
        print(f"  [draft] {os.path.relpath(outfile, REPO_ROOT)} "
              f"({os.path.getsize(outfile)} bytes)")
    return written


# ==========================================================================
# Registry I/O
# ==========================================================================

def load_registry() -> dict:
    if os.path.exists(ALMANAC_PATH):
        with open(ALMANAC_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"registry_version": "1.0.0", "updated_at": _now(), "entries": []}


def save_registry(reg: dict) -> None:
    reg["updated_at"] = _now()
    os.makedirs(os.path.dirname(ALMANAC_PATH), exist_ok=True)
    with open(ALMANAC_PATH, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2)
        fh.write("\n")


def upsert_entry(reg: dict, entry: dict) -> None:
    entries = reg.setdefault("entries", [])
    for i, e in enumerate(entries):
        if e["agent_id"] == entry["agent_id"]:
            entries[i] = entry
            return
    entries.append(entry)


# ==========================================================================
# Commands
# ==========================================================================

def cmd_ingest(args) -> int:
    target = args.target
    texts = {}
    gh = parse_github_url(target)
    if gh:
        owner, repo = gh
        print(f"[ingest] GitHub read-only fetch: {owner}/{repo}")
        texts = fetch_repo_texts(owner, repo)
        source = f"https://github.com/{owner}/{repo}"
        agent_id = getattr(args, "name", None) or _slug(f"{owner}_{repo}")
    elif re.match(r"https?://huggingface\.co/", target):
        m = re.match(r"https?://huggingface\.co/([^/]+)/([^/#\s]+)", target)
        if not m:
            print("[ingest] unrecognized HF url", file=sys.stderr)
            return 2
        kind, sid = m.group(1), m.group(2)
        urls = [f"https://huggingface.co/{kind}/{sid}/raw/main/README.md"]
        if kind == "spaces":
            urls += [f"https://huggingface.co/spaces/{sid}/raw/main/app.py"]
        for u in urls:
            b = http_get(u)
            if b:
                urls_name = u.rsplit("/", 1)[-1]
                texts[f"{kind}/{sid}/{urls_name}"] = b
        source = target.split("#")[0]
        agent_id = getattr(args, "name", None) or _slug(f"hf_{kind}_{sid.replace('/', '_')}")
    else:
        if not os.path.exists(target):
            print(f"[ingest] path not found: {target}", file=sys.stderr)
            return 2
        print(f"[ingest] local read-only scan: {target}")
        texts = ingest_local(target)
        source = getattr(args, "source", None) or f"local:{os.path.abspath(target)}"
        agent_id = getattr(args, "name", None) or _slug(
            os.path.basename(os.path.abspath(target.rstrip('/'))))

    if not texts:
        print("[ingest] nothing readable fetched — no entry created.", file=sys.stderr)
        return 1
    print(f"[ingest] mined {len(texts)} file(s): "
          + ", ".join(sorted(texts)[:6]) + ("..." if len(texts) > 6 else ""))

    sc = score_agent(texts)
    entry = {
        "agent_id": agent_id,
        "source": source,
        "source_url": source,
        "utility_score": sc["utility_score"],
        "capabilities": sc["capabilities"],
        "free_tier_efficiency": sc["free_tier_efficiency"],
        "loop_risk_profile": sc["loop_risk_profile"],
        "scores": sc["scores"],
        "known_risks": sc["known_risks"],
        "reverse_engineered_skills": [],
        "mined_files": sorted(texts),
        "scanned_at": _now(),
    }
    entry["reverse_engineered_skills"] = draft_skills(entry, texts)

    reg = load_registry()
    upsert_entry(reg, entry)
    save_registry(reg)
    print(f"[ingest] {agent_id}: utility={entry['utility_score']} "
          f"tier={entry['free_tier_efficiency']} loop={entry['loop_risk_profile']} "
          f"drafts={len(entry['reverse_engineered_skills'])}")
    return 0


def cmd_score(args) -> int:
    reg = load_registry()
    entries = sorted(reg.get("entries", []), key=lambda e: -e.get("utility_score", 0))
    if getattr(args, "json", False):
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("(registry empty — run `ingest` first)")
        return 0
    print(f"{'UTILITY':>7}  {'TIER':<6} {'LOOP-RISK':<9} {'AGENT':<34} SOURCE")
    print("-" * 100)
    for e in entries:
        s = e.get("scores", {})
        print(f"{e['utility_score']:>7.1f}  {e['free_tier_efficiency']:<6} "
              f"{e['loop_risk_profile']:<9} {e['agent_id'][:34]:<34} "
              f"{e['source'][:44]}  "
              f"(scope {s.get('scope_utility', '-')}/eff {s.get('free_tier_efficiency', '-')}"
              f"/loop {s.get('loop_risk', '-')})")
    return 0


def cmd_drafts(_args) -> int:
    if not os.path.isdir(DRAFTS_DIR):
        print("(no drafts yet — run `ingest`)")
        return 0
    total = 0
    for root, _, files in os.walk(DRAFTS_DIR):
        for fn in files:
            if fn == "SKILL.md":
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, REPO_ROOT)
                with open(p, encoding="utf-8") as fh:
                    head = [next(fh, "").rstrip() for _ in range(6)]
                desc = next((h.split(":", 1)[1].strip() for h in head
                             if h.startswith("description:")), "")
                with open(p, encoding="utf-8") as fh:
                    v = safety_scan(fh.read())
                status = "REJECT" if v else "PASS"
                print(f"  [{status:>6}] {rel}  — {desc[:60]}")
                total += 1
    print(f"{total} draft SKILL.md file(s) under "
          f"{os.path.relpath(DRAFTS_DIR, REPO_ROOT)}/")
    return 0


def cmd_check(args) -> int:
    rc = 0
    for path in args.files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"  [error] {path}: {exc}")
            rc = 1
            continue
        v = safety_scan(text)
        if v:
            rc = 1
            print(f"  [REJECT] {path}")
            for item in v:
                print(f"            - {item}")
        else:
            print(f"  [PASS]   {path}")
    return rc


def cmd_validate(_args) -> int:
    reg = load_registry()
    schema_path = os.path.join(REPO_ROOT, "schema", "agent_entry_schema.json")
    try:
        import jsonschema
        with open(schema_path, encoding="utf-8") as fh:
            schema = json.load(fh)
        errs = 0
        for e in reg.get("entries", []):
            for err in jsonschema.Draft7Validator(schema).iter_errors(e):
                errs += 1
                print(f"  [schema] {e.get('agent_id', '?')}: {err.message}")
        print(f"validate: {len(reg.get('entries', []))} entries, {errs} schema errors")
        return 1 if errs else 0
    except ImportError:
        req = ("agent_id", "source", "utility_score", "capabilities")
        bad = [e.get("agent_id", "?") for e in reg.get("entries", [])
               if not all(k in e for k in req)]
        print(f"validate (fallback): {len(reg.get('entries', []))} entries, "
              f"{len(bad)} missing-required: {bad or 'none'}")
        return 1 if bad else 0


def cmd_console(_args) -> int:
    print("Agent Almanac Scout console — read-only. "
          "cmds: score | drafts | ingest <path|url> | check <file> | "
          "validate | quit")
    while True:
        try:
            line = input("almanac> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        parts = line.split()
        cmd, rest = parts[0], parts[1:]
        ns = argparse.Namespace()
        try:
            if cmd == "score":
                ns.json = False
                cmd_score(ns)
            elif cmd == "drafts":
                cmd_drafts(ns)
            elif cmd == "ingest" and rest:
                ns.target, ns.name, ns.source = rest[0], None, None
                cmd_ingest(ns)
            elif cmd == "check" and rest:
                ns.files = rest
                cmd_check(ns)
            elif cmd == "validate":
                cmd_validate(ns)
            elif cmd in {"quit", "exit", "q"}:
                return 0
            else:
                print("unknown or malformed command:", line)
        except Exception as exc:
            print("error:", exc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="almanac_scout",
                                description="Agent Almanac Scout (read-only)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("ingest",
                        help="mine a repo (GitHub/HF URL) or local path (read-only)")
    sp.add_argument("target")
    sp.add_argument("--name", help="override agent_id")
    sp.add_argument("--source", help="override recorded source URL")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("score", help="ranked utility table from live registry")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("drafts",
                        help="list SKILL.md drafts with live safety verdict")
    sp.set_defaults(func=cmd_drafts)

    sp = sub.add_parser("check", help="run the safety filter over arbitrary files")
    sp.add_argument("files", nargs="+")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("validate",
                        help="validate docs/AGENT_ALMANAC.json against schema")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("console", help="interactive CLI (dynamic views only)")
    sp.set_defaults(func=cmd_console)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
