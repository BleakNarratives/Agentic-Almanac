#!/usr/bin/env python3
"""
gh_repo_scout.py — GitHub repo scout for BleakNarratives/Agentic-Almanac
========================================================================

Read-only ingestion of agent repositories: parses a GitHub URL (or local
path), extracts agent SYSTEM PROMPTS and TOOL DECLARATIONS, scores their
utility against schema/agent_entry_schema.json, and renders vetted
SKILL.md files into skills/ using skills/templates/SKILL_TEMPLATE.md.

The extraction / scoring / safety / schema / template ENGINE lives in the
shared `almanac` package (almanac/core.py) so every scout enforces exactly
one deny-list and one rubric. This module only adds GitHub-specific,
read-only fetching.

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
                                  [--register] [--dry-run] [-v]

Exit codes: 0 ok (>=1 skill written or --dry-run clean), 1 rejected/no
skills, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from almanac import core as engine  # shared read-only engine (single source of truth)


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
    tree = engine.http_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
        verbose=verbose)
    entries = [e for e in (tree or {}).get("tree", [])
               if e.get("type") == "blob"
               and engine.INTERESTING_FILE.search("/" + e.get("path", ""))
               and 0 < e.get("size", 0) <= engine.MAX_BLOB_BYTES]
    if entries:
        # prioritize config/docs-ish paths first, cap total requests
        def prio(p):
            path = p.lower()
            return (0 if any(k in path for k in ("agents.md", "claude.md", "skill", "prompt")) else
                    1 if path.endswith(".md") else 2)
        entries.sort(key=lambda e: (prio(e["path"]), e["path"]))
        for e in entries[:25]:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{e['path']}"
            body = engine.http_get(url, verbose=verbose)
            if body:
                texts[e["path"]] = body
            if sum(len(v) for v in texts.values()) > 1_500_000:
                break
    if not texts:  # API unavailable / rate limited / empty match → static fallback
        if verbose:
            print("  [fallback] tree API unavailable, trying known text paths", file=sys.stderr)
        for url in default_paths_for_repo(owner, repo, branch):
            body = engine.http_get(url, verbose=verbose)
            if body:
                texts[url.split(f"/{branch}/")[-1]] = body
    return texts


def ingest_local(path: str, verbose: bool = False) -> dict:
    """Read-only walk of a local checkout/dir/file. Skips binaries & huge files."""
    texts: dict = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            texts[os.path.basename(path)] = fh.read(engine.MAX_FETCH_BYTES)
        return texts
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), path).replace(os.sep, "/")
            if not engine.INTERESTING_FILE.search("/" + rel):
                continue
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > engine.MAX_BLOB_BYTES:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    texts[rel] = fh.read(engine.MAX_FETCH_BYTES)
            except OSError:
                pass
    if verbose:
        print(f"  [local] mined {len(texts)} files from {path}", file=sys.stderr)
    return texts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def cmd_scout(args) -> int:
    target = args.target
    gh = parse_github_url(target)
    verbose = args.verbose
    if gh:
        owner, repo, branch = gh
        source_url = f"https://github.com/{owner}/{repo}"
        agent_id = args.name or engine.slug(f"{owner}_{repo}")
        print(f"[scout] GitHub read-only fetch: {owner}/{repo}@{branch}")
        texts = fetch_repo_texts(owner, repo, branch, verbose=verbose)
    elif os.path.exists(target):
        source_url = args.source or f"local:{os.path.abspath(target)}"
        agent_id = args.name or engine.slug(os.path.basename(os.path.normpath(target)))
        print(f"[scout] Local read-only ingest: {target}")
        texts = ingest_local(target, verbose=verbose)
    else:
        print(f"[error] target is neither a GitHub URL nor an existing path: {target}",
              file=sys.stderr)
        return 2
    if args.source:
        source_url = args.source
    return engine.run_pipeline(agent_id, source_url, texts,
                               out_dir=args.out_dir, template_path=args.template,
                               schema_path=args.schema, dry_run=args.dry_run,
                               register=args.register, verbose=verbose)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="gh_repo_scout.py",
        description="Read-only GitHub agent-repo scout → scored SKILL.md drafts "
                    "for BleakNarratives/Agentic-Almanac.")
    ap.add_argument("target", help="GitHub repo URL (…/owner/repo[/tree/branch]) or local path")
    ap.add_argument("--name", help="agent_id for the almanac entry (default: derived)")
    ap.add_argument("--source", help="display source URL to record (default: derived)")
    ap.add_argument("--out-dir", default=engine.DEFAULT_OUT,
                    help=f"where skills/ files go (default: {engine.DEFAULT_OUT})")
    ap.add_argument("--template", default=engine.DEFAULT_TEMPLATE,
                    help="SKILL.md template path")
    ap.add_argument("--schema", default=engine.DEFAULT_SCHEMA,
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
