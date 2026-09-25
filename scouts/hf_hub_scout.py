#!/usr/bin/env python3
"""
hf_hub_scout.py — Hugging Face hub scout for BleakNarratives/Agentic-Almanac
============================================================================

Read-only ingestion of Hugging Face models / spaces / datasets that ship
agent code: fetches the repo file listing via the public HF hub API, mines
SYSTEM PROMPTS and TOOL DECLARATIONS, scores utility against
schema/agent_entry_schema.json, and renders vetted SKILL.md files into
skills/ using skills/templates/SKILL_TEMPLATE.md.

This module deliberately REUSES the extraction / scoring / safety / template
engine from scouts/gh_repo_scout.py (imported as a local sibling module —
never does it execute or import any code from the *target* being scouted).

Guarantees
  * READ-ONLY: plain HTTPS GETs to huggingface.co with hard size caps.
    Nothing fetched is executed, imported, or installed.
  * SAFETY FILTER BEFORE WRITE: aggressive directives cause rejection
    before any file touches disk (same deny-list as gh_repo_scout).
  * SCHEMA-GATED: entries must validate against agent_entry_schema.json.

Usage
  python3 scouts/hf_hub_scout.py <hf-url | hub-id>
                                 [--kind auto|model|space|dataset]
                                 [--name AGENT_ID] [--out-dir skills]
                                 [--register] [--dry-run] [-v]

Examples
  python3 scouts/hf_hub_scout.py Qwen/Qwen2.5-Coder-32B-Instruct
  python3 scouts/hf_hub_scout.py https://huggingface.co/spaces/davidbro/smolagents-playground
  python3 scouts/hf_hub_scout.py some/model --dry-run -v

Exit codes: 0 ok, 1 rejected/no skills, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gh_repo_scout as engine  # local sibling module — vetted engine reuse

HF_API = "https://huggingface.co/api"
USER_AGENT = engine.USER_AGENT.replace("github", "hf-hub")


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------
def parse_hf_target(target: str, kind_hint: str = "auto") -> tuple[str, str, str]:
    """Return (kind, repo_id, display_url) for an HF URL or bare hub id."""
    target = target.strip()
    if os.path.exists(target):  # local dir/file — handled by caller as local ingest
        raise FileNotFoundError(target)
    m = re.match(r"https?://huggingface\.co/([^/]+)/([^/]+)/([^/]+)", target)
    if m and m.group(3) in ("resolve", "blob", "tree"):
        kind, rid = m.group(1), f"{m.group(2)}/{m.group(3)}"
    else:
        m = re.match(r"https?://huggingface\.co/(models|datasets|spaces)/(.+?)/?$", target)
        if m:
            kind, rid = m.group(1), m.group(2).strip("/")
        elif re.match(r"https?://huggingface\.co/([^/]+)/([^/?#]+)$", target):
            m = re.match(r"https?://huggingface\.co/([^/]+)/([^/?#]+)$", target)
            g1, g2 = m.group(1), m.group(2)
            if g1 in ("models", "datasets", "spaces"):
                kind, rid = g1, g2
            else:
                kind, rid = "model", f"{g1}/{g2}"  # canonical user/repo form
        elif "/" in target and not target.startswith((".", "/", "~")):
            kind, rid = "model", target.strip("/")
        else:
            raise ValueError(f"not an HF url or hub repo id: {target!r}")
    if kind_hint != "auto":
        kind = kind_hint.rstrip("s") if kind_hint != "space" else "spaces"
        kind = {"model": "models", "dataset": "datasets", "spaces": "spaces",
                "space": "spaces"}.get(kind, kind)
    display = f"https://huggingface.co/{'' if kind=='models' else kind+'/'}{rid}"
    return kind, rid, display


def _kind_candidates(kind: str) -> list[str]:
    order = ["models", "spaces", "datasets"]
    return [kind] if kind in order else order


# ---------------------------------------------------------------------------
# Fetching (read-only)
# ---------------------------------------------------------------------------
def hf_file_listing(kind: str, rid: str) -> list[str]:
    """GET the hub API file tree; returns [] on failure (caller tries next kind)."""
    url = f"{HF_API}/{kind}/{urllib.parse.quote(rid)}"
    data = engine.http_get_json(url)
    if not isinstance(data, dict):
        return []
    # HF hub API names files "rfilename" (GitHub-style "path" as fallback)
    files = [(e.get("rfilename") or e.get("path") or "")
             for e in data.get("siblings", []) if isinstance(e, dict)]
    return [f for f in files if f]


def hf_raw_url(kind: str, rid: str, path: str) -> str:
    rev = {"models": "main", "spaces": "main", "datasets": "refs/main"}[kind]
    return f"https://huggingface.co/{rid}/resolve/{rev}/{path}"


def select_files(files: list[str]) -> list[str]:
    """Pick text blobs worth mining — INTERESTING_FILE heuristic from the
    GitHub scout, plus HF-specific artifacts (app.py for spaces, chat
    templates, config.json which often embeds system/prompt metadata)."""
    keep = [f for f in files if engine.INTERESTING_FILE.search("/" + f)
            or os.path.basename(f.lower()) in ("app.py", "chat_template.jinja",
                                               "config.json")]
    # prioritize prompt/skill docs first, cap total requests like gh_repo_scout
    def prio(p):
        path = p.lower()
        return (0 if any(k in path for k in ("agents.md", "claude.md", "skill", "prompt")) else
                1 if path.endswith(".md") else 2)
    keep.sort(key=lambda p: (prio(p), p))
    return keep[:25]


def ingest_hf(kind: str, rid: str, verbose: bool = False) -> dict[str, str]:
    files = hf_file_listing(kind, rid)
    if not files:
        return {}
    corpus: dict[str, str] = {}
    for path in select_files(files):
        body = engine.http_get(hf_raw_url(kind, rid, path), verbose=verbose)
        if body:
            corpus[f"{kind}/{rid}:{path}"] = body
        if sum(len(v) for v in corpus.values()) > 1_500_000:
            break
    return corpus


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Hugging Face hub agent scout")
    ap.add_argument("target", help="HF url (model/space/dataset) or hub repo id")
    ap.add_argument("--kind", choices=["auto", "model", "space", "dataset"], default="auto")
    ap.add_argument("--name", help="agent_id override (default: derived from repo id)")
    ap.add_argument("--out-dir", default=engine.DEFAULT_OUT)
    ap.add_argument("--template", default=engine.DEFAULT_TEMPLATE)
    ap.add_argument("--schema", default=engine.DEFAULT_SCHEMA)
    ap.add_argument("--register", action="store_true",
                    help="upsert the scored entry into docs/AGENT_ALMANAC.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    hint = {"model": "models", "space": "spaces", "dataset": "datasets",
            "auto": "auto"}[args.kind]
    try:
        kind, rid, display = parse_hf_target(args.target, hint)
    except FileNotFoundError:
        # local directory/file (e.g. an HF space checkout) — read-only walk
        print(f"[scout] Local read-only ingest: {args.target}")
        corpus = engine.ingest_local(args.target, verbose=args.verbose)
        agent_id = args.name or re.sub(
            r"[^a-z0-9]+", "_", os.path.basename(os.path.normpath(args.target)).lower()).strip("_")
        return engine.run_pipeline(agent_id, f"local:{os.path.abspath(args.target)}", corpus,
                                   out_dir=args.out_dir, template_path=args.template,
                                   schema_path=args.schema, dry_run=args.dry_run,
                                   register=args.register, verbose=args.verbose)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    kinds_to_try = [kind] if hint != "auto" else _kind_candidates(kind)
    corpus: dict[str, str] = {}
    used_kind = kind
    for k in kinds_to_try:
        corpus = ingest_hf(k, rid)
        if corpus:
            used_kind = k
            break
    if not corpus:
        print(f"error: no readable text files found for HF {rid!r} "
              f"(tried kinds {', '.join(kinds_to_try)})", file=sys.stderr)
        return 2

    total_bytes = sum(len(v.encode()) for v in corpus.values())
    print(f"[ingest] HF {used_kind}: {len(corpus)} files, {total_bytes//1024} KiB mined "
          f"(read-only GETs; nothing executed)")
    if args.verbose:
        for name in sorted(corpus):
            print(f"  - {name}")

    agent_id = args.name or re.sub(r"[^a-z0-9]+", "_", rid.lower()).strip("_")
    return engine.run_pipeline(agent_id, display, corpus,
                               out_dir=args.out_dir, template_path=args.template,
                               schema_path=args.schema, dry_run=args.dry_run,
                               register=args.register, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
