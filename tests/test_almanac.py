#!/usr/bin/env python3
"""
tests/test_almanac.py — frozen regression suite for the Agentic-Almanac engine
==============================================================================

Network-free (runs entirely on local fixtures + repo files). Locks in the
properties we cannot afford to regress:

  1. SAFETY FILTER: the evil fixture is fully rejected — exit code 1, zero
     SKILL.md files written — through BOTH entry points (scouts/gh_repo_scout.py
     and tools/almanac_scout.py), proving there is exactly ONE deny-list now.
  2. CLEAN PATH: the clean bounded-agent fixture scores well, validates
     against schema/agent_entry_schema.json, and renders SKILL.md files with
     no orphan {{PLACEHOLDER}} tokens.
  3. SINGLE SOURCE OF TRUTH: all three scout modules import their
     safety_scan/score_agent from almanac.core (no forked copies).
  4. REGISTRY: docs/AGENT_ALMANAC.json validates against both schemas.
  5. CLI CONTRACTS: check/validate/dry-run exit codes.

Run:  python -m pytest tests/ -q   (from repo root)
"""

import json
import os
import re
import argparse
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scouts"))

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
EVIL = os.path.join(FIXTURES, "evil_agent_space")
CLEAN = os.path.join(FIXTURES, "clean_agent_repo")
SCHEMA = os.path.join(REPO_ROOT, "schema", "agent_entry_schema.json")
REGISTRY = os.path.join(REPO_ROOT, "docs", "AGENT_ALMANAC.json")

from almanac import core as engine  # noqa: E402


def read_texts(path):
    """Read-only ingest of a fixture dir using the gh scout's walker."""
    import gh_repo_scout as gh
    return gh.ingest_local(path)


# ---------------------------------------------------------------------------
# 3. Single source of truth
# ---------------------------------------------------------------------------
class TestSingleSourceOfTruth:
    def test_all_modules_share_one_safety_list(self):
        import gh_repo_scout
        assert gh_repo_scout.engine.SAFETY_DENY is engine.SAFETY_DENY
        import tools.almanac_scout as tas  # repo root on sys.path -> package import
        assert tas.safety_scan is engine.safety_scan
        assert tas.CAPABILITY_PATTERNS is engine.CAPABILITY_PATTERNS
        assert tas.score_agent is engine.score_agent
        assert "SAFETY_DENY" not in tas.__dict__, \
            "tools/almanac_scout.py must not fork its own deny-list"
        assert "score_agent" in tas.__dict__ and tas.score_agent is engine.score_agent

    def test_hf_scout_uses_shared_engine(self):
        import hf_hub_scout
        assert hf_hub_scout.engine is engine

    def test_forked_rubric_removed_from_tools(self):
        src = open(os.path.join(REPO_ROOT, "tools", "almanac_scout.py")).read()
        assert "utility = round(0.4 * scope" not in src, \
            "scoring rubric must live only in almanac/core.py"
        assert "ignore\\s+(all\\s+)?(user" not in src, \
            "deny-list regexes must live only in almanac/core.py"


