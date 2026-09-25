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
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from almanac import core as engine  # shared read-only engine (single source of truth)

ALMANAC_PATH = os.path.join(REPO_ROOT, "docs", "AGENT_ALMANAC.json")
DRAFTS_DIR = os.path.join(REPO_ROOT, "skills", "drafts")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")
REJECTED_DIR = os.path.join(REPO_ROOT, "docs", "rejected")
frontmatter_set = engine.frontmatter_set

MAX_FETCH_BYTES = engine.MAX_FETCH_BYTES   # hard cap on any remote read
USER_AGENT = engine.USER_AGENT

# --------------------------------------------------------------------------
# ==========================================================================
# Thin aliases — ALL engine logic (taxonomy, safety deny-list, scoring,
# extraction) now lives in almanac/core.py. These wrappers exist only so
# this module's CLI keeps its historical names; behavior is identical and
# any rubric/deny-list change lands once, in the shared engine.
# ==========================================================================
_now = engine.now_iso
_slug = engine.slug
_sha = engine.content_sha
http_get = engine.http_get
extract_capabilities = engine.extract_capabilities
extract_tool_names = engine.extract_tool_declarations
extract_directives = engine.extract_system_prompts
score_agent = engine.score_agent
safety_scan = engine.safety_scan
CAPABILITY_PATTERNS = engine.CAPABILITY_PATTERNS


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
        # HARD GATE mirror of almanac.core.run_pipeline: only quote directives
        # from files that are themselves clean under the shared deny-list.
        safe_relevant = {p: b for p, b in relevant.items() if not safety_scan(b)}
        directives = extract_directives(safe_relevant, limit=4)
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
    # Fetch/ingest delegates to scouts/gh_repo_scout (GitHub tree API + local
    # walk) so there is exactly ONE ingestion implementation in the repo.
    sys.path.insert(0, os.path.join(REPO_ROOT, "scouts"))
    import gh_repo_scout as gh
    target = args.target
    texts = {}
    gh_parsed = gh.parse_github_url(target)
    if gh_parsed:
        owner, repo, branch = gh_parsed
        print(f"[ingest] GitHub read-only fetch: {owner}/{repo}@{branch}")
        texts = gh.fetch_repo_texts(owner, repo, branch)
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
        texts = gh.ingest_local(target)
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
        src_url = e.get("source") or e.get("source_url", "")
        print(f"{e['utility_score']:>7.1f}  {e['free_tier_efficiency']:<6} "
              f"{e['loop_risk_profile']:<9} {e['agent_id'][:34]:<34} "
              f"{src_url[:44]}  "
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


# ---------------------------------------------------------------------------
# Draft lifecycle: draft -> (human review) -> promoted skill | rejected tombstone
# ---------------------------------------------------------------------------
def _find_draft(name: str):
    """Resolve a draft dir by exact name, or <agent_id> prefix match."""
    exact = os.path.join(DRAFTS_DIR, name)
    if os.path.isfile(os.path.join(exact, "SKILL.md")):
        return [exact]
    hits = sorted(
        d for d in glob.glob(os.path.join(DRAFTS_DIR, "*"))
        if os.path.isfile(os.path.join(d, "SKILL.md"))
        and (os.path.basename(d) == name or os.path.basename(d).startswith(name + "_"))
    )
    return hits


def cmd_promote(args) -> int:
    if args.names:
        targets = []
        for n in args.names:
            hits = _find_draft(n)
            if not hits:
                print(f"  [error ] no draft matches '{n}'")
                return 1
            targets.extend(hits)
    else:
        targets = sorted(os.path.dirname(p) for p in
                         glob.glob(os.path.join(DRAFTS_DIR, "*", "SKILL.md")))
    if not targets:
        print("(no drafts to promote — run `ingest` first)")
        return 0
    rc = 0
    for d in targets:
        src_md = os.path.join(d, "SKILL.md")
        if not os.path.isfile(src_md):
            print(f"  [error ] no such draft: {os.path.relpath(d, REPO_ROOT)}")
            rc = 1
            continue
        with open(src_md, encoding="utf-8") as fh:
            text = fh.read()
        violations = safety_scan(text)          # re-gate at promotion time
        if violations:
            rc = 1
            print(f"  [BLOCKED] {os.path.relpath(d, REPO_ROOT)} — safety filter:")
            for v in violations:
                print(f"             - {v}")
            continue
        name = os.path.basename(d)
        dest_dir = os.path.join(SKILLS_DIR, name)
        os.makedirs(dest_dir, exist_ok=True)
        stamped = frontmatter_set(text, {
            "status": "promoted",
            "reviewed_by": args.reviewer,
            "reviewed_at": engine.now_iso(),
        })
        with open(os.path.join(dest_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(stamped)
        shutil.rmtree(d)
        print(f"  [PROMOTE] skills/drafts/{name} -> skills/{name} "
              f"(reviewed_by={args.reviewer})")
    return rc


def cmd_reject(args) -> int:
    hits = _find_draft(args.name)
    if not hits:
        print(f"  [error ] no draft matches '{args.name}'")
        return 1
    rc = 0
    for d in hits:
        src_md = os.path.join(d, "SKILL.md")
        with open(src_md, encoding="utf-8") as fh:
            head = fh.read(600)
        desc = next((m.group(1).strip() for m in
                     [re.search(r"^description:\s*(.+)$", head, re.M)] if m), "")
        name = os.path.basename(d)
        with open(os.path.join(REJECTED_DIR, f"{name}.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"---\nname: {name}\nstatus: rejected\n"
                     f"reason: {args.reason}\nrejected_at: {engine.now_iso()}\n"
                     f"reviewed_by: {args.reviewer}\ndescription: {desc}\n---\n\n"
                     f"Rejected draft tombstone for `{name}`.\n"
                     f"Reason: {args.reason}\n")
        shutil.rmtree(d)
        print(f"  [REJECT ] skills/drafts/{name} -> docs/rejected/{name}.md "
              f"({args.reason})")
    return rc


def cmd_rejected(_args) -> int:
    files = sorted(glob.glob(os.path.join(REJECTED_DIR, "*.md")))
    if not files:
        print("(no rejected drafts recorded)")
        return 0
    for f in files:
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        reason = next((m.group(1) for m in
                       [re.search(r"^reason:\s*(.+)$", text, re.M)] if m), "?")
        when = next((m.group(1) for m in
                     [re.search(r"^rejected_at:\s*(.+)$", text, re.M)] if m), "?")
        print(f"  {os.path.relpath(f, REPO_ROOT):<58} {when:<22} {reason[:60]}")
    print(f"{len(files)} rejected draft(s)")
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
          "promote [name...] [--reviewer X] | reject <name> --reason R | "
          "rejected | validate | quit")
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
            elif cmd == "promote":
                names = [r for r in rest if not r.startswith("--")]
                reviewer = "human"
                if "--reviewer" in rest:
                    i = rest.index("--reviewer")
                    if i + 1 < len(rest):
                        reviewer = rest[i + 1]
                ns.names, ns.reviewer = names, reviewer
                cmd_promote(ns)
            elif cmd == "reject" and len(rest) >= 3:
                ns.name = rest[0]
                ns.reason = (" ".join(rest[2:])
                             if rest[1] == "--reason" else "")
                if not ns.reason:
                    print("usage: reject <name> --reason <text>")
                    continue
                ns.reviewer = "human"
                cmd_reject(ns)
            elif cmd == "rejected":
                cmd_rejected(ns)
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

    sp = sub.add_parser("promote",
                        help="move vetted drafts into skills/ (re-runs safety "
                             "filter; refuses unsafe drafts)")
    sp.add_argument("names", nargs="*",
                    help="draft dir name(s) or agent_id prefix; omit = all")
    sp.add_argument("--reviewer", default="human",
                    help="who reviewed/approved this promotion")
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("reject",
                        help="delete a draft and record a tombstone in docs/rejected/")
    sp.add_argument("name", help="draft dir name or agent_id prefix")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--reviewer", default="human")
    sp.set_defaults(func=cmd_reject)

    sp = sub.add_parser("rejected", help="list rejected-draft tombstones")
    sp.set_defaults(func=cmd_rejected)

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
