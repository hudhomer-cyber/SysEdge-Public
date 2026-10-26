"""P-11 lightweight mode: `model: lightweight` in sysedge.yaml makes user
stories and use cases optional in reports; `config` edits the setting.

Run: python3 -m pytest tests/ -v
"""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_light_under_test",
                                               _ROOT / "scripts" / "sys_edge.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


@pytest.fixture
def project(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "_root", tmp_path)
    se._project_config.cache_clear()
    yield tmp_path
    se._project_config.cache_clear()


def test_default_is_full(project):
    assert se._lightweight() is False


def test_config_set_and_get_preserves_other_content(project, capsys):
    (project / "sysedge.yaml").write_text("# my notes\ngo_root: src\n", encoding="utf-8")
    se.cmd_config(SimpleNamespace(action="set", key="model", value="lightweight"))
    text = (project / "sysedge.yaml").read_text(encoding="utf-8")
    assert "# my notes" in text and "go_root: src" in text and "model: lightweight" in text
    assert se._lightweight() is True
    se.cmd_config(SimpleNamespace(action="set", key="model", value="full"))
    assert (project / "sysedge.yaml").read_text(encoding="utf-8").count("model:") == 1
    assert se._lightweight() is False
    capsys.readouterr()
    se.cmd_config(SimpleNamespace(action="get", key="model", value=""))
    assert capsys.readouterr().out.strip() == "model: full"


def test_config_rejects_unknown_values(project):
    with pytest.raises(SystemExit):
        se.cmd_config(SimpleNamespace(action="set", key="model", value="tiny"))


def _scoped():
    feats = {"F-1": {"id": "F-1", "name": "Login", "status": "Done", "_mods": ["MOD-a"]}}
    stories = {"US-1": {"id": "US-1", "title": "Log in", "_ucs": [], "_feats": ["F-1"]}}
    return {"modules": {"MOD-a": {"id": "MOD-a", "name": "A"}}, "features": feats,
            "usecases": {}, "stories": stories, "tests": {}, "unresolved_tests": {}}


def test_detailed_design_gaps_skip_use_case_layer_when_lightweight(project):
    full = se._export_dd_analyse(_scoped())["gaps"]
    assert full["features_without_usecases"] == ["F-1"]
    assert full["stories_without_usecases"] == ["US-1"]
    se._set_config_value("model", "lightweight")
    light = se._export_dd_analyse(_scoped())["gaps"]
    assert light["features_without_usecases"] == [] and light["stories_without_usecases"] == []
    assert light["features_without_tests"] == ["F-1"]  # test gaps still reported


def test_init_creates_lightweight_config_for_new_project(project, monkeypatch):
    class _S:
        def run(self, q, **k):
            return SimpleNamespace(single=lambda: {"c": 0}, data=lambda: [])
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _D:
        def session(self): return _S()
        def close(self): pass
    monkeypatch.setattr(se, "_driver", lambda *a, **k: _D())
    se.cmd_init(SimpleNamespace())
    assert "model: lightweight" in (project / "sysedge.yaml").read_text(encoding="utf-8")


def test_this_repo_stays_full():
    se._project_config.cache_clear()
    assert se._project_config().get("model") == "full"