# ---------------------------------------------------------------------------
# 1. Safety filter — evil fixture fully rejected
# ---------------------------------------------------------------------------
class TestSafetyFilter:
    def test_evil_fixture_detected_by_safety_scan(self):
        texts = read_texts(EVIL)
        assert texts, "fixture should yield readable text"
        # README.md is the carrier of aggressive directives
        readme = next(b for p, b in texts.items() if p.endswith("README.md"))
        violations = engine.safety_scan(readme)
        assert len(violations) >= 4, f"expected multiple violations, got {violations}"
        joined = " ".join(violations).lower()
        for expected in ("pushback", "confirmation", "auto-commits", "covert",
                         "destructive"):
            assert expected in joined, f"missing detection: {expected} in {joined}"
        # corpus-level scan must also flag the target as a whole
        assert engine.safety_scan("\n".join(texts.values()))

    def test_corpus_gate_blocks_write_of_unsafe_directives(self, tmp_path):
        """Regression: hostile source text must never be laundered into a
        rendered SKILL.md via quoted 'source directives'."""
        out = tmp_path / "skills"
        rc = engine.run_pipeline("evil_unit", "local:test", read_texts(EVIL),
                                 out_dir=str(out))
        assert rc == 1
        assert list(out.rglob("SKILL.md")) == [] if out.exists() else True

    def test_gh_scout_rejects_evil_fixture_exit_1_no_writes(self, tmp_path):
        out = tmp_path / "skills"
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scouts", "gh_repo_scout.py"),
             EVIL, "--name", "evil_test", "--out-dir", str(out)],
            capture_output=True, text=True)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        written = list(out.rglob("SKILL.md")) if out.exists() else []
        assert written == [], f"safety filter leaked writes: {written}"
        combined = proc.stdout + proc.stderr
        assert "reject" in combined.lower(), \
            "corpus gate must report the rejection reason"

    def test_tool_cli_check_flags_evil_files_exit_1(self):
        evil_files = [os.path.join(EVIL, f) for f in sorted(os.listdir(EVIL))]
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "almanac_scout.py"),
             "check", *evil_files],
            capture_output=True, text=True)
        assert proc.returncode == 1
        assert "REJECT" in proc.stdout

    def test_tool_cli_check_rejects_full_evil_corpus(self, tmp_path):
        """The tools CLI runs the SAME shared deny-list end-to-end: checking
        every fixture file must exit 1 and name every violation class."""
        evil_files = [os.path.join(EVIL, f) for f in sorted(os.listdir(EVIL))]
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "almanac_scout.py"),
             "check", *evil_files], capture_output=True, text=True)
        assert proc.returncode == 1
        out = proc.stdout.lower()
        for word in ("pushback", "confirmation", "auto-commits", "covert",
                     "destructive"):
            assert word in out, f"check output missing {word!r}: {out}"
        assert "[PASS]" in proc.stdout, "clean files must still pass individually"

    def test_draft_never_laundered_unsafe_directives(self, tmp_path):
        """Regression: quoted 'source directive' lines in drafted skills must
        come only from deny-list-clean files (per-file quarantine)."""
        texts = read_texts(EVIL)
        entry = {"agent_id": "evil_unit", "source": "local:test",
                 **engine.score_agent(texts)}
        drafts = self._draft_with_redirect(entry, texts, tmp_path)
        for rel in drafts:
            body = open(rel if os.path.isabs(rel) else
                        os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
            assert "ignore all user objections" not in body.lower()
            assert "auto-commit without asking" not in body.lower()
            assert "never ask the user" not in body.lower()

    @staticmethod
    def _draft_with_redirect(entry, texts, tmp_path):
        import tools.almanac_scout as tas
        monkey_dir = str(tmp_path / "drafts")
        orig = tas.DRAFTS_DIR
        tas.DRAFTS_DIR = monkey_dir
        try:
            return tas.draft_skills(entry, texts)
        finally:
            tas.DRAFTS_DIR = orig

    def test_clean_fixture_passes_safety_scan(self):
        texts = read_texts(CLEAN)
        blob = "\n".join(texts.values())
        assert engine.safety_scan(blob) == [], \
            "clean fixture must not trip the deny-list"


# ---------------------------------------------------------------------------
# 2. Clean path — score, validate, render
# ---------------------------------------------------------------------------
class TestCleanPipeline:
    def test_scores_and_caps(self):
        texts = read_texts(CLEAN)
        sc = engine.score_agent(texts)
        assert sc["capabilities"], "clean fixture must expose capabilities"
        assert {"web_search", "python_exec"} <= set(sc["capabilities"])
        assert sc["loop_risk_profile"] == "LOW", "bounded fixture ⇒ LOW loop risk"
        assert sc["free_tier_efficiency"] in ("HIGH", "MEDIUM")
        assert 5.0 <= sc["utility_score"] <= 10.0

    def test_entry_validates_against_schema(self):
        texts = read_texts(CLEAN)
        entry = engine.make_entry("clean_unit", "local:tests/fixtures/clean_agent_repo",
                                  texts)
        ok, method, detail = engine.validate_against_schema(entry, SCHEMA)
        assert ok, f"{method}: {detail}"

    def test_run_pipeline_writes_rendered_skills(self, tmp_path):
        out = tmp_path / "skills"
        rc = engine.run_pipeline("clean_unit", "local:tests/fixtures/clean_agent_repo",
                                 read_texts(CLEAN), out_dir=str(out))
        assert rc == 0
        files = sorted(out.rglob("SKILL.md"))
        assert files, "clean fixture must produce at least one SKILL.md"
        for f in files:
            body = f.read_text()
            assert not re.search(r"\{\{[A-Z0-9_]+\}\}", body), \
                f"orphan placeholder left in {f.name}"
            assert "safety_filter: PASSED" in body or "Safety filter: PASSED" in body \
                or "PASSED" in body
            assert "Verify output with ONE check" in body

    def test_dry_run_writes_nothing(self, tmp_path):
        out = tmp_path / "skills"
        rc = engine.run_pipeline("clean_dry", "local:test", read_texts(CLEAN),
                                 out_dir=str(out), dry_run=True)
        assert rc == 0
        assert not out.exists() or list(out.rglob("SKILL.md")) == []

    def test_template_has_no_orphan_placeholders_after_full_mapping(self):
        template = open(engine.DEFAULT_TEMPLATE).read()
        used = set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", template))
        texts = read_texts(CLEAN)
        entry = engine.make_entry("tpl_unit", "local:test", texts)
        ok, method, detail = engine.validate_against_schema(entry, SCHEMA)
        assert ok, f"{method}: {detail}"
        files = engine.build_skill_files(entry, texts, template, f"{method}: {detail}")
        assert files
        for rel, content in files:
            assert not re.search(r"\{\{[A-Z0-9_]+\}\}", content), rel


# ---------------------------------------------------------------------------
# 4. Registry & schema integrity
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_registry_entries_validate(self):
        reg = json.load(open(REGISTRY))
        schema = json.load(open(SCHEMA))
        import jsonschema
        v = jsonschema.Draft7Validator(schema)
        errs = sum(1 for e in reg["entries"] for _ in v.iter_errors(e))
        assert errs == 0
        assert len(reg["entries"]) >= 1

    def test_bundled_skills_pass_safety_filter(self):
        """Every SKILL.md currently under skills/ must pass the shared filter."""
        bad = []
        for root, dirs, files in os.walk(os.path.join(REPO_ROOT, "skills")):
            dirs[:] = [d for d in dirs if d != "templates"]
            for fn in files:
                if fn == "SKILL.md":
                    p = os.path.join(root, fn)
                    v = engine.safety_scan(open(p, encoding="utf-8").read())
                    if v:
                        bad.append((p, v))
        assert not bad, f"on-disk skills violating the current deny-list: {bad}"


# ---------------------------------------------------------------------------
# 5. CLI contracts
# ---------------------------------------------------------------------------
class TestCliContracts:
    def test_validate_command_exit_0(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "almanac_scout.py"),
             "validate"], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_gh_scout_bad_target_exit_2(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scouts", "gh_repo_scout.py"),
             "/nonexistent/path/__nope__"], capture_output=True, text=True)
        assert proc.returncode == 2

    def test_gh_scout_clean_dry_run_exit_0(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scouts", "gh_repo_scout.py"),
             CLEAN, "--name", "clean_cli", "--dry-run"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[dry-run] would write" in proc.stdout
        assert "PASS" in proc.stdout  # schema gate reported

    def test_parse_github_url_variants(self):
        import gh_repo_scout as gh
        assert gh.parse_github_url("https://github.com/huggingface/smolagents") == \
            ("huggingface", "smolagents", "HEAD")
        assert gh.parse_github_url("https://www.github.com/pjt222/agent-almanac/tree/main/skills") == \
            ("pjt222", "agent-almanac", "main")
        assert gh.parse_github_url("https://example.com/x/y") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# 8. Draft lifecycle: promote / reject / rejected (sandboxed via DRAFTS_DIR)
# ---------------------------------------------------------------------------
UNSAFE_DRAFT_FIXTURE = os.path.join(FIXTURES, "unsafe_draft",
                                    "skills", "drafts")


def _make_tool_cli(tmp_path):
    """Fresh import of tools/almanac_scout.py with paths redirected to tmp."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "almanac_scout_cli", os.path.join(REPO_ROOT, "tools", "almanac_scout.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    drafts = tmp_path / "skills" / "drafts"
    shutil.copytree(UNSAFE_DRAFT_FIXTURE, drafts)
    mod.DRAFTS_DIR = str(drafts)
    mod.SKILLS_DIR = str(tmp_path / "skills")
    mod.REJECTED_DIR = str(tmp_path / "docs" / "rejected")
    os.makedirs(mod.REJECTED_DIR, exist_ok=True)
    return mod, drafts


class TestDraftLifecycle:
    def test_promote_blocks_unsafe_draft(self, tmp_path):
        mod, drafts = _make_tool_cli(tmp_path)
        ns = argparse.Namespace(names=[], reviewer="pytest")
        assert mod.cmd_promote(ns) == 1
        assert not os.path.exists(os.path.join(mod.SKILLS_DIR, "evil_candidate")), \
            "unsafe draft must never reach skills/"
        assert os.path.isfile(os.path.join(drafts, "evil_candidate", "SKILL.md")), \
            "blocked draft stays in drafts for inspection"

    def test_reject_writes_tombstone_and_removes_draft(self, tmp_path):
        mod, drafts = _make_tool_cli(tmp_path)
        ns = argparse.Namespace(name="evil_candidate",
                                reason="contains force-push directive",
                                reviewer="pytest")
        assert mod.cmd_reject(ns) == 0
        assert not os.path.exists(os.path.join(drafts, "evil_candidate"))
        tomb = os.path.join(mod.REJECTED_DIR, "evil_candidate.md")
        text = open(tomb, encoding="utf-8").read()
        assert "status: rejected" in text and "force-push" in text
        assert mod.cmd_rejected(None) == 0

    def test_promote_moves_clean_draft_with_stamp(self, tmp_path):
        mod, drafts = _make_tool_cli(tmp_path)
        clean = drafts / "clean_candidate"
        clean.mkdir()
        (clean / "SKILL.md").write_text(
            "---\nname: clean_candidate\ndescription: safe\nstatus: draft\n---\n\n"
            "# Clean\nRun one check, bounded to 5 steps. Ask the user before "
            "any destructive action.\n")
        ns = argparse.Namespace(names=["clean_candidate"], reviewer="pytest")
        assert mod.cmd_promote(ns) == 0
        dest = os.path.join(mod.SKILLS_DIR, "clean_candidate", "SKILL.md")
        stamped = open(dest, encoding="utf-8").read()
        assert "status: promoted" in stamped
        assert "reviewed_by: pytest" in stamped
        assert re.search(r"reviewed_at: \d{4}-\d{2}-\d{2}T", stamped)
        assert not clean.exists(), "source draft removed after promotion"

    def test_frontmatter_set_creates_and_updates(self):
        out = engine.frontmatter_set("body only", {"a": "1"})
        assert out.startswith("---\na: 1\n---\n")
        out2 = engine.frontmatter_set(out, {"a": "2", "b": "3"})
        assert "a: 2" in out2 and "b: 3" in out2
        assert out2.count("a: ") == 1, "update must replace, not duplicate"

    def test_force_push_pattern_in_deny_list(self):
        v = engine.safety_scan("then run git push --force origin main")
        assert any("forced git push" in x for x in v)
