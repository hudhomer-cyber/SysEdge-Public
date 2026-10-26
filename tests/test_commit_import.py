"""commit-import: keeps all imported fields, never overwrites an existing id on
create, applies update / merge-into to existing nodes, and allocates free ids.

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
_spec = importlib.util.spec_from_file_location("sys_edge_import_under_test",
                                               _ROOT / "scripts" / "sys_edge.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


class Graph:
    def __init__(self, existing=()):
        self.nodes = {i: {} for i in existing}
        self.log = []

    def run(self, q, **p):
        q = " ".join(q.split())
        self.log.append((q, p))
        if q.endswith("RETURN n.id AS id"):
            return SimpleNamespace(single=lambda: {"id": p["id"]} if p["id"] in self.nodes else None)
        if q.startswith("CREATE (n:"):
            self.nodes[p["id"]] = dict(p["props"])
        elif "SET n += $props" in q:
            self.nodes[p["id"]].update(p["props"])
        return SimpleNamespace(single=lambda: None, data=lambda: [])

    def session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _run(monkeypatch, tmp_path, kind, decisions, existing=()):
    g = Graph(existing)
    monkeypatch.setattr(se, "_driver", lambda *a, **k: g)
    monkeypatch.setattr(se, "_require_licence", lambda *a: None)
    data = tmp_path / "in.json"
    data.write_text(json.dumps({"kind": kind, "instance": "api"}), encoding="utf-8")
    dec = tmp_path / "dec.json"
    dec.write_text(json.dumps(decisions), encoding="utf-8")
    se.cmd_commit_import(SimpleNamespace(file=str(data), decisions=str(dec)))
    return g


def test_story_create_keeps_acceptance_criteria(monkeypatch, tmp_path):
    g = _run(monkeypatch, tmp_path, "stories", [
        {"ref": "1", "action": "create", "entity": {"id_hint": "US-9", "title": "Pay",
                                                     "acceptance_criteria": ["a", "b"]}}])
    assert g.nodes["US-9"]["acceptanceCriteria"] == ["a", "b"]
    assert g.nodes["US-9"]["instance"] == "api"


def test_create_never_overwrites_existing(monkeypatch, tmp_path, capsys):
    g = _run(monkeypatch, tmp_path, "stories", [
        {"ref": "1", "action": "create", "entity": {"id_hint": "US-1", "title": "New"}}],
        existing=["US-1"])
    assert g.nodes["US-1"] == {}
    assert "already exists" in capsys.readouterr().err


def test_generated_ids_skip_taken_ones(monkeypatch, tmp_path):
    g = _run(monkeypatch, tmp_path, "stories", [
        {"ref": "1", "action": "create", "entity": {"title": "A"}},
        {"ref": "2", "action": "create", "entity": {"title": "B"}}],
        existing=["US-IMPORT-001"])
    assert {"US-IMPORT-002", "US-IMPORT-003"} <= set(g.nodes)


def test_use_case_fields_and_update(monkeypatch, tmp_path):
    g = _run(monkeypatch, tmp_path, "use-cases", [
        {"ref": "1", "action": "create", "entity": {
            "id_hint": "UC-5", "title": "Checkout", "preconditions": ["logged in"],
            "main_flow": ["add", "pay"], "failure_scenarios": "card declined"}},
        {"ref": "2", "action": "update", "match_id": "UC-1", "entity": {"title": "Renamed"}}],
        existing=["UC-1"])
    assert g.nodes["UC-5"]["mainFlow"] == "add\npay"
    assert g.nodes["UC-5"]["preconditions"] == "logged in"
    assert g.nodes["UC-5"]["failureScenarios"] == "card declined"
    assert g.nodes["UC-1"] == {"title": "Renamed"}


def test_review_and_skip_are_not_written(monkeypatch, tmp_path):
    g = _run(monkeypatch, tmp_path, "stories", [
        {"ref": "1", "action": "review", "entity": {"id_hint": "US-2", "title": "x"}},
        {"ref": "2", "action": "skip", "entity": {"id_hint": "US-3", "title": "y"}}])
    assert g.nodes == {}
