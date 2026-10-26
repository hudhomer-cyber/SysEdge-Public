"""Unit tests for `sys_edge.py export --type detailed-design`.

Runs against a fake Neo4j session — no database required.
Run: python3 -m pytest tests/ -v
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "sys_edge.py"
_MAKE_LITE = _ROOT / "setup" / "make_lite.py"

# sys_edge.py exits at import without a password; the fake driver never uses it.
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_under_test", _SCRIPT)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


# ── Fake graph ────────────────────────────────────────────────────────────────

GRAPH = {
    "modules": [
        {"m": {"id": "MOD-auth", "name": "Auth", "instance": "core", "type": "backend",
               "paths": ["backend/internal/auth"]}},
        {"m": {"id": "MOD-plan", "name": "Planning", "instance": "plan"}},
    ],
    "features": [
        {"f": {"id": "F-AUTH-001", "name": "Login", "status": "Done",
               "description": "OIDC login"}, "mods": ["MOD-auth"]},
        {"f": {"id": "F-AUTH-002", "name": "Logout", "status": "Done"}, "mods": ["MOD-auth"]},
        {"f": {"id": "F-PLAN-001", "name": "Plan board", "status": "Done"}, "mods": ["MOD-plan"]},
        {"f": {"id": "F-LOOSE", "name": "Unowned", "status": "Proposed", "instance": "core"},
         "mods": []},
    ],
    "usecases": [
        {"uc": {"id": "UC-AUTH-001", "title": "Sign in", "instance": "core", "priority": "P1",
                "preconditions": "User has an account\nIdP reachable",
                "mainFlow": "1. Open app\n2. Redirect to IdP\n3. Return with session",
                "postconditions": ["Session cookie set"],
                "authorizedRoles": "viewer, manager",
                "failureScenarios": "- IdP down -> error page"},
         "feats": ["F-AUTH-001"]},
        {"uc": {"id": "UC-AUTH-002", "title": "Sign out", "instance": "core"},
         "feats": ["F-AUTH-002"]},
        {"uc": {"id": "UC-ORPHAN", "title": "Floating UC", "instance": "core"}, "feats": []},
        {"uc": {"id": "UC-PLAN-001", "title": "View board", "instance": "plan"},
         "feats": ["F-PLAN-001"]},
    ],
    "stories": [
        {"us": {"id": "US-001", "title": "Log in securely", "actor": "employee",
                "goal": "to sign in with SSO", "benefit": "I avoid another password",
                "priority": "Must", "acceptanceCriteria": ["Redirects to IdP", "Lands on home"]},
         "ucs": ["UC-AUTH-001"], "feats": []},
        {"us": {"id": "US-002", "title": "Direct story", "actor": "admin", "goal": "x"},
         "ucs": [], "feats": ["F-AUTH-002"]},
        {"us": {"id": "US-ORPHAN", "title": "Lonely story", "actor": "nobody",
                "instance": "core"}, "ucs": [], "feats": []},
        {"us": {"id": "US-PLAN", "title": "See plan"}, "ucs": ["UC-PLAN-001"], "feats": []},
    ],
    "tests": [
        {"t": {"id": "T-login-int", "file": "backend/tests/test_auth.py",
               "function": "test_login", "testType": "integration", "lastStatus": "passed",
               "lastTestedAt": "2026-10-01T10:00:00Z"},
         "feats": ["F-AUTH-001"], "pkgCat": "integration", "pkgArea": "core"},
        {"t": {"id": "T-login-cmp", "file": "frontend/login.test.tsx", "function": "renders|ok"},
         "feats": ["F-AUTH-001"], "pkgCat": "component", "pkgArea": "core"},
        {"t": {"id": "T-unlinked", "file": "backend/tests/test_misc.py", "function": "test_x"},
         "feats": [], "pkgCat": None, "pkgArea": "core"},
        {"t": {"id": "T-plan", "file": "frontend/board.test.tsx", "function": "test_board"},
         "feats": ["F-PLAN-001"], "pkgCat": None, "pkgArea": "plan"},
        # Link-integrity cases: excluded from coverage, listed in section 4
        {"t": {"id": "T-gone", "file": "gone/test_logout.py", "function": "test_logout"},
         "feats": ["F-AUTH-002"], "pkgCat": "integration", "pkgArea": "core"},
        {"t": {"id": "T-foreign", "file": "core/record_test.go", "repo": "acme/other",
               "function": "TestRecord"}, "feats": ["F-AUTH-001"], "pkgCat": None, "pkgArea": None},
        {"t": {"id": "T-nofile", "function": "test_x"}, "feats": ["F-AUTH-001"],
         "pkgCat": None, "pkgArea": "core"},
        {"t": {"id": "T-winpath", "file": "backend\\tests\\test_session.py",
               "function": "test_session"}, "feats": ["F-AUTH-001"], "pkgCat": "integration",
         "pkgArea": "core"},
    ],
}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


class _Session:
    def run(self, query, **_params):
        first = query.strip().splitlines()[0]
        for label, key in (("(m:SysModule)", "modules"), ("(f:SysFeature)", "features"),
                           ("(uc:SysUseCase)", "usecases"), ("(us:SysUserStory)", "stories"),
                           ("(t:SysTest)", "tests")):
            if first == f"MATCH {label}" or first.startswith(f"MATCH {label} RETURN"):
                return _Result(GRAPH[key])
        raise AssertionError(f"unexpected query: {query}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Driver:
    def session(self):
        return _Session()

    def close(self):
        pass


def _fake_resolve(file, repo=""):
    """Stand-in for _resolve_test_file: fixture paths don't exist on disk."""
    if not file:
        return "nofile", ""
    if repo and repo != "acme/app":
        return "foreign", file
    if file.startswith("gone/"):
        return "missing", file
    if "\\" in file:
        return "fixed", file.replace("\\", "/")
    return "ok", file


