"""P-10 compliance building blocks: change log (hash chain), external refs,
baselines + diff, traceability matrix (sha256 + Ed25519), ReqIF round trip.

No database required. Run: python3 -m pytest tests/ -v
"""
import csv
import importlib.util
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_compliance_under_test",
                                               _ROOT / "scripts" / "sys_edge.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


# ── Change log ────────────────────────────────────────────────────────────────

class _R:
    def __init__(self, rows):
        self.rows = rows

    def single(self):
        return self.rows[0] if self.rows else None

    def data(self):
        return self.rows


class ChangeStore:
    """Fake session that persists SysChange nodes in memory."""

    def __init__(self):
        self.changes = []
        self.node = None  # snapshot row returned for _node_snapshot

    def run(self, q, **p):
        q = " ".join(q.split())
        if "ORDER BY c.seq DESC LIMIT 1" in q and "c.hash AS h" in q:
            return _R([{"h": self.changes[-1]["hash"]}] if self.changes else [])
        if "coalesce(max(c.seq), 0) AS s" in q:
            return _R([{"s": max((c["seq"] for c in self.changes), default=0)}])
        if q.startswith("CREATE (c:SysChange) SET c = $entry"):
            self.changes.append(dict(p["entry"]))
            return _R([])
        if "RETURN c {.*} AS c ORDER BY c.seq" in q and "DESC" not in q:
            return _R([{"c": dict(c)} for c in self.changes])
        if "AS props" in q and "AS out" in q:
            return _R([self.node] if self.node else [])
        return _R([])

    def session(self, **_):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    st = ChangeStore()
    monkeypatch.setattr(se, "_driver", lambda *a, **k: st)
    monkeypatch.setattr(se, "_actor", lambda: "dev@example.com")
    return st


def test_snapshot_diff_fields_and_edges():
    before = {"props": {"title": "A", "x": 1}, "edges": ["REQUIRES>F-1"]}
    after = {"props": {"title": "B", "x": 1}, "edges": ["REQUIRES>F-2"]}
    d = se._snapshot_diff(before, after)
    assert d == {"fields": {"title": {"before": "A", "after": "B"}},
                 "edges_added": ["REQUIRES>F-2"], "edges_removed": ["REQUIRES>F-1"]}


def test_no_change_no_entry(store):
    snap = {"label": "SysUserStory", "props": {"title": "A"}, "edges": []}
    assert se._record_change(store, "update-story", "US-1", snap, snap) is None
    assert store.changes == []


def test_change_log_chain_and_verify(store, capsys):
    a = {"label": "SysUserStory", "props": {"title": "A"}, "edges": []}
    b = {"label": "SysUserStory", "props": {"title": "B"}, "edges": []}
    assert se._record_change(store, "create-story", "US-1", None, a) == "CHG-000001"
    assert se._record_change(store, "update-story", "US-1", a, b) == "CHG-000002"
    first, second = store.changes
    assert first["action"] == "create" and second["action"] == "update"
    assert second["prevHash"] == first["hash"] and first["actor"] == "dev@example.com"
    se.cmd_changes(SimpleNamespace(verify=True, id="", limit=50, json=False))
    assert "intact (2 entries)" in capsys.readouterr().out
    store.changes[0]["diff"] = '{"fields": {}}'  # tamper
    with pytest.raises(SystemExit):
        se.cmd_changes(SimpleNamespace(verify=True, id="", limit=50, json=False))
    assert "broken" in capsys.readouterr().out


def test_audited_wrapper_records_command_effect(store):
    store.node = {"label": "SysFeature", "props": {"status": "Proposed"}, "out": [], "inc": []}

    def fake_update(args):
        store.node = {"label": "SysFeature", "props": {"status": "Implemented"}, "out": [], "inc": []}

    se._audited("update-feature", fake_update)(SimpleNamespace(id="F-1"))
    assert len(store.changes) == 1
    diff = json.loads(store.changes[0]["diff"])
    assert diff["fields"]["status"] == {"before": "Proposed", "after": "Implemented"}


def test_every_audited_command_exists():
    src = (_ROOT / "scripts" / "sys_edge.py").read_text(encoding="utf-8")
    for name in se._AUDITED_COMMANDS:
        assert f'"{name}"' in src and f'add_parser("{name}"' in src, name


def test_external_ref_system_validated(monkeypatch):
    with pytest.raises(SystemExit):
        se.cmd_set_external_ref(SimpleNamespace(id="US-1", system="Jira Cloud!", ref="X-1",
                                                remove=False))


