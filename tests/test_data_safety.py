"""Unit tests for P-03 data safety: scoped wipe, generic relationship backup,
empty-backup guard, first-run seed, dry run and reset.

Runs against a recording fake Neo4j session — no database required.
Run: python3 -m pytest tests/ -v
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_safety_under_test",
                                               _ROOT / "scripts" / "sys_edge.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return list(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


class FakeGraph:
    """Answers queries by substring rules; records every query run."""

    def __init__(self, sys_nodes=0, labels=(), label_counts=None, relationships=(),
                 other_nodes=0):
        self.log = []
        self.sys_nodes = sys_nodes
        self.labels = list(labels)
        self.label_counts = label_counts or {}
        self.relationships = list(relationships)
        self.other_nodes = other_nodes

    def run(self, query, **params):
        q = " ".join(query.split())
        self.log.append((q, params))
        if "AND NOT n:SysSchema RETURN count(n) AS c" in q:
            return _Result([{"c": self.sys_nodes}])
        if q.startswith("CALL db.labels()"):
            return _Result([{"label": l} for l in self.labels])
        if q.startswith("MATCH (n:`") and q.endswith("RETURN count(n) AS c"):
            label = q.split("`")[1]
            return _Result([{"c": self.label_counts.get(label, 0)}])
        if "NONE(l IN labels(n) WHERE l STARTS WITH 'Sys')" in q:
            return _Result([{"c": self.other_nodes}])
        if "RETURN head([l IN labels(a) WHERE l STARTS WITH 'Sys']) AS fromLabel" in q:
            return _Result(self.relationships)
        if "head([l IN labels(n) WHERE l STARTS WITH 'Sys']) AS label, count(*) AS c" in q:
            return _Result([{"label": k, "c": v} for k, v in self.label_counts.items()])
        return _Result([])

    # driver / session / transaction protocol
    def session(self, **_):
        return self

    def begin_transaction(self, **_):
        self.log.append(("BEGIN", {}))
        return self

    def commit(self):
        self.log.append(("COMMIT", {}))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def queries(self, needle):
        return [q for q, _ in self.log if needle in q]


@pytest.fixture
def graph(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    g = FakeGraph()
    monkeypatch.setattr(se, "_driver", lambda *a, **k: g)
    return g


def _seed_args(path, **kw):
    base = dict(file=str(path), instance="", module="", full_restore=False,
                dry_run=False, no_backup=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── Scoped wipe ───────────────────────────────────────────────────────────────

def test_wipe_touches_only_sys_labels(graph):
    graph.labels = ["Person", "Organization", "SysFeature", "SysTest", "SysSchema", "Sys bad"]
    graph.label_counts = {"SysFeature": 3, "SysTest": 5}
    assert se._wipe_sys_nodes(graph) == 8
    deletes = graph.queries("DETACH DELETE")
    assert deletes == [
        "MATCH (n:`SysFeature`) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS",
        "MATCH (n:`SysTest`) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS",
    ]
    assert not any("Person" in q or "SysSchema" in q for q in deletes)


def test_full_restore_never_runs_unscoped_delete(graph, tmp_path):
    graph.sys_nodes = 10
    graph.labels = ["Person", "SysFeature"]
    graph.label_counts = {"SysFeature": 10}
    graph.other_nodes = 42
    seed = _write(tmp_path / "b.json", {"_schema_version": 3, "features": []})
    se.cmd_seed(_seed_args(seed, full_restore=True, no_backup=True))
    assert graph.queries("MATCH (n:`SysFeature`) CALL { WITH n DETACH DELETE n }")
    assert not [q for q, _ in graph.log if "DETACH DELETE" in q and "MATCH (n) " in q]


# ── First run, dry run, refusal ───────────────────────────────────────────────

def test_unfiltered_seed_allowed_on_empty_graph(graph, tmp_path, capsys):
    graph.sys_nodes = 0
    seed = _write(tmp_path / "seed.json", {"systems": [{"id": "SYS-1", "name": "x"}]})
    se.cmd_seed(_seed_args(seed))
    assert graph.queries("MERGE (n:SysSystem {id:$id})")
    assert "first run" in capsys.readouterr().err
    assert not list(tmp_path.glob("data/*.json"))  # no pre-seed backup of an empty graph


def test_unfiltered_seed_refused_on_populated_graph(graph, tmp_path):
    graph.sys_nodes = 5
    seed = _write(tmp_path / "seed.json", {"systems": []})
    with pytest.raises(SystemExit) as exc:
        se.cmd_seed(_seed_args(seed))
    assert exc.value.code == 1
    assert not graph.queries("MERGE")


def test_dry_run_writes_nothing(graph, tmp_path):
    graph.sys_nodes = 5
    seed = _write(tmp_path / "seed.json", {"features": [{"id": "F-1"}]})
    se.cmd_seed(_seed_args(seed, instance="core", dry_run=True))
    assert not (tmp_path / "data").exists()
    assert not graph.queries("MERGE")


# ── Backup ────────────────────────────────────────────────────────────────────

def test_backup_refuses_empty_graph(graph, tmp_path):
    graph.sys_nodes = 0
    out = tmp_path / "data" / "sys-backup-x.json"
    assert se._do_backup(out) is False
    assert not out.exists()
    with pytest.raises(SystemExit) as exc:
        se.cmd_backup(SimpleNamespace(output=str(out), allow_empty=False))
    assert exc.value.code == 1 and not out.exists()


def test_backup_allow_empty_writes(graph, tmp_path):
    out = tmp_path / "empty.json"
    assert se._do_backup(out, allow_empty=True, quiet=True) is True
    assert out.exists()


def test_backup_includes_generic_relationships(graph, tmp_path):
    graph.sys_nodes = 3
    graph.relationships = [
        {"fromLabel": "SysTestPackage", "from": "pkg", "type": "HAS_RUN", "props": {},
         "toLabel": "SysTestRun", "to": "run-1"},
        {"fromLabel": "SysNote", "from": "NOTE-1", "type": "NOTED_ON", "props": {"why": "x"},
         "toLabel": "SysEnhancement", "to": "ENH-1"},
    ]
    out = tmp_path / "b.json"
    se._do_backup(out, quiet=True)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["_schema_version"] == se.SCHEMA_VERSION == 3
    assert data["_relationships"] == graph.relationships


def test_backup_warns_on_large_drop(graph, tmp_path, capsys):
    (tmp_path / "data").mkdir()
    _write(tmp_path / "data" / "sys-backup-old.json",
           {"features": [{"id": f"F-{i}"} for i in range(100)]})
    graph.sys_nodes = 1
    se._do_backup(tmp_path / "data" / "sys-backup-new.json", quiet=True)
    assert "fewer" in capsys.readouterr().err


# ── Relationship restore ──────────────────────────────────────────────────────

def test_restore_relationships_groups_validates_and_filters(graph):
    rels = [
        {"fromLabel": "SysTestPackage", "from": "pkg", "type": "HAS_RUN", "props": {},
         "toLabel": "SysTestRun", "to": "r1"},
        {"fromLabel": "SysTestPackage", "from": "pkg", "type": "HAS_RUN", "props": {},
         "toLabel": "SysTestRun", "to": "r2"},
        {"fromLabel": "SysNote", "from": "N1", "type": "NOTED_ON", "props": {},
         "toLabel": "SysEnhancement", "to": "ENH-9"},
        {"fromLabel": "SysNote", "from": "N2", "type": "BAD) DETACH DELETE (x", "props": {},
         "toLabel": "SysEnhancement", "to": "ENH-9"},
        {"fromLabel": "Person", "from": "P", "type": "KNOWS", "props": {},
         "toLabel": "SysUser", "to": "U"},
    ]
    assert se._restore_relationships(graph, rels) == 3
    merges = [(q, p) for q, p in graph.log if "MERGE (a)-[r:" in q]
    assert len(merges) == 2
    assert all("DELETE" not in q for q, _ in merges)
    graph.log.clear()
    assert se._restore_relationships(graph, rels, keep_ids={"N1"}) == 1


def test_seed_runs_in_one_transaction(graph, tmp_path):
    graph.sys_nodes = 0
    seed = _write(tmp_path / "s.json", {"systems": [{"id": "SYS-1"}]})
    se.cmd_seed(_seed_args(seed))
    order = [q for q, _ in graph.log]
    assert order.index("BEGIN") < order.index("MERGE (n:SysSystem {id:$id}) SET n += $props") \
        < order.index("COMMIT")


def test_seed_restores_generic_relationships(graph, tmp_path):
    graph.sys_nodes = 0
    seed = _write(tmp_path / "b.json", {
        "testRuns": [{"id": "r1"}],
        "_relationships": [{"fromLabel": "SysTestPackage", "from": "pkg", "type": "HAS_RUN",
                            "props": {}, "toLabel": "SysTestRun", "to": "r1"}],
    })
    se.cmd_seed(_seed_args(seed))
    q = graph.queries("MERGE (a)-[r:`HAS_RUN`]->(b)")
    assert q and "MATCH (a:`SysTestPackage` {id: row.f}), (b:`SysTestRun` {id: row.t})" in q[0]


# ── Reset ─────────────────────────────────────────────────────────────────────

def test_reset_refuses_without_terminal(graph, monkeypatch, tmp_path):
    graph.label_counts = {"SysFeature": 2}
    monkeypatch.setattr(se.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as exc:
        se.cmd_reset(SimpleNamespace())
    assert exc.value.code == 1
    assert not graph.queries("DETACH DELETE")
    assert not (tmp_path / "data").exists()


def test_reset_wrong_phrase_aborts(graph, monkeypatch):
    graph.sys_nodes = 2
    graph.label_counts = {"SysFeature": 2}
    graph.labels = ["SysFeature"]
    monkeypatch.setattr(se.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(se, "_read_from_terminal", lambda prompt: "yes")
    se.cmd_reset(SimpleNamespace())
    assert not graph.queries("DETACH DELETE")


def test_reset_confirmed_wipes_sys_labels_only(graph, monkeypatch):
    graph.sys_nodes = 2
    graph.label_counts = {"SysFeature": 2}
    graph.labels = ["Person", "SysFeature"]
    monkeypatch.setattr(se.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(se, "_read_from_terminal", lambda prompt: se._RESET_PHRASE)
    se.cmd_reset(SimpleNamespace())
    assert graph.queries("MATCH (n:`SysFeature`) CALL { WITH n DETACH DELETE n }")
    assert not graph.queries("MATCH (n:`Person`)")