@pytest.fixture
def run_export(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "_driver", lambda *a, **k: _Driver())
    monkeypatch.setattr(se, "_resolve_test_file", _fake_resolve)
    monkeypatch.setattr(se, "_require_licence", lambda *a, **k: None)

    def _run(instance="", module="", fmt="md"):
        out = tmp_path / "dd.md"
        se.cmd_export(SimpleNamespace(type="detailed-design", instance=instance, module=module,
                                      format=fmt, output=str(out)))
        if fmt == "json":
            return json.loads(out.with_suffix(".json").read_text())
        return out.read_text()
    return _run


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_unscoped_document_has_all_sections(run_export):
    md = run_export()
    for heading in ("# Detailed Design — all", "## Summary", "## 1. Design by Module",
                    "## 2. Traceability Matrix", "## 3. Coverage Gaps", "## 4. Test Link Integrity",
                    "## 5. Orphan Nodes"):
        assert heading in md
    assert md.index("## 1. Design by Module") < md.index("## 2. Traceability Matrix") \
        < md.index("## 3. Coverage Gaps") < md.index("## 4. Test Link Integrity") \
        < md.index("## 5. Orphan Nodes")


def test_nested_chain_module_feature_usecase_story_test(run_export):
    md = run_export()
    order = [md.index(x) for x in ("### MOD-auth — Auth", "#### F-AUTH-001 — Login",
                                   "| backend/tests/test_auth.py |", "##### UC-AUTH-001 — Sign in",
                                   "###### US-001 — Log in securely")]
    assert order == sorted(order)
    assert '<a id="uc-auth-001"></a>' in md
    assert "As a **employee**, I want to sign in with SSO, so that I avoid another password.  \n" in md


def test_usecase_details_are_normalised(run_export):
    md = run_export()
    assert "1. Open app\n2. Redirect to IdP\n3. Return with session\n" in md
    assert "- User has an account\n- IdP reachable\n" in md
    assert "- Session cookie set\n" in md
    assert "- IdP down -> error page\n" in md
    assert "**Authorised roles:** viewer, manager" in md
    assert "1. Redirects to IdP\n2. Lands on home\n" in md


def test_test_table_escapes_pipes_and_uses_package_tier(run_export):
    md = run_export()
    assert "**Verified by tests (3 in 3 files):**" in md
    assert "| frontend/login.test.tsx | component | 1 | 1 not run | — | renders\\|ok |" in md
    assert "| backend/tests/test_auth.py | integration | 1 | 1 passed | 2026-10-01 | test_login |" in md
    assert "**Verified by tests (1 in 1 file):**" in md


def test_traceability_matrix_rows(run_export):
    md = run_export()
    matrix = md.split("## 2. Traceability Matrix")[1].split("## 3.")[0]
    assert "| [US-001](#us-001) | [UC-AUTH-001](#uc-auth-001) | [F-AUTH-001](#f-auth-001) " \
           "| MOD-auth | 3 | component, integration |" in matrix
    assert "| [US-002](#us-002) | — | [F-AUTH-002](#f-auth-002) | MOD-auth | ⚠ 0 |" in matrix
    assert "| [US-ORPHAN](#us-orphan) ⚠ | — | — |" in matrix
    assert "| — | [UC-AUTH-002](#uc-auth-002) | [F-AUTH-002](#f-auth-002)" in matrix


