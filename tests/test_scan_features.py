"""Tests for scan-features, update-feature, _instance_to_feat_prefix,
and the scan-endpoints Cypher injection fix.

No live database required — uses the fake_db harness from test_cli_harness.py.
Run: python3 -m pytest tests/ -v
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "sys_edge.py"
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_sf_under_test", _SCRIPT)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


# ── shared fake-DB infrastructure ────────────────────────────────────────────

class _Result:
    def data(self):    return []
    def single(self):  return {}
    def value(self):   return []
    def consume(self): return None
    def __iter__(self): return iter([])


class _Session:
    def __init__(self, log):
        self.log = log

    def run(self, query, **_params):
        self.log.append(query.strip())
        return _Result()

    def __enter__(self):  return self
    def __exit__(self, *a): return False
    def close(self):      pass


class _Driver:
    def __init__(self):
        self.log = []

    def session(self, **_):
        return _Session(self.log)

    def close(self):
        pass


@pytest.fixture
def fake_driver(monkeypatch):
    drv = _Driver()
    monkeypatch.setattr(se, "_driver", lambda *a, **kw: drv)
    monkeypatch.setattr(se, "_root", Path(_ROOT))   # real repo root so git works
    monkeypatch.setattr(se, "_require_licence", lambda *a, **k: None)
    return drv


# ── _instance_to_feat_prefix ──────────────────────────────────────────────────

@pytest.mark.parametrize("instance,expected_prefix", [
    ("",            "F-AUTO-"),
    ("sys-edge",    "F-SE-"),
    ("master",      "F-MAST-"),       # single part → up to 4 chars
    ("ce-architect","F-CA-"),
    ("plan",        "F-PLAN-"),
    ("ce-kernel",   "F-CK-"),
    ("deploy",      "F-DEPL-"),
])
def test_instance_to_feat_prefix(instance, expected_prefix):
    assert se._instance_to_feat_prefix(instance) == expected_prefix


# ── scan-endpoints Cypher injection ───────────────────────────────────────────

def test_scan_endpoints_instance_filter_uses_where_not_and(fake_driver, monkeypatch, tmp_path):
    """The injected filter must start with WHERE, not AND (invalid Cypher)."""
    # Patch _scan_go_routes to return empty so the command reaches the Cypher call
    monkeypatch.setattr(se, "_scan_go_routes", lambda *a, **k: [])
    se.cmd_scan_endpoints(SimpleNamespace(
        path=str(tmp_path),
        instance="core",
        apply=False,
        binary="",
    ))
    cypher_calls = "\n".join(fake_driver.log)
    # Must not produce "MATCH ... AND EXISTS" without a WHERE
    assert "AND EXISTS" not in cypher_calls or "WHERE EXISTS" in cypher_calls, (
        "scan-endpoints injected 'AND EXISTS' without 'WHERE' — invalid Cypher"
    )
    assert "WHERE EXISTS" in cypher_calls


def test_scan_endpoints_no_instance_no_where_clause(fake_driver, monkeypatch, tmp_path):
    """Without --instance, no WHERE clause should be injected."""
    monkeypatch.setattr(se, "_scan_go_routes", lambda *a, **k: [])
    se.cmd_scan_endpoints(SimpleNamespace(
        path=str(tmp_path),
        instance="",
        apply=False,
        binary="",
    ))
    cypher_calls = "\n".join(fake_driver.log)
    assert "WHERE EXISTS" not in cypher_calls


# ── scan-features argument validation ────────────────────────────────────────

def test_scan_features_feature_without_enh_raises(fake_driver, monkeypatch):
    """--feature without --enh must exit with an error."""
    with pytest.raises(SystemExit) as exc:
        se.cmd_scan_features(SimpleNamespace(
            since="HEAD~1",
            enh="",
            feature="F-ARCH-001",
            instance="",
            module="",
            yes=False,
            dry_run=True,
        ))
    assert exc.value.code == 1


def test_scan_features_bad_sha_exits_1(fake_driver, monkeypatch):
    """An invalid git ref must exit with code 1."""
    with pytest.raises(SystemExit) as exc:
        se.cmd_scan_features(SimpleNamespace(
            since="NOT-A-REAL-SHA-deadbeef1234",
            enh="",
            feature="",
            instance="",
            module="",
            yes=False,
            dry_run=True,
        ))
    assert exc.value.code == 1


def test_scan_features_dry_run_no_writes(fake_driver, monkeypatch):
    """--dry-run must not call any graph write (MERGE/SET/CREATE) queries."""
    # Patch git calls so the command reaches proposal stage then stops
    import subprocess as _sp
    real_run = _sp.run

    def _fake_sp(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if cmd[0] == "git" and "rev-parse" in cmd:
            # Return a valid-looking SHA
            r.stdout = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        elif cmd[0] == "git" and "cat-file" in cmd:
            r.stdout = "commit"
        elif cmd[0] == "git" and "diff" in cmd:
            r.stdout = ""   # no changed files → command should exit cleanly
        elif cmd[0] == "git" and "log" in cmd:
            r.stdout = "2026-01-01T00:00:00+00:00"
        return r

    monkeypatch.setattr(_sp, "run", _fake_sp)

    # Should print "Nothing to do" and return without touching the DB
    se.cmd_scan_features(SimpleNamespace(
        since="",
        enh="",
        feature="",
        instance="",
        module="",
        yes=False,
        dry_run=True,
    ))
    write_queries = [q for q in fake_driver.log
                     if any(w in q.upper() for w in ("MERGE", "CREATE", "SET "))]
    assert write_queries == [], f"dry-run should not write; got: {write_queries}"


# ── update-feature ────────────────────────────────────────────────────────────

def test_update_feature_unknown_id_exits_1(fake_driver, monkeypatch):
    """Updating a non-existent feature must exit 1."""
    with pytest.raises(SystemExit) as exc:
        se.cmd_update_feature(SimpleNamespace(
            id="F-DOES-NOT-EXIST",
            name="",
            description="",
            narrative="",
            source="",
            status="",
            module="",
        ))
    assert exc.value.code == 1


def test_update_feature_no_fields_exits_cleanly(fake_driver, monkeypatch, capsys):
    """Calling update-feature with no update fields must not traceback."""
    # Make the MATCH return a row so the feature "exists"
    class _FoundResult:
        def single(self): return {"id": "F-TEST-001", "name": "Test"}
        def data(self): return []
        def __iter__(self): return iter([])

    class _FoundSession:
        def run(self, *a, **k): return _FoundResult()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

    class _FoundDriver:
        def session(self, **_): return _FoundSession()
        def close(self): pass

    monkeypatch.setattr(se, "_driver", lambda *a, **kw: _FoundDriver())
    se.cmd_update_feature(SimpleNamespace(
        id="F-TEST-001",
        name="",
        description="",
        narrative="",
        source="",
        status="",
        module="",
    ))
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_update_feature_invalid_status_exits_1(fake_driver, monkeypatch):
    """An invalid --status value must exit 1."""
    class _FoundResult:
        def single(self): return {"id": "F-TEST-001", "name": "Test"}
        def data(self): return []
        def __iter__(self): return iter([])

    class _FoundSession:
        def run(self, *a, **k): return _FoundResult()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

    class _FoundDriver:
        def session(self, **_): return _FoundSession()
        def close(self): pass

    monkeypatch.setattr(se, "_driver", lambda *a, **kw: _FoundDriver())
    with pytest.raises(SystemExit) as exc:
        se.cmd_update_feature(SimpleNamespace(
            id="F-TEST-001",
            name="",
            description="",
            narrative="",
            source="",
            status="NotAStatus",
            module="",
        ))
    assert exc.value.code == 1
