"""Unit tests for repo-relative test paths and `sys_edge.py check-test-links`.

Resolver tests run against this repository's real files; the command tests use
a recording fake Neo4j session. Run: python3 -m pytest tests/ -v
"""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_links_under_test",
                                               _ROOT / "scripts" / "sys_edge.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


@pytest.fixture(autouse=True)
def _pin_repo_id(monkeypatch):
    monkeypatch.setattr(se, "_repo_id", lambda: "acme/app")


# ── Path normalisation and resolution ─────────────────────────────────────────

def test_repo_rel_path_normalises_formats():
    root = str(se._root.resolve())
    assert se._repo_rel_path(f"{root}/scripts/sys_edge.py") == "scripts/sys_edge.py"
    assert se._repo_rel_path("scripts\\sys_edge.py") == "scripts/sys_edge.py"
    assert se._repo_rel_path("./scripts/sys_edge.py") == "scripts/sys_edge.py"
    assert se._repo_rel_path("/elsewhere/core/x_test.go") == "/elsewhere/core/x_test.go"
    assert se._repo_rel_path("") == ""


@pytest.mark.parametrize("stored, repo, expected", [
    ("scripts/sys_edge.py", "", ("ok", "scripts/sys_edge.py")),
    ("scripts/sys_edge.py", "acme/app", ("ok", "scripts/sys_edge.py")),
    ("scripts\\sys_edge.py", "", ("fixed", "scripts/sys_edge.py")),
    ("examples/arch-standards.yaml", "", ("ok", "examples/arch-standards.yaml")),
    ("core/record_model_test.go", "", ("missing", "core/record_model_test.go")),
    ("scripts/sys_edge.py", "acme/other", ("foreign", "scripts/sys_edge.py")),
    ("/elsewhere/core/x_test.go", "", ("foreign", "/elsewhere/core/x_test.go")),
    ("", "", ("nofile", "")),
])
def test_resolve_test_file(stored, repo, expected):
    assert se._resolve_test_file(stored, repo) == expected


def test_resolve_absolute_path_inside_repo_is_fixed():
    stored = f"{se._root.resolve()}/scripts/sys_edge.py"
    assert se._resolve_test_file(stored) == ("fixed", "scripts/sys_edge.py")


def test_repo_id_uses_owner_and_name(monkeypatch):
    monkeypatch.undo()  # drop the autouse pin
    se._repo_id.cache_clear()
    monkeypatch.setenv("SYSEDGE_REPO", "")
    run = lambda *a, **k: SimpleNamespace(stdout="git@github.com:acme/app.git\n")
    monkeypatch.setattr(se.subprocess, "run", run)
    try:
        assert se._repo_id() == "acme/app"
    finally:
        se._repo_id.cache_clear()


# ── check-test-links ──────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def single(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, log, tests, pkgs, existing):
        self.log, self.tests, self.pkgs, self.existing = log, tests, pkgs, existing

    def run(self, query, **params):
        self.log.append((" ".join(query.split()), params))
        q = " ".join(query.split())
        if q.startswith("MATCH (t:SysTest) OPTIONAL MATCH"):
            return _Result(self.tests)
        if q.startswith("MATCH (p:SysTestPackage) RETURN"):
            return _Result(self.pkgs)
        if "RETURN count(n) AS c" in q:
            return _Result([{"c": 1 if params["id"] in self.existing else 0}])
        if "RETURN type(r) AS t" in q:
            return _Result([{"t": "VERIFIES", "outgoing": True, "oid": "4:x:1",
                             "props": {"confidence": 0.9}}])
        return _Result([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def links_cli(monkeypatch):
    log = []
    root = str(se._root.resolve())
    tests = [
        {"id": "scripts/sys_edge.py::test_ok", "file": "scripts/sys_edge.py", "repo": None, "links": 1},
        {"id": f"{root}/scripts/sys_edge.py::test_abs", "file": f"{root}/scripts/sys_edge.py",
         "repo": None, "links": 2},
        {"id": "core/record_model_test.go::TestX", "file": "core/record_model_test.go",
         "repo": None, "links": 1},
        {"id": "x/y_test.go::TestY", "file": "x/y_test.go", "repo": "acme/other", "links": 1},
        {"id": "T-legacy", "file": None, "repo": None, "links": 1},
    ]
    pkgs = [{"id": "scripts\\sys_edge.py", "file": "scripts\\sys_edge.py", "repo": None}]
    existing = {"scripts/sys_edge.py::test_abs"}

    class _Driver:
        def session(self):
            return _Session(log, tests, pkgs, existing)

        def close(self):
            pass

    monkeypatch.setattr(se, "_driver", lambda *a, **k: _Driver())

    def _run(**flags):
        opts = {"fix": False, "yes": False, "verbose": False, **flags}
        se.cmd_check_test_links(SimpleNamespace(**opts))
        return log
    return _run


def _writes(log):
    return [(q, p) for q, p in log if any(w in q for w in ("SET", "DELETE", "MERGE"))]


def test_dry_run_reports_and_writes_nothing(links_cli, capsys):
    log = links_cli()
    out = capsys.readouterr().out
    assert "3 of 5 linked tests (60.0%) do not resolve" in out  # legacy T- excluded from nofile
    assert "→  scripts/sys_edge.py" in out
    assert "dry run — --fix re-keys 2 and deletes 1 nodes" in out
    assert _writes(log) == []


def test_fix_rekeys_deletes_and_tags(links_cli, capsys):
    log = links_cli(fix=True, yes=True)
    writes = _writes(log)
    joined = "\n".join(q for q, _ in writes)
    # Absolute-path test merged into the existing repo-relative node, relationship props kept
    rekey = [p for q, p in writes if q.startswith("MATCH (old:SysTest {") and "MERGE (new" in q]
    assert [p["new"] for p in rekey] == ["scripts/sys_edge.py::test_abs"]
    assert any("SET r += $props" in q and p["props"] == {"confidence": 0.9} for q, p in writes)
    # Backslash package id re-keyed too
    assert any(q.startswith("MATCH (old:SysTestPackage") and p["new"] == "scripts/sys_edge.py"
               for q, p in writes if "MERGE (new" in q)
    # Missing file deleted; foreign repo untouched
    delete = [p for q, p in writes if "MATCH (t:SysTest {id: tid}) DETACH DELETE t" in q][0]
    assert delete["tids"] == ["core/record_model_test.go::TestX"]
    assert "x/y_test.go::TestY" not in str(writes)
    # Remaining in-repo nodes get the repo tag
    tag = [p for q, p in writes if "WHERE t.repo IS NULL SET t.repo" in q][0]
    assert tag == {"ids": ["scripts/sys_edge.py::test_ok"], "repo": "acme/app"}
    assert "Re-keyed 2 nodes (1 merged into existing), deleted 1" in capsys.readouterr().out
    assert "DETACH DELETE old" in joined


def test_test_file_falls_back_to_id_prefix():
    assert se._test_file_of({"id": "backend/tests/test_x.py::TestA", "file": None}) == "backend/tests/test_x.py"
    assert se._test_file_of({"id": "a.py::T", "file": "b.py"}) == "b.py"
    assert se._test_file_of({"id": "T-legacy", "file": ""}) == ""
