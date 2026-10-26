"""CLI harness: every subcommand's --help, plus smoke runs of read-mostly
commands against an empty fake graph. Catches NameError / AttributeError /
safe-driver false positives that only show up when a command actually runs.

No database required. Run: python3 -m pytest tests/ -v
"""
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "sys_edge.py"
os.environ.setdefault("SYSEDGE_NEO4J_PASSWORD", "unit-test")
_spec = importlib.util.spec_from_file_location("sys_edge_cli_under_test", _SCRIPT)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

SUBCOMMANDS = sorted(set(re.findall(r'sub\.add_parser\(\s*"([a-z0-9-]+)"',
                                    _SCRIPT.read_text(encoding="utf-8"))))


class _Row(dict):
    """A row whose missing keys read as 0 — aggregate queries always return one row."""
    def __missing__(self, key):
        return 0


class _Result:
    def data(self):
        return []

    def single(self):
        return _Row()

    def value(self):
        return []

    def consume(self):
        return None

    def __iter__(self):
        return iter([])


class _Session:
    def __init__(self, log):
        self.log = log

    def run(self, query, **params):
        self.log.append(query)
        return _Result()

    def begin_transaction(self, **_):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RawDriver:
    def __init__(self):
        self.log = []

    def session(self, **_):
        return _Session(self.log)

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # main() chdirs to _root; restore cwd afterwards
    raw = _RawDriver()
    # Wrap with the real _SafeDriver so its DELETE guard is exercised
    monkeypatch.setattr(se, "_driver",
                        lambda admin=False: raw if admin else se._SafeDriver(raw))
    monkeypatch.setattr(se, "_root", tmp_path)
    monkeypatch.setattr(se, "_require_licence", lambda *a, **k: None)
    return raw


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["sys_edge.py", *argv])
    try:
        se.main()
    except SystemExit as e:
        return e.code or 0
    return 0


def test_subcommand_list_is_complete():
    assert len(SUBCOMMANDS) >= 95
    for name in ("export", "check-test-links", "reset", "seed", "backup", "briefing"):
        assert name in SUBCOMMANDS


@pytest.mark.parametrize("cmd", SUBCOMMANDS)
def test_help(cmd, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(se, "_root", tmp_path)
    assert _run_cli(monkeypatch, [cmd, "--help"]) == 0
    assert "usage:" in capsys.readouterr().out


SMOKE = [
    ["status"],
    ["briefing", "--instance", "core"],
    ["worklog", "--instance", "core"],
    ["test-gaps", "--instance", "core"],
    ["search", "--query", "login"],
    ["unclaim-defect", "--id", "DEF-1"],
    ["abandon-enhancement", "--id", "ENH-1"],
    ["check-test-links"],
    ["show-enhancement", "--id", "ENH-1"],
    ["list-usecases", "--instance", "core"],
    ["test-status"],
    ["analyse", "--orphans"],
]


@pytest.mark.parametrize("argv", SMOKE, ids=lambda a: " ".join(a))
def test_smoke_against_empty_graph(argv, fake_db, monkeypatch, capsys):
    code = _run_cli(monkeypatch, argv)
    err = capsys.readouterr().err
    # Not-found and "nothing to do" exits are fine; crashes are not.
    assert "Traceback" not in err, err[-2000:]
    assert "DELETE operations are not permitted" not in err
    assert code in (0, 1, 4), (code, err[-1000:])
