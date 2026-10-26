"""P-06 zero-Docker SQLite backend (scripts/sys_edge_sqlite.py).

Arguments are parsed by sys_edge.py's own argparse parser (captured from
main()), so these tests also check the backend accepts the real Namespace.
No Neo4j required. Run:
    python3 -m pytest tests/test_sqlite_backend.py -q -p no:cacheprovider
"""
import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


be = _load("sys_edge_sqlite_under_test", _ROOT / "scripts" / "sys_edge_sqlite.py")
se = _load("sys_edge_cli_for_sqlite_tests", _ROOT / "scripts" / "sys_edge.py")


class _Parsed(Exception):
    def __init__(self, ns):
        self.ns = ns


def parse(argv):
    """Parse argv with sys_edge.py's real parser (main() stops right after parse_args)."""
    orig = argparse.ArgumentParser.parse_args
    old_argv, old_cwd = sys.argv, os.getcwd()

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
        os.chdir(old_cwd)
    raise AssertionError("parser did not run")


NOW = datetime.now(timezone.utc).isoformat()

FIXTURE = {
    "_schema_version": 3,
    "systems": [{"id": "SYS-1", "name": "Demo"}],
    "domains": [{"id": "DOM-1", "name": "Core", "systemId": "SYS-1"}],
    "modules": [
        {"id": "MOD-api", "name": "API", "instance": "api", "domainId": "DOM-1",
         "codePaths": ["src/api"], "testPaths": ["tests/api"]},
        {"id": "MOD-web", "name": "Web", "instance": "web", "domainId": "DOM-1"},
    ],
    "features": [
        {"id": "F-API-001", "name": "Login", "status": "Implemented", "moduleId": "MOD-api"},
        {"id": "F-API-002", "name": "Logout", "status": "Implemented", "moduleId": "MOD-api",
         "description": "Ends the session token"},
        {"id": "F-API-003", "name": "Audit trail", "status": "Proposed", "moduleId": "MOD-api"},
        {"id": "F-WEB-001", "name": "Dashboard", "moduleId": "MOD-web"},
    ],
    "userStories": [{"id": "US-001", "title": "Sign in", "requiresFeatures": ["F-API-001"]}],
    "useCases": [{"id": "UC-001", "title": "User signs in", "instance": "api",
                  "storyId": "US-001", "requiresFeatures": ["F-API-001"]}],
    "testPackages": [{"id": "tests/api/test_auth.py", "testCategory": "integration", "area": "api",
                      "path": "tests/api/test_auth.py"}],
    "tests": [
        {"id": "tests/api/test_auth.py::test_login", "file": "tests/api/test_auth.py",
         "packageId": "tests/api/test_auth.py", "verifiesFeatures": ["F-API-001"]},
        {"id": "src/api/auth_test.go::TestLogout", "file": "src/api/auth_test.go",
         "testType": "go-unit", "verifiesFeatures": ["F-API-002"]},
    ],
    "enhancements": [
        {"id": "ENH-7", "title": "Add audit trail", "instance": "api", "priority": "Must",
         "status": "proposed", "extendsFeatures": ["F-API-003"]},
        {"id": "ENH-9", "title": "Remember me checkbox", "instance": "api", "priority": "Could",
         "status": "proposed", "extendsFeatures": ["F-API-001"]},
        {"id": "ENH-3", "title": "Old work", "instance": "api", "status": "done",
         "completedAt": "2020-01-01T00:00:00+00:00", "extendsFeatures": []},
    ],
    "defects": [
        {"id": "DEF-4", "title": "Logout leaves cookie", "severity": "high", "status": "open",
         "instance": "api", "affectsFeatures": ["F-API-002"]},
    ],
    "notes": [{"id": "NOTE-2", "instance": "api", "body": "Remember the token TTL", "status": "active",
               "createdAt": NOW}],
    "blockedBy": [{"fromId": "ENH-9", "toId": "ENH-7"}],
    "_relationships": [
        {"fromLabel": "SysNote", "from": "NOTE-2", "type": "NOTED_ON", "props": {"why": "ctx"},
         "toLabel": "SysEnhancement", "to": "ENH-7"},
        {"fromLabel": "SysModule", "from": "MOD-api", "type": "PROVIDES", "props": {},
         "toLabel": "SysFeature", "to": "F-API-001"},
        {"fromLabel": "SysModule", "from": "MOD-nope", "type": "PROVIDES", "props": {},
         "toLabel": "SysFeature", "to": "F-API-001"},
    ],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSEDGE_SQLITE_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("SYSEDGE_REPO", "acme/demo")
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return tmp_path


def cli(env, capsys, *argv):
    """Run one command; returns (exit code, stdout, stderr)."""
    ns = parse(list(argv))
    code = 0
    try:
        be.run(ns.cmd, ns, env)
    except SystemExit as e:
        code = e.code or 0
    out, err = capsys.readouterr()
    return code, out, err


def seeded(env, capsys):
    assert cli(env, capsys, "init")[0] == 0
    code, out, err = cli(env, capsys, "seed", str(env / "seed.json"))
    assert code == 0, err
    return out


def node(env, label, nid):
    con = sqlite3.connect(env / "graph.sqlite3")
    row = con.execute("SELECT props FROM nodes WHERE label=? AND id=?", (label, nid)).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


# ── module contract ──────────────────────────────────────────────────────────

def test_supported_commands_exist_in_cli_parser():
    for cmd in be.SUPPORTED:
        assert f'"{cmd}"' in (_ROOT / "scripts" / "sys_edge.py").read_text(encoding="utf-8")
    assert {"briefing", "worklog", "test-gaps", "seed", "backup"} <= be.SUPPORTED


def test_unsupported_command_exits_1(env, capsys):
    # a command not in _HANDLERS must exit(1) with a Neo4j hint
    ns = argparse.Namespace(cmd="neo4j-only-cmd")
    with pytest.raises(SystemExit) as e:
        be.run("neo4j-only-cmd", ns, env)
    assert e.value.code == 1
    assert "Neo4j" in capsys.readouterr().err


def test_missing_db_exits_2(env, capsys):
    code, _, err = cli(env, capsys, "worklog", "--instance", "api")
    assert code == 2 and "not found" in err


def test_unreadable_db_exits_2(env, capsys):
    (env / "graph.sqlite3").write_bytes(b"this is not a database at all" * 100)
    code, _, err = cli(env, capsys, "status")
    assert code == 2


def test_default_db_path(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSEDGE_SQLITE_PATH", raising=False)
    assert be.db_path(tmp_path) == tmp_path / "data" / "sysedge.sqlite3"


# ── seed / backup ────────────────────────────────────────────────────────────

def test_init_and_seed(env, capsys):
    out = seeded(env, capsys)
    assert "Seed complete" in out
    assert node(env, "SysFeature", "F-API-001")["name"] == "Login"
    assert "moduleId" not in node(env, "SysFeature", "F-API-001")
    con = sqlite3.connect(env / "graph.sqlite3")
    edges = {tuple(r) for r in con.execute("SELECT from_id, type, to_id FROM edges")}
    assert ("MOD-api", "PROVIDES", "F-API-001") in edges
    assert ("US-001", "REALIZED_BY", "UC-001") in edges
    assert ("UC-001", "REQUIRES", "F-API-001") in edges
    assert ("tests/api/test_auth.py", "CONTAINS_TEST", "tests/api/test_auth.py::test_login") in edges
    assert ("ENH-9", "BLOCKED_BY", "ENH-7") in edges
    assert ("NOTE-2", "NOTED_ON", "ENH-7") in edges
    assert ("SYS-1", "CONTAINS", "DOM-1") in edges
    assert not any(e[0] == "MOD-nope" for e in edges)  # dangling edge skipped
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
    assert (env / "sysedge.yaml").read_text().count("model: lightweight") == 1


def test_unfiltered_seed_into_non_empty_db_refused(env, capsys):
    seeded(env, capsys)
    code, _, err = cli(env, capsys, "seed", str(env / "seed.json"))
    assert code == 1 and "STOP" in err
    code, _, _ = cli(env, capsys, "seed", str(env / "seed.json"), "--instance", "api", "--no-backup")
    assert code == 0
    code, out, _ = cli(env, capsys, "seed", str(env / "seed.json"), "--full-restore", "--no-backup")
    assert code == 0 and "wiped" in out


def test_seed_never_reopens_done_or_closed(env, capsys):
    seeded(env, capsys)
    assert cli(env, capsys, "close-defect", "--id", "DEF-4")[0] == 0
    assert cli(env, capsys, "seed", str(env / "seed.json"), "--instance", "api", "--no-backup")[0] == 0
    assert node(env, "SysDefect", "DEF-4")["status"] == "closed"


def test_backup_round_trip(env, capsys, monkeypatch):
    seeded(env, capsys)
    a = env / "a.json"
    code, out, _ = cli(env, capsys, "backup", "--output", str(a))
    assert code == 0 and "Backup written" in out
    data_a = json.loads(a.read_text())
    assert data_a["_schema_version"] == 3
    feats = {f["id"]: f for f in data_a["features"]}
    assert feats["F-API-001"]["moduleId"] == "MOD-api"
    tests = {t["id"]: t for t in data_a["tests"]}
    assert tests["tests/api/test_auth.py::test_login"]["verifiesFeatures"] == ["F-API-001"]
    assert data_a["useCases"][0]["storyId"] == "US-001"
    assert data_a["blockedBy"] == [{"fromId": "ENH-9", "toId": "ENH-7"}]
    assert any(r["type"] == "NOTED_ON" and r["props"] == {"why": "ctx"} for r in data_a["_relationships"])

    fresh = env / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("SYSEDGE_SQLITE_PATH", str(fresh / "graph.sqlite3"))
    assert cli(fresh, capsys, "seed", str(a))[0] == 0
    b = env / "b.json"
    assert cli(fresh, capsys, "backup", "--output", str(b))[0] == 0
    data_b = json.loads(b.read_text())

    def norm(d):
        out = {}
        for k, v in d.items():
            if isinstance(v, list):
                out[k] = sorted(json.dumps(x, sort_keys=True) for x in v)
            else:
                out[k] = v
        return out

    assert norm(data_a) == norm(data_b)


def test_empty_backup_refused(env, capsys):
    cli(env, capsys, "init")
    out_file = env / "empty.json"
    code, _, err = cli(env, capsys, "backup", "--output", str(out_file))
    assert code == 1 and "nothing backed up" in err and not out_file.exists()
    code, _, _ = cli(env, capsys, "backup", "--output", str(out_file), "--allow-empty")
    assert code == 0 and out_file.exists()


def test_newer_schema_refused(env, capsys):
    cli(env, capsys, "init")
    f = env / "new.json"
    f.write_text(json.dumps({"_schema_version": 99}))
    code, _, err = cli(env, capsys, "seed", str(f))
    assert code == 1 and "newer" in err


def test_dry_run_writes_nothing(env, capsys):
    cli(env, capsys, "init")
    code, out, _ = cli(env, capsys, "seed", str(env / "seed.json"), "--dry-run")
    assert code == 0 and "DRY RUN" in out
    assert node(env, "SysFeature", "F-API-001") is None


# ── session-start views ──────────────────────────────────────────────────────

def test_briefing(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "briefing", "--instance", "api")
    assert code == 0
    assert "SYSTEM GRAPH BRIEFING — api" in out
    assert "MOD-api" in out and "tested: 2/3" in out
    assert "✓ F-API-001" in out and "✗ F-API-003" in out
    assert "~cmp 1/3" in out and "~int 1/3" in out
    assert "COVERAGE GAPS (1)" in out
    assert "ENH-7" in out and "DEF-4" in out
    assert "MOD-web" not in out


def test_worklog(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "worklog", "--instance", "api")
    assert code == 0
    assert out.index("OPEN DEFECTS (1)") < out.index("ENHANCEMENTS — PROPOSED")
    assert out.index("ENH-7") < out.index("ENH-9")  # Must before Could
    assert "⛔ BLOCKED BY: ENH-7" in out
    assert "NOTE-2" in out
    assert "ENH-3" not in out  # done long ago


def test_test_gaps(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "test-gaps", "--instance", "api")
    assert code == 0
    comp = out.split("COMPONENT GAPS")[1].split("INTEGRATION GAPS")[0]
    integ = out.split("INTEGRATION GAPS")[1]
    assert "F-API-001" in comp and "F-API-003" in comp and "F-API-002" not in comp
    assert "F-API-002" in integ and "F-API-001" not in integ.split("═")[0]
    code, out, _ = cli(env, capsys, "test-gaps", "--unlinked-packages")
    assert code == 0 and "P6 TEST PACKAGE GAPS" in out


def test_status_and_features(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "status")
    assert code == 0 and "api" in out and "DEF-4" in out
    code, out, _ = cli(env, capsys, "features", "--module", "MOD-api")
    assert code == 0 and "✓ F-API-001" in out and "✗ F-API-003" in out
    code, out, _ = cli(env, capsys, "show-feature", "--id", "F-API-002")
    assert code == 0 and "DEF-4" in out and "src/api/auth_test.go::TestLogout" in out
    code, _, err = cli(env, capsys, "show-feature", "--id", "UC-001")
    assert code == 1 and "show-usecase" in err


# ── work items ───────────────────────────────────────────────────────────────

def test_enhancement_lifecycle(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "create-enhancement", "--title", "Rate limit login",
                       "--instance", "api", "--priority", "Must", "--feature", "F-API-001")
    assert code == 0 and "ENH-10" in out  # next after ENH-9
    assert node(env, "SysEnhancement", "ENH-10")["status"] == "proposed"

    assert cli(env, capsys, "start-enhancement", "--id", "ENH-10", "--instance", "api")[0] == 0
    e = node(env, "SysEnhancement", "ENH-10")
    assert e["status"] == "in-progress" and e["startedAt"]
    code, _, err = cli(env, capsys, "start-enhancement", "--id", "ENH-10")
    assert code == 1 and "already in-progress" in err
    _, out, _ = cli(env, capsys, "worklog", "--instance", "api")
    assert "IN PROGRESS (1)" in out

    assert cli(env, capsys, "abandon-enhancement", "--id", "ENH-10")[0] == 0
    e = node(env, "SysEnhancement", "ENH-10")
    assert e["status"] == "proposed" and "startedAt" not in e
    assert cli(env, capsys, "abandon-enhancement", "--id", "ENH-10")[0] == 1

    assert cli(env, capsys, "start-enhancement", "--id", "ENH-10")[0] == 0
    code, out, _ = cli(env, capsys, "close-enhancement", "--id", "ENH-10", "--instance", "api")
    assert code == 0 and "ENH-10 done" in out
    assert node(env, "SysEnhancement", "ENH-10")["status"] == "done"
    assert cli(env, capsys, "start-enhancement", "--id", "ENH-10")[0] == 1

    code, out, _ = cli(env, capsys, "show-enhancement", "--id", "ENH-10")
    assert code == 0 and "status: done" in out and "F-API-001" in out
    code, _, err = cli(env, capsys, "show-enhancement", "--id", "DEF-4")
    assert code == 1 and "show-defect" in err


def test_close_blocked_without_tests(env, capsys):
    seeded(env, capsys)
    code, _, err = cli(env, capsys, "close-enhancement", "--id", "ENH-7")
    assert code == 1 and "0 verifying tests" in err
    assert node(env, "SysEnhancement", "ENH-7")["status"] == "proposed"
    assert cli(env, capsys, "close-enhancement", "--id", "ENH-7", "--force")[0] == 0
    assert node(env, "SysEnhancement", "ENH-7")["status"] == "done"


def test_defect_claim_unclaim_close(env, capsys):
    seeded(env, capsys)
    code, out, err = cli(env, capsys, "link-defect", "--title", "Login 500 on empty password",
                         "--feature", "F-API-001", "--severity", "critical", "--instance", "api")
    assert code == 0 and "DEF-5" in out and "AFFECTS" in out
    assert cli(env, capsys, "claim-defect", "--id", "DEF-5", "--instance", "api")[0] == 0
    d = node(env, "SysDefect", "DEF-5")
    assert d["status"] == "in-progress" and d["claimedBy"] == "api" and d["claimedAt"]
    _, out, _ = cli(env, capsys, "worklog", "--instance", "api")
    assert "Claimed : api" in out
    assert cli(env, capsys, "unclaim-defect", "--id", "DEF-5")[0] == 0
    d = node(env, "SysDefect", "DEF-5")
    assert d["status"] == "open" and "claimedBy" not in d
    assert cli(env, capsys, "unclaim-defect", "--id", "DEF-5")[0] == 1
    assert cli(env, capsys, "close-defect", "--id", "DEF-5")[0] == 0
    assert node(env, "SysDefect", "DEF-5")["status"] == "closed"
    code, out, _ = cli(env, capsys, "show-defect", "--id", "DEF-5")
    assert code == 0 and "status: closed" in out
    assert cli(env, capsys, "close-defect", "--id", "DEF-404")[0] == 1
    assert cli(env, capsys, "claim-defect", "--id", "DEF-5")[0] == 1  # closed


def test_notes(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "add-note", "--instance", "api", "--body", "Deploy needs a gateway restart")
    assert code == 0 and "NOTE-3" in out
    code, out, _ = cli(env, capsys, "show-notes", "--instance", "api")
    assert code == 0 and "NOTE-3" in out and "NOTE-2" in out and "gateway restart" in out
    code, out, _ = cli(env, capsys, "show-notes", "--instance", "web")
    assert "No active notes" in out


def test_search(env, capsys):
    seeded(env, capsys)
    code, out, _ = cli(env, capsys, "search", "--query", "LOGOUT")
    assert code == 0 and "F-API-002" in out and "DEF-4" in out
    code, out, _ = cli(env, capsys, "search", "--query", "session token", "--type", "SysFeature")
    assert "F-API-002" in out  # description match
    code, out, _ = cli(env, capsys, "search", "--query", "100%_nothing")
    assert "No results" in out


def test_check_test_links_dry_run(env, capsys):
    seeded(env, capsys)
    (env / "tests" / "api").mkdir(parents=True)
    (env / "tests" / "api" / "test_auth.py").write_text("def test_login(): pass\n")
    code, out, _ = cli(env, capsys, "check-test-links")
    assert code == 0 and "TEST LINK CHECK" in out and "dry run" in out
    assert "src/api/auth_test.go::TestLogout" in out.split("── missing")[1]


# ── concurrency ──────────────────────────────────────────────────────────────

def test_concurrent_writers_get_unique_ids(env, capsys):
    cli(env, capsys, "init")
    path = env / "graph.sqlite3"
    errors = []

    def worker(n):
        try:
            S = be.Store(path)
            for _ in range(15):
                with S.write():
                    nid = S.alloc_id("SysEnhancement", "ENH-")
                    S.update("SysEnhancement", nid, {"title": f"w{n}", "status": "proposed"})
            S.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    con = sqlite3.connect(path)
    ids = [r[0] for r in con.execute("SELECT id FROM nodes WHERE label='SysEnhancement'")]
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert len(ids) == 30 and len(set(ids)) == 30
    assert sorted(int(i.split("-")[1]) for i in ids) == list(range(1, 31))
