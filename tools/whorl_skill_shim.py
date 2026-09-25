#!/usr/bin/env python3
"""
whorl_skill_shim.py — read-only consumer shim between the Agent Almanac and Whorl.
===================================================================================

Whorl's loader (src/skill/scout.py) expects vetted, self-contained SKILL.md
files. This shim gives it exactly that contract WITHOUT touching core system
boundaries:

  * READ-ONLY: never writes, moves, or deletes anything. It lists, parses,
    validates and emits JSON/markdown to stdout for a human (or Whorl) to act on.
  * GATED: only skills in skills/ (promoted) are offered; skills/drafts/ are
    visible via `list --all` but flagged status=draft and excluded from
    `export` unless --include-drafts is passed explicitly.
  * SAFETY-RECHECKED: every skill is re-scanned with almanac.core.safety_scan
    at load time. A promoted skill whose text trips the deny-list is reported
    BLOCKED — promotion is not a permanent trust grant.
  * FRONTMATTER CONTRACT: name / utility_score / free_tier_efficiency /
    loop_risk_profile / safety_filter must be present and sane.

CLI (dynamic views only — everything reflects live disk state):
  python3 tools/whorl_skill_shim.py list [--all]        # catalog table
  python3 tools/whorl_skill_shim.py show <skill-name>   # parsed skill + checks
  python3 tools/whorl_skill_shim.py export [--out FILE] # bundle for src/skill/scout.py
  python3 tools/whorl_skill_shim.py verify              # contract+safety over all skills

Exit codes: 0 ok · 1 blocked/contract-violating skills found · 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from almanac import core as engine  # shared engine (single source of truth)

SKILLS_DIR = os.path.join(REPO_ROOT, "skills")
DRAFTS_DIR = os.path.join(SKILLS_DIR, "drafts")

REQUIRED_FIELDS = ("name", "utility_score", "free_tier_efficiency",
                   "loop_risk_profile", "safety_filter")


# ---------------------------------------------------------------------------
# Parsing (tiny frontmatter reader — deliberately local so the shim stays
# dependency-free even if almanac/core.py evolves).
# ---------------------------------------------------------------------------
def parse_frontmatter(text: str) -> dict:
    """Return simple key: value pairs from a leading --- block."""
    fm = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re_match_kv(line)
        if m:
            k, v = m
            if v == "" or v in (">-", ">"):     # folded scalar: grab next indented lines
                continue
            fm[k] = v.strip().strip('"')
    return fm


def re_match_kv(line: str):
    idx = line.find(":")
    if idx <= 0 or line.startswith((" ", "\t")):
        return None
    key = line[:idx].strip()
    if not key or key.startswith("#"):
        return None
    return key, line[idx + 1:]


# ---------------------------------------------------------------------------
# Skill discovery + validation
# ---------------------------------------------------------------------------
def find_skills(include_drafts: bool = False):
    """Yield (path, status) for every SKILL.md on disk. Live scan, no cache.

    Only <dir>/SKILL.md files are considered — one skill per directory — so
    fixture trees and foreign bundles nested under skills/ never masquerade as
    offerable skills."""
    roots = [(SKILLS_DIR, "promoted")]
    if include_drafts:
        roots.append((DRAFTS_DIR, "draft"))
    out = []
    for root, status in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            skill_md = os.path.join(root, entry, "SKILL.md")
            if entry == "templates" or entry == "drafts" and status == "promoted":
                continue
            if os.path.isfile(skill_md):
                out.append((skill_md, status))
    return out


def inspect(path: str, status: str) -> dict:
    """Parse one SKILL.md → record with contract + safety verdicts."""
    rec = {"path": os.path.relpath(path, REPO_ROOT), "status": status}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        rec.update(ok=False, errors=[f"unreadable: {exc}"], violations=[],
                   frontmatter={})
        return rec

    fm = parse_frontmatter(text)
    missing = [k for k in REQUIRED_FIELDS if k not in fm]
    errors = [f"missing frontmatter field: {k}" for k in missing]

    score = fm.get("utility_score", "")
    try:
        val = float(score)
        if not 0.0 <= val <= 10.0:
            errors.append(f"utility_score out of range: {val}")
    except ValueError:
        if score != "":
            errors.append(f"utility_score not numeric: {score!r}")

    violations = engine.safety_scan(text)          # re-check at load time (human-readable strings)
    blocked = bool(violations) or fm.get("safety_filter") != "PASSED"

    rec.update(
        name=fm.get("name", os.path.basename(os.path.dirname(path))),
        frontmatter=fm,
        capability=fm.get("name", "").rsplit("_", 1)[-1] if "_" in fm.get("name", "") else "",
        utility_score=val if score else None,
        tier=fm.get("free_tier_efficiency", "?"),
        loop_risk=fm.get("loop_risk_profile", "?"),
        reviewed_by=fm.get("reviewed_by", ""),
        violations=violations,
        blocked=blocked,
        errors=errors,
        ok=not errors and not blocked,
    )
    return rec


def load_all(include_drafts: bool = False):
    return [inspect(p, s) for p, s in find_skills(include_drafts)]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_list(args) -> int:
    recs = load_all(include_drafts=args.all)
    if not recs:
        print("(no SKILL.md files on disk yet)")
        return 0
    fmt = "{:<44} {:<9} {:>5}  {:<6} {:<9} {}"
    print(fmt.format("SKILL", "STATUS", "SCORE", "TIER", "LOOP-RISK", "VERDICT"))
    print("-" * 88)
    bad = 0
    for r in recs:
        verdict = "OK" if r["ok"] else ("BLOCKED" if r["blocked"] else "CONTRACT-ERR")
        if verdict != "OK":
            bad += 1
        print(fmt.format(r["name"][:44], r["status"],
                         f"{r['utility_score']:.1f}" if r.get("utility_score") is not None else "-",
                         r.get("tier", "-"), r.get("loop_risk", "-"), verdict))
    print(f"\n{len(recs)} skill(s); {bad} not offerable to Whorl.")
    return 1 if bad else 0


def cmd_show(args) -> int:
    recs = load_all(include_drafts=True)
    match = [r for r in recs if r["name"] == args.name]
    if not match:
        print(f"no skill named {args.name!r}; run `list --all`", file=sys.stderr)
        return 2
    r = match[0]
    print(json.dumps({k: v for k, v in r.items() if k != "frontmatter"}, indent=2))
    print("--- frontmatter ---")
    print(json.dumps(r["frontmatter"], indent=2))
    if r["violations"]:
        print("--- safety violations ---")
        for v in r["violations"]:
            print(f"  - {v}")
    return 0 if r["ok"] else 1


def cmd_export(args) -> int:
    """Emit the Whorl-consumable manifest (stdout or --out file)."""
    recs = load_all(include_drafts=args.include_drafts)
    offerable = [r for r in recs if r["ok"]]
    manifest = {
        "generated_at": engine.now_iso(),
        "generator": "tools/whorl_skill_shim.py (read-only)",
        "consumer_contract": "src/skill/scout.py — load by path, treat as static procedure docs",
        "skills": [
            {
                "name": r["name"],
                "path": r["path"],
                "status": r["status"],
                "utility_score": r["utility_score"],
                "free_tier_efficiency": r["tier"],
                "loop_risk_profile": r["loop_risk"],
                "reviewed_by": r["reviewed_by"],
            }
            for r in offerable
        ],
        "excluded": [
            {"name": r["name"], "path": r["path"],
             "reason": ("safety:" + "; ".join(r["violations"]))
            if r["blocked"] else "contract:" + ";".join(r["errors"])}
            for r in recs if not r["ok"]
        ],
    }
    blob = json.dumps(manifest, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
        print(f"wrote {args.out} ({len(manifest['skills'])} offerable, "
              f"{len(manifest['excluded'])} excluded)")
    else:
        print(blob)
    return 0


def cmd_verify(_args) -> int:
    recs = load_all(include_drafts=True)
    problems = [r for r in recs if not r["ok"]]
    for r in problems:
        kind = "BLOCKED" if r["blocked"] else "CONTRACT-ERR"
        detail = ("; ".join(r["violations"]) or ";".join(r["errors"]))
        print(f"[{kind}] {r['path']}: {detail}")
    print(f"verified {len(recs)} skill(s): {len(recs) - len(problems)} clean, "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="catalog table of skills on disk")
    sp.add_argument("--all", action="store_true", help="include drafts")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="parsed view + live checks for one skill")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("export", help="emit Whorl manifest (JSON)")
    sp.add_argument("--out", help="write to file instead of stdout")
    sp.add_argument("--include-drafts", action="store_true")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("verify", help="contract+safety pass over everything")
    sp.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