def test_gaps_and_orphans_are_marked(run_export):
    data = run_export(fmt="json")["data"]
    assert data["orphans"] == {"stories": ["US-ORPHAN"], "usecases": ["UC-ORPHAN"],
                               "features": ["F-LOOSE"], "tests": ["T-unlinked"]}
    assert data["gaps"]["features_without_tests"] == ["F-AUTH-002", "F-LOOSE"]
    assert data["gaps"]["usecases_without_stories"] == ["UC-AUTH-002"]
    assert data["gaps"]["stories_without_usecases"] == ["US-002"]
    md = run_export()
    orphans = md.split("## 5. Orphan Nodes")[1]
    for node in ("US-ORPHAN — Lonely story  ⚠ **ORPHAN**", "UC-ORPHAN — Floating UC  ⚠ **ORPHAN**",
                 "F-LOOSE — Unowned  ⚠ **ORPHAN**", "| backend/tests/test_misc.py | unclassified | 1 | test_x |"):
        assert node in orphans


def test_instance_scope_excludes_other_instances(run_export):
    md = run_export(instance="core")
    assert "MOD-plan" not in md and "UC-PLAN-001" not in md and "US-PLAN" not in md
    assert "board.test.tsx" not in md
    # Orphans carrying the instance property stay in scope
    assert "US-ORPHAN" in md and "F-LOOSE" in md and "test_misc.py" in md
    data = run_export(instance="core", fmt="json")["data"]
    assert data["counts"]["modules"] == 1


def test_module_scope(run_export):
    data = run_export(module="MOD-plan", fmt="json")["data"]
    assert [f["id"] for f in data["features"]] == ["F-PLAN-001"]
    assert [s["id"] for s in data["stories"]] == ["US-PLAN"]
    assert [t["id"] for t in data["tests"]] == ["T-plan"]
    assert data["orphans"] == {"stories": [], "usecases": [], "features": [], "tests": []}


def test_empty_graph(run_export, monkeypatch):
    monkeypatch.setattr(se, "_export_dd_collect",
                        lambda s: {k: [] for k in ("modules", "features", "usecases",
                                                   "stories", "tests")})
    md = run_export()
    assert "(no entries)" in md


def test_unresolved_test_links_excluded_from_coverage(run_export):
    md = run_export()
    body = md.split("## 2. Traceability Matrix")[0]
    for tid in ("T-gone", "T-foreign", "T-nofile"):
        assert tid not in body
    # F-AUTH-002's only test points at a deleted file, so it is still untested
    assert "| [US-002](#us-002) | — | [F-AUTH-002](#f-auth-002) | MOD-auth | ⚠ 0 |" in md
    # Normalised path is rendered in its repo-relative form
    assert "| backend/tests/test_session.py | integration | 1 |" in md
    integrity = md.split("## 4. Test Link Integrity")[1].split("## 5.")[0]
    assert "(1 only after path normalisation)" in integrity
    assert "### Belong to another repo (1)" in integrity
    assert "| T-foreign ⚠ | core/record_test.go | [F-AUTH-001](#f-auth-001) |" in integrity
    assert "### File not found in this repo (deleted, moved, or scanned from another repo) (1)" in integrity
    assert "4 linked tests resolve to a file in this repo (1 only after path normalisation); " \
           "3 do not" in integrity
    assert "| T-gone ⚠ | gone/test_logout.py | [F-AUTH-002](#f-auth-002) |" in integrity
    assert "### No file recorded (1)" in integrity
    assert "check-test-links" in integrity
    links = run_export(fmt="json")["data"]["test_links"]
    assert links == {"resolved": 4, "fixed_paths": 1, "unlinked_unresolved": 0,
                     "foreign": ["T-foreign"],
                     "missing": ["T-gone"], "nofile": ["T-nofile"]}


def test_list_normalisation():
    assert se._export_dd_list(None) == []
    assert se._export_dd_list('["a", "b"]') == ["a", "b"]
    assert se._export_dd_list("1) one\n* two\n\n") == ["one", "two"]
    assert se._export_dd_list(["x", " "]) == ["x"]


def _top_level_names(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_make_lite_stubs_detailed_design_helpers(tmp_path):
    pytest.skip("make_lite.py is an internal build tool not included in the public release")
