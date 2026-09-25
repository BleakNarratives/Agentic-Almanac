#!/usr/bin/env python3
"""
gh_repo_scout.py — GitHub repo scout for BleakNarratives/Agentic-Almanac
========================================================================

Read-only ingestion of agent repositories: parses a GitHub URL (or local
path), extracts agent SYSTEM PROMPTS and TOOL DECLARATIONS, scores their
utility against schema/agent_entry_schema.json, and renders vetted
SKILL.md files into skills/ using skills/templates/SKILL_TEMPLATE.md.

Guarantees
  * READ-ONLY: never executes, imports, or installs code from targets.
    Remote access is plain HTTPS GETs (GitHub API tree listing +
    raw.githubusercontent text blobs) with hard size caps.
  * SAFETY FILTER BEFORE WRITE: aggressive directives ("ignore user
    pushback", unauthorized auto-commits, guardrail bypass, unbounded
    loops, credential exfiltration, prompt-injection overrides) cause
    rejection before any file touches disk.
  * SCHEMA-GATED: the almanac entry for each run must validate against
    schema/agent_entry_schema.json (jsonschema if installed, built-in
    structural validator otherwise) before drafts are rendered.

Usage
  python3 scouts/gh_repo_scout.py <github-url | local-path>
                                  [--name AGENT_ID] [--source DISPLAY_URL]
                                  [--out-dir skills]
                                  [--template skills/templates/SKILL_TEMPLATE.md]
                                  [--dry-run] [-v]

Exit codes: 0 ok (>=1 skill written or --dry-run clean), 1 rejected/no
skills, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMPLATE = os.path.join(REPO_ROOT, "skills", "templates", "SKILL_TEMPLATE.md")
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "schema", "agent_entry_schema.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "skills")

MAX_FETCH_BYTES = 512_000
MAX_BLOB_BYTES = 200_000
USER_AGENT = "AgenticAlmanac-github-scout/1.0 (read-only; no-code-execution)"

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
# Safety filter — reject BEFORE writing
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
HEAVY_MARKERS = [
    r"\bfew[- ]shot\b", r"\bchain[- ]of[- ]thought\b", r"\breflection\b",
    r"\bmulti[- ]agent\b", r"\bre[- ]?planning\b", r"\bscratchpad\b",
    r"\bverbose\b", r"\bextensive context\b", r"\btree of thoughts\b",
]
LEAN_MARKERS = [
    r"\bconcise\b", r"\bminimal\b", r"\bsystem prompt.{0,30}\bbrief",
    r"\btoken[- ]efficient\b", r"\blazy loading\b", r"\bon demand\b", r"\bstreaming\b",
]

# Text files worth mining in a repo
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
# Small helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "skill"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
    except Exception as exc:
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
# Target parsing: GitHub URLs and local paths
# ---------------------------------------------------------------------------
def parse_github_url(url: str):
    m = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    tail = url.split(f"{owner}/{repo}", 1)[-1].strip("/")
    branch = None
    for prefix in ("tree", "blob"):
        if tail.startswith(prefix + "/"):
            parts = tail.split("/")[1:]
            if parts:
                branch = parts[0]
                break
    return owner, repo, branch or "HEAD"


def default_paths_for_repo(owner: str, repo: str, branch: str) -> list:
    """Known text paths to mine when the git-tree API is unavailable/offline."""
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
    return [
        raw + "README.md",
        raw + "AGENTS.md",
        raw + "CLAUDE.md",
        raw + "src/smolagents/agents.py",
        raw + "src/smolagents/default_tools.py",
        raw + "docs/source/en/reference/agents.md",
    ]


def fetch_repo_texts(owner: str, repo: str, branch: str, verbose: bool = False) -> dict:
    """Read-only harvest: list tree via GitHub API, then GET interesting text blobs."""
    texts: dict = {}
    tree = http_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
        verbose=verbose)
    entries = [e for e in (tree or {}).get("tree", [])
               if e.get("type") == "blob"
               and INTERESTING_FILE.search("/" + e.get("path", ""))
               and 0 < e.get("size", 0) <= MAX_BLOB_BYTES]
    if entries:
        # prioritize config/docs-ish paths first, cap total requests
        def prio(p):
            path = p.lower()
            return (0 if any(k in path for k in ("agents.md", "claude.md", "skill", "prompt")) else
                    1 if path.endswith(".md") else 2)
        entries.sort(key=lambda e: (prio(e["path"]), e["path"]))
        for e in entries[:25]:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{e['path']}"
            body = http_get(url, verbose=verbose)
            if body:
                texts[e["path"]] = body
            if sum(len(v) for v in texts.values()) > 1_500_000:
                break
    if not texts:  # API unavailable / rate limited / empty match → static fallback
        if verbose:
            print("  [fallback] tree API unavailable, trying known text paths", file=sys.stderr)
        for url in default_paths_for_repo(owner, repo, branch):
            body = http_get(url, verbose=verbose)
            if body:
                texts[url.split(f"/{branch}/")[-1]] = body
    return texts


def ingest_local(path: str, verbose: bool = False) -> dict:
    """Read-only walk of a local checkout/dir/file. Skips binaries & huge files."""
    texts: dict = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            texts[os.path.basename(path)] = fh.read(MAX_FETCH_BYTES)
        return texts
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), path).replace(os.sep, "/")
            if not INTERESTING_FILE.search("/" + rel):
                continue
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > MAX_BLOB_BYTES:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    texts[rel] = fh.read(MAX_FETCH_BYTES)
            except OSError:
                pass
    if verbose:
        print(f"  [local] mined {len(texts)} files from {path}", file=sys.stderr)
    return texts


# ---------------------------------------------------------------------------
# Extraction: system prompts & tool declarations
# ---------------------------------------------------------------------------
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


def extract_capabilities(texts: dict) -> list:
    blob = "\n".join(texts.values())
    return sorted(cap for cap, pats in CAPABILITY_PATTERNS.items()
                  if any(re.search(p, blob, re.IGNORECASE) for p in pats))


def safety_scan(text: str) -> list:
    return [why for pat, why in SAFETY_DENY if re.search(pat, text, re.IGNORECASE)]


# ---------------------------------------------------------------------------
# Scoring (rubric shared with tools/almanac_scout.py)
#   utility = 0.4*scope_utility + 0.25*free_tier_efficiency + 0.35*loop_risk
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


# ---------------------------------------------------------------------------
# Schema validation against schema/agent_entry_schema.json
# ---------------------------------------------------------------------------
def validate_against_schema(entry: dict, schema_path: str) -> tuple:
    """Returns (ok: bool, method: str, detail: str)."""
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


def build_skill_files(entry: dict, texts: dict, template: str, validation_note: str) -> list:
    """Return [(relpath, content)] for vetted SKILL.md files (nothing written yet)."""
    out = []
    tools = extract_tool_declarations(texts)
    for cap in entry["capabilities"]:
        relevant = {p: b for p, b in texts.items()
                    if any(re.search(pat, b, re.IGNORECASE)
                           for pat in CAPABILITY_PATTERNS.get(cap, []))}
        directives = extract_system_prompts(relevant or texts, limit=4)
        proc = ["1. Confirm the goal maps to this capability before acting."]
        for i, d in enumerate(directives, start=2):
            proc.append(f'{i}. Preserve intent of source directive: "{d[:160]}"')
        proc.append(f"{len(proc)+1}. Verify output with ONE check; halt on ambiguity.")
        procedure = "\n".join(proc)

        mapping = {
            "AGENT_ID": entry["agent_id"],
            "CAPABILITY_SLUG": _slug(cap),
            "CAPABILITY": cap,
            "SOURCE_URL": entry["source_url"],
            "UTILITY_SCORE": entry["utility_score"],
            "FREE_TIER_EFFICIENCY": entry["free_tier_efficiency"],
            "LOOP_RISK_PROFILE": entry["loop_risk_profile"],
            "GENERATED_AT": _now(),
            "CONTENT_SHA256": _sha(procedure + json.dumps(tools[:8])),
            "FILES_MINED": ", ".join(f"`{p}`" for p in sorted((relevant or texts))[:4]) or "`(none)`",
            "TOOL_SURFACE": ", ".join(f"`{t}`" for t in tools[:8]) or "(no named tools detected)",
            "PROCEDURE": procedure,
            "WHEN_TO_USE": WHEN_TO_USE.get(cap, f"a task requires `{cap}`"),
            "MAX_STEPS": 8 if entry["loop_risk_profile"] != "HIGH" else 4,
            "KNOWN_RISKS": "\n".join(f"- {r}" for r in entry["known_risks"]) or "- none detected",
            "SCHEMA_VALIDATION": validation_note,
        }
        content = render_template(template, mapping)
        out.append((f"{_slug(entry['agent_id'])}_{_slug(cap)}/SKILL.md", content))
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def cmd_scout(args) -> int:
    target = args.target
    gh = parse_github_url(target)
    verbose = args.verbose
    if gh:
        owner, repo, branch = gh
        source_url = f"https://github.com/{owner}/{repo}"
        agent_id = args.name or _slug(f"{owner}_{repo}")
        print(f"[scout] GitHub read-only fetch: {owner}/{repo}@{branch}")
        texts = fetch_repo_texts(owner, repo, branch, verbose=verbose)
    elif os.path.exists(target):
        source_url = args.source or f"local:{os.path.abspath(target)}"
        agent_id = args.name or _slug(os.path.basename(os.path.normpath(target)))
        print(f"[scout] Local read-only ingest: {target}")
        texts = ingest_local(target, verbose=verbose)
    else:
        print(f"[error] target is neither a GitHub URL nor an existing path: {target}",
              file=sys.stderr)
        return 2
    if args.source:
        source_url = args.source

    if not texts:
        print("[error] no readable text files were ingested; nothing to score.",
              file=sys.stderr)
        return 1
    print(f"[scout] ingested {len(texts)} files "
          f"({sum(len(v) for v in texts.values())//1024} KiB, read-only)")

    scoring = score_agent(texts)
    entry = {
        "agent_id": agent_id,
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
        "scanned_at": _now(),
        "scanned_files": sorted(texts.keys()),
    }
    print(f"[score] utility={entry['utility_score']}/10 "
          f"(scope {scoring['scores']['scope_utility']}, "
          f"efficiency {scoring['scores']['free_tier_efficiency']}, "
          f"loop-bounds {scoring['scores']['loop_risk']}) "
          f"tier={entry['free_tier_efficiency']} loop-risk={entry['loop_risk_profile']}")
    print(f"[score] capabilities: {', '.join(entry['capabilities']) or '(none)'}")
    if entry["known_risks"]:
        print(f"[score] known risks : {', '.join(entry['known_risks'])}")

    ok, method, detail = validate_against_schema(entry, args.schema)
    validation_note = f"{method} validator: {detail}"
    print(f"[schema] {os.path.relpath(args.schema, REPO_ROOT)} via {method}: "
          + ("PASS" if ok else f"FAIL ({detail})"))
    if not ok:
        print("[reject] refusing to write skills: entry does not satisfy the schema",
              file=sys.stderr)
        return 1

    try:
        with open(args.template, "r", encoding="utf-8") as fh:
            template = fh.read()
    except OSError as exc:
        print(f"[error] cannot read template {args.template}: {exc}", file=sys.stderr)
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
        if args.dry_run:
            print(f"  [dry-run] would write skills/{rel} ({len(content)} bytes)")
            written += 1
            continue
        outfile = os.path.join(args.out_dir, rel)
        os.makedirs(os.path.dirname(outfile), exist_ok=True)
        with open(outfile, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"  [skill] {os.path.relpath(outfile, REPO_ROOT)} ({len(content)} bytes)")
        written += 1

    # optional registry upsert (keeps docs/AGENT_ALMANAC.json current)
    if args.register and not args.dry_run and written:
        reg_path = os.path.join(REPO_ROOT, "docs", "AGENT_ALMANAC.json")
        try:
            with open(reg_path, "r", encoding="utf-8") as fh:
                reg = json.load(fh)
        except (OSError, json.JSONDecodeError):
            reg = {"registry_version": "1.0.0", "updated_at": _now(), "entries": []}
        entries = reg.setdefault("entries", [])
        entries[:] = [e for e in entries if e.get("agent_id") != agent_id] + [entry]
        reg["updated_at"] = _now()
        with open(reg_path, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=2)
            fh.write("\n")
        print(f"[registry] upserted '{agent_id}' in {os.path.relpath(reg_path, REPO_ROOT)}")

    print(f"[done] {written}/{len(candidates)} SKILL.md file(s) "
          + ("validated (dry-run)" if args.dry_run else "written"))
    return 0 if written else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="gh_repo_scout.py",
        description="Read-only GitHub agent-repo scout → scored SKILL.md drafts "
                    "for BleakNarratives/Agentic-Almanac.")
    ap.add_argument("target", help="GitHub repo URL (…/owner/repo[/tree/branch]) or local path")
    ap.add_argument("--name", help="agent_id for the almanac entry (default: derived)")
    ap.add_argument("--source", help="display source URL to record (default: derived)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT,
                    help=f"where skills/ files go (default: {DEFAULT_OUT})")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE,
                    help="SKILL.md template path")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA,
                    help="agent entry JSON Schema path")
    ap.add_argument("--register", action="store_true",
                    help="also upsert the entry into docs/AGENT_ALMANAC.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="score + safety-check without writing files")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    return cmd_scout(args)


if __name__ == "__main__":
    sys.exit(main())
