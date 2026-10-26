"""Regression: raw S.q() calls must use the 'props' column, not 'data'.

The nodes table schema defines the JSON column as `props`.  Several commands
use inline S.q("SELECT ... data FROM nodes ...") instead of the high-level
helpers, so a wrong column name silently passes all other tests but raises
OperationalError at runtime.

Each test here seeds a minimal graph and calls the affected command end-to-end
against a real SQLite database.  A schema mismatch causes an immediate failure.
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


be = _load("sys_edge_sqlite_reg", _ROOT / "scripts" / "sys_edge_sqlite.py")
se = _load("sys_edge_cli_reg",    _ROOT / "scripts" / "sys_edge.py")


class _Parsed(Exception):
    def __init__(self, ns): self.ns = ns


def _parse(argv):
    orig = argparse.ArgumentParser.parse_args
    old_argv = sys.argv
    def fake(self, args=None, namespace=None):
        raise _Parsed(orig(self, args, namespace))
    argparse.ArgumentParser.parse_args = fake
    sys.argv = ["sys_edge.py", *argv]
    try:
        se.main()
    except _Parsed as p:
        return p.ns
    finally:
        argparse.ArgumentParser.parse_args = orig
        sys.argv = old_argv
    raise AssertionError("parser did not run")


def _cli(env, capsys, *argv):
    ns = _parse(list(argv))
    code = 0
    try:
        be.run(ns.cmd, ns, env)
    except SystemExit as e:
        code = e.code or 0
    out, err = capsys.readouterr() if capsys is not None else ("", "")
    return code, out, err


SEED = {
    "_schema_version": 3,
    "systems":  [{"id": "SYS-1", "name": "Demo"}],
    "domains":  [{"id": "DOM-1", "name": "Core", "systemId": "SYS-1"}],
    "modules":  [{"id": "MOD-api", "name": "API", "instance": "api",
                  "domainId": "DOM-1", "codePaths": ["src/api"], "testPaths": ["tests/api"]}],
    "features": [{"id": "F-API-001", "name": "Login",  "status": "Implemented", "moduleId": "MOD-api"},
                 {"id": "F-API-002", "name": "Logout", "status": "Implemented", "moduleId": "MOD-api"}],
    "userStories": [{"id": "US-001", "title": "Sign in", "requiresFeatures": ["F-API-001"]}],
    "useCases":    [{"id": "UC-001", "title": "User signs in", "instance": "api",
                     "storyId": "US-001", "requiresFeatures": ["F-API-001"]}],
    "testPackages": [{"id": "tests/api/test_auth.py", "testCategory": "integration",
                      "area": "api", "path": "tests/api/test_auth.py"}],
    "tests": [
        {"id": "tests/api/test_auth.py::test_login", "file": "tests/api/test_auth.py",
         "packageId": "tests/api/test_auth.py", "verifiesFeatures": ["F-API-001"],
         "testType": "integration"},
        {"id": "tests/api/test_auth.py::test_logout", "file": "tests/api/test_auth.py",
         "packageId": "tests/api/test_auth.py", "verifiesFeatures": ["F-API-002"],
         "testType": "component"},
    ],
}

ARCH_STANDARDS = {
    "standards": [
        {"id": "AS-SEC-001", "domain": "Security", "title": "Input validation",
         "description": "All inputs validated at boundary"},
    ],
    "qualityDimensions": [
        {"id": "AS-REQ-001", "aspect": "completeness",
         "question": "Are all happy-path steps listed?"},
        {"id": "AS-REQ-002", "aspect": "testability",
         "question": "Can each step be verified by a test?"},
        {"id": "AS-TEST-UC-001", "aspect": "uc-coverage",
         "question": "Does the test cover the main success path?"},
        {"id": "AS-TEST-COMP-001", "aspect": "component-coverage",
         "question": "Does every branch have a component test?"},
        {"id": "AS-TEST-US-001", "aspect": "e2e-coverage",
         "question": "Does the E2E test cover the full user journey?"},
    ],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSEDGE_SQLITE_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("SYSEDGE_REPO", "acme/demo")
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(SEED), encoding="utf-8")
    stds = tmp_path / "standards.yaml"
    import yaml
    stds.write_text(yaml.safe_dump(ARCH_STANDARDS), encoding="utf-8")
    # create the test files that audit-test will try to read
    (tmp_path / "tests" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "api" / "test_auth.py").write_text(
        "def test_login(): pass\ndef test_logout(): pass\n", encoding="utf-8")
    assert _cli(tmp_path, None, "init")[0] == 0
    code, _, err = _cli(tmp_path, None, "seed", str(seed))
    assert code == 0, err
    return tmp_path


@pytest.fixture
def env_with_standards(env, capsys):
    stds = env / "standards.yaml"
    code, _, err = _cli(env, capsys, "seed-standards", str(stds))
    assert code == 0, err
    return env


# Each test calls one command that previously used "data" instead of "props"
# in a raw S.q() call.  If the column name is wrong, sqlite3.OperationalError
# is raised and the test fails with a clear message.

def test_coverage_review_does_not_crash(env_with_standards, capsys):
    code, out, err = _cli(env_with_standards, capsys, "coverage-review", "--instance", "api")
    assert code == 0, f"coverage-review crashed:\n{err}"


def test_quality_review_does_not_crash(env_with_standards, capsys):
    code, out, err = _cli(env_with_standards, capsys, "quality-review", "--instance", "api")
    assert code == 0, f"quality-review crashed:\n{err}"


def test_audit_test_does_not_crash(env_with_standards, capsys):
    # --analyse emits the request JSON rather than calling an AI provider
    code, out, err = _cli(env_with_standards, capsys,
                          "audit-test", "--feature", "F-API-001", "--analyse")
    assert code == 0, f"audit-test crashed:\n{err}"


def test_run_history_does_not_crash(env, capsys):
    code, out, err = _cli(env, capsys, "run-history", "--instance", "api")
    assert code == 0, f"run-history crashed:\n{err}"


def test_trace_matrix_does_not_crash(env, capsys):
    code, out, err = _cli(env, capsys, "trace-matrix", "--instance", "api")
    assert code == 0, f"trace-matrix crashed:\n{err}"


def test_enh_align_does_not_crash(env, capsys):
    S = be.Store(env / "graph.sqlite3")
    S.upsert("SysEnhancement", "ENH-1", {"title": "Add MFA", "instance": "api",
                                          "status": "proposed", "priority": "Must"})
    S.link("SysEnhancement", "ENH-1", "EXTENDS", "SysFeature", "F-API-001")
    S.close()
    # --id is the correct flag (not --enh)
    code, out, err = _cli(env, capsys, "enh-align", "--id", "ENH-1")
    assert code == 0, f"enh-align crashed:\n{err}"


def test_uc_state_check_does_not_crash(env, capsys):
    code, out, err = _cli(env, capsys, "uc-state-check", "--uc", "UC-001")
    assert code == 0, f"uc-state-check crashed:\n{err}"


def test_deliver_brief_does_not_crash(env, capsys):
    S = be.Store(env / "graph.sqlite3")
    S.upsert("SysEnhancement", "ENH-2", {"title": "Rate limiting", "instance": "api",
                                          "status": "proposed", "priority": "Must"})
    S.link("SysEnhancement", "ENH-2", "EXTENDS", "SysFeature", "F-API-001")
    S.close()
    code, out, err = _cli(env, capsys, "deliver-brief", "--enh", "ENH-2")
    assert code == 0, f"deliver-brief crashed:\n{err}"


def test_scan_tests_finds_functions(env, capsys):
    # Regression: ast was missing from imports; _scan_python_tests silently returned []
    test_file = env / "tests" / "api" / "test_auth.py"
    code, out, err = _cli(env, capsys, "scan-tests", "--path", str(test_file))
    assert code == 0, f"scan-tests crashed:\n{err}"
    # Must have registered at least 1 test function
    S = be.Store(env / "graph.sqlite3")
    count = len(S.q("SELECT id FROM nodes WHERE label='SysTest'"))
    S.close()
    assert count > 0, "scan-tests registered no test functions — likely missing 'import ast'"