# ── Baselines, matrix, ReqIF ──────────────────────────────────────────────────

def _snapshot():
    scoped = {
        "modules": {"MOD-a": {"id": "MOD-a", "name": "Auth"}},
        "features": {"F-1": {"id": "F-1", "name": "Login", "status": "Done", "_mods": ["MOD-a"],
                             "externalRefs": ["jira:AUTH-1"]},
                     "F-2": {"id": "F-2", "name": "Logout", "status": "Done", "_mods": ["MOD-a"]}},
        "usecases": {"UC-1": {"id": "UC-1", "title": "Sign in", "_feats": ["F-1"]}},
        "stories": {"US-1": {"id": "US-1", "title": "Log in & <stay>", "_ucs": ["UC-1"], "_feats": []}},
        "tests": {"T-1": {"id": "T-1", "file": "tests/test_login.py", "function": "test_login",
                          "testType": "integration", "lastStatus": "passed", "_feats": ["F-1"],
                          "_path": "tests/test_login.py"}},
    }
    return json.loads(json.dumps(se._premium_snapshot(scoped)))


def test_baseline_diff():
    a = _snapshot()
    b = json.loads(json.dumps(a))
    b["features"]["F-2"]["status"] = "Deprecated"
    b["features"]["F-3"] = {"name": "New", "links": {}}
    del b["stories"]["US-1"]
    b["tests"]["T-1"]["lastTestedAt"] = "2026-10-10"  # volatile: ignored
    d = se._premium_baseline_diff(a, b)
    assert d["features"] == {"added": ["F-3"], "removed": [], "changed": {"F-2": ["status"]}}
    assert d["stories"]["removed"] == ["US-1"]
    assert d["tests"]["changed"] == {}


def test_matrix_rows_cover_paths_and_gaps():
    rows = se._premium_matrix_rows(_snapshot())
    full = [r for r in rows if r["story"] == "US-1"][0]
    assert (full["use_case"], full["feature"], full["test"], full["test_tier"]) == \
        ("UC-1", "F-1", "T-1", "integration")
    assert full["external_refs"] == "jira:AUTH-1"
    gap = [r for r in rows if r["feature"] == "F-2"][0]
    assert gap["story"] == "" and gap["test"] == ""


def test_trace_matrix_csv_manifest_and_signature(monkeypatch, tmp_path):
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(se, "_require_licence", lambda *a: None)
    base = tmp_path / "data" / "baselines"
    base.mkdir(parents=True)
    (base / "R1.json").write_text(json.dumps({"snapshot": _snapshot()}), encoding="utf-8")
    key = crypto.Ed25519PrivateKey.generate()
    keyfile = tmp_path / "k.hex"
    from cryptography.hazmat.primitives import serialization
    keyfile.write_text(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption()).hex(), encoding="utf-8")
    out = tmp_path / "m.csv"
    se.cmd_trace_matrix(SimpleNamespace(format="csv", baseline="R1", instance="", module="",
                                        output=str(out), sign=True, sign_key=str(keyfile)))
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0].keys() == set(se._MATRIX_COLUMNS) or list(rows[0].keys()) == se._MATRIX_COLUMNS
    import hashlib
    manifest = (tmp_path / "m.csv.sha256").read_text(encoding="utf-8").split()[0]
    assert manifest == hashlib.sha256(out.read_bytes()).hexdigest()
    sig = bytes.fromhex((tmp_path / "m.csv.sig").read_text(encoding="utf-8").strip())
    key.public_key().verify(sig, out.read_bytes())  # raises if invalid


def test_reqif_export_is_well_formed_and_round_trips():
    xml = se._premium_reqif_export(_snapshot(), "Demo & <spec>")
    root = ET.fromstring(xml)
    ns = {"r": se._REQIF_NS}
    objs = root.findall(".//r:SPEC-OBJECT", ns)
    assert {o.get("IDENTIFIER") for o in objs} == {"US-1", "UC-1", "F-1", "F-2"}
    rels = root.findall(".//r:SPEC-RELATION", ns)
    assert len(rels) == 2  # US-1 REALIZED_BY UC-1, UC-1 REQUIRES F-1
    parsed = {r["identifier"]: r for r in se._premium_reqif_parse(xml)}
    assert parsed["US-1"]["title"] == "Log in & <stay>"


def test_premium_compliance_commands_are_gated():
    for cmd in ("baseline", "trace-matrix", "reqif"):
        assert cmd in se.PREMIUM_COMMANDS
    assert "changes" not in se.PREMIUM_COMMANDS
