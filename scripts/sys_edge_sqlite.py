"""SysEdge SQLite backend — zero-Docker storage for the coordination and traceability loop.

Implements the full SysEdge command set using SQLite (Python stdlib). No infrastructure required.

The Neo4j CLI (scripts/sys_edge.py) parses the command line; when the SQLite
backend is selected it hands the parsed argparse Namespace to this module:

    import sys_edge_sqlite
    if args.cmd in sys_edge_sqlite.SUPPORTED:
        sys_edge_sqlite.run(args.cmd, args, _root)

Storage
-------
A generic property graph in one SQLite file:

    nodes(label, id, props JSON)                              PK (label, id)
    edges(from_label, from_id, type, to_label, to_id, props)  PK (all five keys)

Node ``props`` always include ``id``; labels and relationship types are the
same as in Neo4j (SysModule-PROVIDES->SysFeature, SysTest-VERIFIES->SysFeature,
...). The file runs in WAL mode with a busy timeout so parallel agent sessions
can share it, and every write runs in a ``BEGIN IMMEDIATE`` transaction (id
allocation included, so two sessions never get the same ENH-/DEF-/NOTE- id).

Database path: ``$SYSEDGE_SQLITE_PATH``, else ``<project root>/data/sysedge.sqlite3``.

Migration (the backup JSON format is shared, schema v3 with ``_relationships``)
------------------------------------------------------------------------------
Neo4j -> SQLite:
    python3 scripts/sys_edge.py backup --output data/graph.json        # Neo4j backend
    SYSEDGE_BACKEND=sqlite python3 scripts/sys_edge.py seed data/graph.json
SQLite -> Neo4j:
    SYSEDGE_BACKEND=sqlite python3 scripts/sys_edge.py backup --output data/graph.json
    python3 scripts/sys_edge.py seed data/graph.json --full-restore    # or into an empty graph

Exit codes: 0 ok, 1 error / not found / refused, 2 database missing or unreadable.

Python standard library only; importing this module needs no Neo4j driver and
no environment variables.
"""
from __future__ import annotations

import ast
import contextlib
import functools
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.4.0"
SCHEMA_VERSION = 3

SUPPORTED: set[str] = {
    # Phase 1 (original)
    "init", "seed", "backup", "status",
    "briefing", "coverage", "worklog", "test-gaps",
    "create-enhancement", "start-enhancement", "close-enhancement",
    "abandon-enhancement", "show-enhancement",
    "link-defect", "claim-defect", "unclaim-defect", "close-defect", "show-defect",
    "add-note", "show-notes",
    "search", "show-feature", "features", "check-test-links",
    # Phase 2 — requirements authoring
    "create-story", "update-story", "show-story", "stories",
    "create-usecase", "update-usecase", "show-usecase", "list-usecases",
    "create-feature", "update-feature", "retire-feature", "show-feature-tests",
    # Phase 2 — linking
    "link-test", "link-story", "link-usecase", "link-feature",
    "link-blocks", "unlink-blocks", "show-blockers",
    "link-enhancement", "unlink-enhancement", "unlink-usecase",
    # Phase 2 — enhancement workflow
    "wontfix-enhancement", "defer-enhancement", "reopen-enhancement", "approve-enhancement",
    "list-enhancements",
    # Phase 2 — defects
    "list-defects", "update-defect",
    # Phase 2 — ADRs
    "create-adr", "seed-standards",
    # Phase 2 — analysis
    "coverage-report", "trace-matrix", "export", "health-report", "full-inventory",
    # Phase 3 — test-run tracking
    "record-run", "run-history", "prune-runs", "test-status",
    # Phase 3 — utility
    "impact", "link-commit", "add-standard", "scan-tests",
    # Phase 3 — session-driven AI review
    "coverage-review", "quality-review", "audit-status",
    # Phase 5 — audit-test, session-close, config, set-external-ref, baseline
    "audit-test", "session-close", "config", "set-external-ref", "baseline",
    # Phase 6 — defect alias, enhancement update, stale-test pruning, endpoint, reset
    "create-defect", "update-enhancement", "prune-stale-tests",
    "link-endpoint", "reset",
    # Phase 7 — scenario model
    "expire-note", "scenarios", "link-chapter", "link-scenario-story", "link-training",
    "actor-coverage",
    # Phase 7 — graph utilities
    "merge-nodes", "migrate-binary", "analyse", "changes", "reconcile-done",
    # Phase 7 — session-driven AI
    "enh-align", "uc-state-check",
    # Phase 7 — import/export
    "reqif", "preview-import", "commit-import", "export-neo4j",
    # Phase 7 — Go scanning
    "scan-endpoints", "scan-go-tests",
    # Phase 8 — module context + delivery briefing
    "add-module-context", "show-module-context", "deliver-brief",
}

# Commands that may create the database file; every other command needs it to exist.
_CREATES_DB = {"init", "seed"}

VALID_LABELS = {
    "SysSystem", "SysDomain", "SysModule", "SysFeature", "SysUserStory", "SysArchStd",
    "SysTest", "SysEndpoint", "SysSymbol", "SysScenario", "SysScenarioChapter",
    "SysTrainingProgram", "SysTrainingModule", "SysTestPackage", "SysTestRun", "SysUser",
    "SysApplication", "SysDefect", "SysEnhancement", "SysUseCase", "SysDirCategory",
    "SysFeedback", "SysProposal", "SysNote", "SysArchDecision", "SysDocArtifact",
    "SysSchema", "SysChange", "SysBaseline",
}

_SYS_LABEL_RE = re.compile(r"^Sys[A-Za-z]+$")
_REL_TYPE_RE = re.compile(r"^[A-Z][A-Z_]*$")

W = 62
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2}
SEV_MARK = {"critical": "●●●●", "high": "●●●○", "medium": "●●○○", "low": "●○○○"}
PRIO_ORDER = {"Must": 0, "Should": 1}

# ── Backup / seed layout ─────────────────────────────────────────────────────
# key -> (label, fields stripped from node props on seed, derived fields)
# derived field: (field, "one"|"many", "in"|"out", rel type, other label, mode)
#   mode "both": seeded into an edge and re-derived on backup
#   mode "seed": seeded into an edge only (the field is also kept as a node prop)
NODE_SPECS: dict = {
    "systems":      ("SysSystem", (), ()),
    "domains":      ("SysDomain", (), (("systemId", "one", "in", "CONTAINS", "SysSystem", "both"),)),
    "modules":      ("SysModule", (), (("domainId", "one", "in", "CONTAINS", "SysDomain", "both"),)),
    "features":     ("SysFeature", ("moduleId", "parentId"),
                     (("moduleId", "one", "in", "PROVIDES", "SysModule", "both"),
                      ("parentId", "one", "in", "CONTAINS", "SysFeature", "both"))),
    "userStories":  ("SysUserStory", ("requiresFeatures",),
                     (("requiresFeatures", "many", "out", "REQUIRES", "SysFeature", "both"),)),
    "archStandards": ("SysArchStd", ("systemId", "appliesTo"),
                      (("systemId", "one", "in", "GOVERNED_BY", "SysSystem", "both"),
                       ("appliesTo", "many", "in", "CONFORMS_TO", "SysModule", "both"))),
    "testPackages": ("SysTestPackage", (), ()),
    "tests":        ("SysTest", ("verifiesFeatures",),
                     (("verifiesFeatures", "many", "out", "VERIFIES", "SysFeature", "both"),
                      ("packageId", "one", "in", "CONTAINS_TEST", "SysTestPackage", "seed"))),
    "endpoints":    ("SysEndpoint", ("implementsFeature",),
                     (("implementsFeature", "one", "out", "IMPLEMENTS", "SysFeature", "both"),)),
    "symbols":      ("SysSymbol", ("implementsFeature",),
                     (("implementsFeature", "one", "out", "IMPLEMENTS", "SysFeature", "both"),
                      ("moduleId", "one", "in", "CONTAINS_SYMBOL", "SysModule", "seed"))),
    "dirCategories": ("SysDirCategory", (), ()),
    "users":        ("SysUser", ("initiatesStories", "usesApps"),
                     (("initiatesStories", "many", "out", "INITIATES", "SysUserStory", "both"),
                      ("usesApps", "many", "out", "USES", "SysApplication", "both"))),
    "applications": ("SysApplication", ("containsModules", "usedBy"),
                     (("containsModules", "many", "out", "CONTAINS", "SysModule", "both"),
                      ("usedBy", "many", "in", "USES", "SysUser", "seed"))),
    "enhancements": ("SysEnhancement", ("extendsFeatures",),
                     (("extendsFeatures", "many", "out", "EXTENDS", "SysFeature", "both"),)),
    "defects":      ("SysDefect", ("affectsFeatures", "detectedInModules"),
                     (("affectsFeatures", "many", "out", "AFFECTS", "SysFeature", "both"),
                      ("detectedInModules", "many", "out", "DETECTED_IN", "SysModule", "both"))),
    "useCases":     ("SysUseCase", ("storyId", "requiresFeatures"),
                     (("requiresFeatures", "many", "out", "REQUIRES", "SysFeature", "both"),
                      ("storyId", "one", "in", "REALIZED_BY", "SysUserStory", "both"))),
    "archDecisions": ("SysArchDecision", ("constrainsModules", "systemId"),
                      (("constrainsModules", "many", "in", "CONSTRAINED_BY", "SysModule", "both"),
                       ("systemId", "one", "in", "GOVERNED_BY", "SysSystem", "both"))),
    "feedback":     ("SysFeedback", (), ()),
    "proposals":    ("SysProposal", ("extendsFeatures",),
                     (("extendsFeatures", "many", "out", "EXTENDS", "SysFeature", "both"),)),
    "notes":        ("SysNote", (), ()),
    "testRuns":     ("SysTestRun", (), ()),
    "docArtifacts": ("SysDocArtifact", (), ()),
    "changes":      ("SysChange", (), ()),
    "baselines":    ("SysBaseline", (), ()),
}

# key -> (from label, type, to label, from field, to field)
EDGE_SPECS: dict = {
    "usesPattern":     ("SysModule", "USES_PATTERN", "SysModule", "moduleId", "patternId"),
    "prerequisiteFor": ("SysModule", "PREREQUISITE_FOR", "SysModule", "fromId", "toId"),
    "blockedBy":       ("SysEnhancement", "BLOCKED_BY", "SysEnhancement", "fromId", "toId"),
    "demonstrates":    ("SysScenarioChapter", "DEMONSTRATES", "SysFeature", "fromId", "toId"),
    "addresses":       ("SysArchDecision", "ADDRESSES", "SysArchStd", "fromId", "toId"),
    "supersedes":      ("SysArchDecision", "SUPERSEDES", "SysArchDecision", "fromId", "toId"),
}

# Key order of a Neo4j backup (sys_edge.py _do_backup), so files diff cleanly.
BACKUP_ORDER = [
    "systems", "domains", "modules", "features", "userStories", "archStandards",
    "scenarios", "trainingPrograms", "testPackages", "tests", "endpoints", "symbols",
    "usesPattern", "dirCategories", "users", "applications", "enhancements", "defects",
    "useCases", "archDecisions", "prerequisiteFor", "feedback", "proposals", "notes",
    "testRuns", "docArtifacts", "changes", "baselines", "blockedBy", "demonstrates",
    "addresses", "supersedes",
]
_KNOWN_KEYS = set(BACKUP_ORDER) | {"_schema_version", "_relationships"}
_EDGE_LIST_KEYS = set(EDGE_SPECS) | {"_relationships"}


class CommandError(Exception):
    """A user-facing failure: message to stderr, exit with code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def db_path(root: Path) -> Path:
    env = os.environ.get("SYSEDGE_SQLITE_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(root) / "data" / "sysedge.sqlite3"


# ── Store ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    label TEXT NOT NULL,
    id    TEXT NOT NULL,
    props TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (label, id)
);
CREATE TABLE IF NOT EXISTS edges (
    from_label TEXT NOT NULL,
    from_id    TEXT NOT NULL,
    type       TEXT NOT NULL,
    to_label   TEXT NOT NULL,
    to_id      TEXT NOT NULL,
    props      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (from_label, from_id, type, to_label, to_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS edges_to   ON edges (to_label, to_id, type);
CREATE INDEX IF NOT EXISTS edges_type ON edges (type, from_label, to_label);
CREATE INDEX IF NOT EXISTS nodes_id   ON nodes (id);
CREATE INDEX IF NOT EXISTS nodes_instance ON nodes (label, json_extract(props, '$.instance'));
CREATE INDEX IF NOT EXISTS nodes_status   ON nodes (label, json_extract(props, '$.status'));
"""


class Store:
    """Thin repository over the nodes/edges tables."""

    def __init__(self, path: Path, create: bool = False):
        self.path = Path(path)
        if not self.path.exists():
            if not create:
                raise CommandError(
                    f"⚠  SQLite database not found at {self.path} — run "
                    f"`SYSEDGE_BACKEND=sqlite sys_edge.py init` (or set SYSEDGE_SQLITE_PATH).", 2)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise CommandError(f"⚠  Cannot create {self.path.parent}: {e}", 2)
        try:
            self.conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 30000")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.executescript(_DDL)
        except sqlite3.DatabaseError as e:
            raise CommandError(f"⚠  SQLite database {self.path} is unreadable: {e}", 2)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()

    @contextlib.contextmanager
    def write(self):
        """One write transaction; the write lock is taken up front (BEGIN IMMEDIATE)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # queries
    def q(self, sql: str, params=()) -> list:
        return self.conn.execute(sql, params).fetchall()

    def node(self, label: str, nid: str):
        row = self.conn.execute("SELECT props FROM nodes WHERE label=? AND id=?",
                                (label, nid)).fetchone()
        return json.loads(row["props"]) if row else None

    def nodes(self, label: str, **eq) -> list:
        sql = "SELECT props FROM nodes WHERE label=?"
        params: list = [label]
        for k, v in eq.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(k)
            sql += f" AND json_extract(props, '$.{k}') = ?"
            params.append(v)
        sql += " ORDER BY id"
        return [json.loads(r["props"]) for r in self.conn.execute(sql, params)]

    def labels_of(self, nid: str) -> list:
        return [r["label"] for r in self.q("SELECT label FROM nodes WHERE id=? ORDER BY label", (nid,))]

    def count_nodes(self) -> int:
        return self.conn.execute("SELECT count(*) FROM nodes").fetchone()[0]

    def edges(self, rtype: str, from_label: str, to_label: str) -> list:
        """[(from_id, to_id)] for one relationship type between two labels."""
        return [(r[0], r[1]) for r in self.q(
            "SELECT from_id, to_id FROM edges WHERE type=? AND from_label=? AND to_label=? "
            "ORDER BY from_id, to_id", (rtype, from_label, to_label))]

    def out_ids(self, label, nid, rtype, to_label=None) -> list:
        if to_label:
            return [r[0] for r in self.q(
                "SELECT to_id FROM edges WHERE from_label=? AND from_id=? AND type=? AND to_label=? "
                "ORDER BY to_id", (label, nid, rtype, to_label))]
        return [(r[0], r[1]) for r in self.q(
            "SELECT to_label, to_id FROM edges WHERE from_label=? AND from_id=? AND type=? "
            "ORDER BY to_id", (label, nid, rtype))]

    def in_ids(self, label, nid, rtype, from_label=None) -> list:
        if from_label:
            return [r[0] for r in self.q(
                "SELECT from_id FROM edges WHERE to_label=? AND to_id=? AND type=? AND from_label=? "
                "ORDER BY from_id", (label, nid, rtype, from_label))]
        return [(r[0], r[1]) for r in self.q(
            "SELECT from_label, from_id FROM edges WHERE to_label=? AND to_id=? AND type=? "
            "ORDER BY from_id", (label, nid, rtype))]

    # writes (call inside write())
    def upsert(self, label: str, nid: str, props: dict) -> None:
        """MERGE (n:label {id}) SET n += props — None values remove the key."""
        patch = dict(props)
        patch["id"] = nid
        self.conn.execute(
            "INSERT INTO nodes(label, id, props) VALUES (?, ?, json_patch('{}', ?)) "
            "ON CONFLICT(label, id) DO UPDATE SET props = json_patch(nodes.props, excluded.props)",
            (label, nid, json.dumps(patch, default=str)))

    def update(self, label: str, nid: str, patch: dict) -> bool:
        """SET n += patch on an existing node; False when it does not exist."""
        cur = self.conn.execute(
            "UPDATE nodes SET props = json_patch(props, ?) WHERE label=? AND id=?",
            (json.dumps(patch, default=str), label, nid))
        return cur.rowcount > 0

    def link(self, from_label, from_id, rtype, to_label, to_id, props=None) -> bool:
        """MATCH both nodes, MERGE the edge (SET r += props). False when an end is missing."""
        cur = self.conn.execute(
            "INSERT INTO edges(from_label, from_id, type, to_label, to_id, props) "
            "SELECT ?, ?, ?, ?, ?, json_patch('{}', ?) "
            "WHERE EXISTS (SELECT 1 FROM nodes WHERE label=? AND id=?) "
            "  AND EXISTS (SELECT 1 FROM nodes WHERE label=? AND id=?) "
            "ON CONFLICT(from_label, from_id, type, to_label, to_id) "
            "DO UPDATE SET props = json_patch(edges.props, excluded.props)",
            (from_label, from_id, rtype, to_label, to_id, json.dumps(props or {}, default=str),
             from_label, from_id, to_label, to_id))
        return cur.rowcount > 0

    def delete_node(self, label: str, nid: str) -> None:
        self.conn.execute("DELETE FROM edges WHERE (from_label=? AND from_id=?) OR (to_label=? AND to_id=?)",
                          (label, nid, label, nid))
        self.conn.execute("DELETE FROM nodes WHERE label=? AND id=?", (label, nid))

    def alloc_id(self, label: str, prefix: str) -> str:
        """Next sequential id (prefix + max numeric suffix + 1). Call inside write()."""
        mx = 0
        for r in self.q("SELECT id FROM nodes WHERE label=? AND substr(id, 1, ?) = ?",
                        (label, len(prefix), prefix)):
            tail = r[0][len(prefix):]
            if tail.isdigit():
                mx = max(mx, int(tail))
        nid = f"{prefix}{mx + 1}"
        self.upsert(label, nid, {})
        return nid


# ── Shared lookups ───────────────────────────────────────────────────────────

def _project_model(root: Path) -> str:
    path = Path(root) / "sysedge.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "full"
    m = re.search(r"^model\s*:\s*['\"]?([A-Za-z]+)", text, re.MULTILINE)
    return m.group(1).lower() if m else "full"


def _lightweight(root: Path) -> bool:
    return _project_model(root) == "lightweight"


_TIER_OF_TYPE = {"component": "cmp", "go-unit": "cmp", "integration": "int",
                 "usecase": "uc", "e2e": "e2e"}
_TIER_OF_CAT = {"component": "cmp", "integration": "int", "usecase": "uc", "e2e": "e2e"}


def _test_tiers(S: Store) -> dict:
    """test id -> set of tiers ('cmp','int','uc','e2e') from testType or package testCategory."""
    tiers: dict = {}
    for r in S.q("SELECT id, json_extract(props, '$.testType') FROM nodes WHERE label='SysTest'"):
        t = _TIER_OF_TYPE.get(r[1])
        tiers.setdefault(r[0], set())
        if t:
            tiers[r[0]].add(t)
    for r in S.q("""
        SELECT e.to_id, json_extract(p.props, '$.testCategory')
        FROM edges e JOIN nodes p ON p.label = 'SysTestPackage' AND p.id = e.from_id
        WHERE e.type = 'CONTAINS_TEST' AND e.from_label = 'SysTestPackage' AND e.to_label = 'SysTest'
    """):
        t = _TIER_OF_CAT.get(r[1])
        if t:
            tiers.setdefault(r[0], set()).add(t)
    return tiers


def _feature_coverage(S: Store, fids) -> dict:
    """feature id -> {'tests': n, 'cmp': n, 'int': n, 'uc': n, 'e2e': n}."""
    tiers = _test_tiers(S)
    cov = {fid: {"tests": 0, "cmp": 0, "int": 0, "uc": 0, "e2e": 0} for fid in fids}
    for tid, fid in S.edges("VERIFIES", "SysTest", "SysFeature"):
        if fid in cov:
            cov[fid]["tests"] += 1
            for t in tiers.get(tid, ()):
                cov[fid][t] += 1
    return cov


def _module_of_feature(S: Store) -> dict:
    return {fid: mid for mid, fid in S.edges("PROVIDES", "SysModule", "SysFeature")}


def _verified_features(S: Store) -> set:
    return {fid for _, fid in S.edges("VERIFIES", "SysTest", "SysFeature")}


def _resolve(root: Path, p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else Path(root) / path


def _ids(csv: str) -> list:
    return [x.strip() for x in (csv or "").split(",") if x.strip()]


def _title_overlap(a: str, b: str) -> float:
    wa, wb = set((a or "").lower().split()), set((b or "").lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa | wb), 1)


# ── init / seed / backup ─────────────────────────────────────────────────────

def cmd_init(args, root, S):
    with S.write():
        S.conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
        S.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (_now_iso(),))
    print(f"✓ SQLite database ready at {S.path} · WAL · schema v{SCHEMA_VERSION}")
    cfg = Path(root) / "sysedge.yaml"
    if not cfg.exists() and S.count_nodes() == 0:
        cfg.write_text("# SysEdge project settings (see sys_edge.py config --help)\n"
                       "model: lightweight\n", encoding="utf-8")
        print("✓ sysedge.yaml created with model: lightweight "
              "(stories and use cases optional; `config set model full` for the full V-model)")


def _filter_seed_data(data: dict, instance: str = "", module_id: str = "") -> dict:
    """Narrow a backup to one instance or module (same rules as sys_edge.py)."""
    if not instance and not module_id:
        return data
    all_mods = data.get("modules", [])
    scope_mods = [m for m in all_mods if (m.get("id") == module_id if module_id
                                          else m.get("instance") == instance)]
    scope_mod_ids = {m["id"] for m in scope_mods}
    direct = {f["id"] for f in data.get("features", []) if f.get("moduleId") in scope_mod_ids}
    scope_feats = [f for f in data.get("features", [])
                   if f.get("moduleId") in scope_mod_ids or f.get("parentId") in direct]
    fids = {f["id"] for f in scope_feats}

    def touches(n, field):
        return any(fid in fids for fid in n.get(field, []) or [])

    if instance:
        enhs = [e for e in data.get("enhancements", []) if e.get("instance") == instance]
        defs = [d for d in data.get("defects", []) if d.get("instance") == instance]
        props = [p for p in data.get("proposals", []) if p.get("instance") == instance]
        notes = [n for n in data.get("notes", []) if n.get("instance") == instance]
    else:
        enhs = [e for e in data.get("enhancements", []) if touches(e, "extendsFeatures")]
        defs = [d for d in data.get("defects", []) if touches(d, "affectsFeatures")]
        props = [p for p in data.get("proposals", []) if touches(p, "extendsFeatures")]
        notes = []
    tests = [t for t in data.get("tests", []) if touches(t, "verifiesFeatures")]
    pkgs = [p for p in data.get("testPackages", [])
            if any(t.get("packageId") == p["id"] for t in tests)]
    eps = [e for e in data.get("endpoints", []) if e.get("implementsFeature") in fids]
    syms = [s for s in data.get("symbols", [])
            if s.get("moduleId") in scope_mod_ids or s.get("implementsFeature") in fids]
    ucs = [u for u in data.get("useCases", []) if touches(u, "requiresFeatures")]
    stories = [u for u in data.get("userStories", []) if touches(u, "requiresFeatures")]
    out = {
        "modules": scope_mods, "features": scope_feats, "enhancements": enhs,
        "defects": defs, "proposals": props, "notes": notes, "tests": tests,
        "testPackages": pkgs, "endpoints": eps, "symbols": syms, "useCases": ucs,
        "userStories": stories,
        "systems": data.get("systems", []), "domains": data.get("domains", []),
    }
    desc = f"instance={instance}" if instance else f"module={module_id}"
    print(f"  [filtered seed: {desc}]")
    print(f"  modules={len(scope_mods)}  features={len(scope_feats)}  "
          f"enhancements={len(enhs)}  defects={len(defs)}")
    print(f"  tests={len(tests)}  endpoints={len(eps)}  symbols={len(syms)}")
    return out


def _seed_into(S: Store, data: dict, raw_rels: list, keep_ids) -> dict:
    """Write a (possibly filtered) backup dict into the store. Call inside write()."""
    counts: dict = {}
    pending: list = []  # (from_label, from_id, type, to_label, to_id, props)

    for key, (label, strip, derived) in NODE_SPECS.items():
        for node in data.get(key, []) or []:
            if not isinstance(node, dict) or not node.get("id"):
                continue
            nid = str(node["id"])
            props = {k: v for k, v in node.items() if k not in strip}
            existing = S.node(label, nid)
            # A restore from an older backup must never reopen finished work
            if label == "SysDefect" and existing and existing.get("status") == "closed" \
                    and props.get("status") != "closed":
                props["status"] = "closed"
            if label == "SysEnhancement" and existing and existing.get("status") == "done" \
                    and props.get("status") != "done":
                props["status"] = "done"
            S.upsert(label, nid, props)
            counts[key] = counts.get(key, 0) + 1
            for field, card, direction, rtype, other, _mode in derived:
                vals = node.get(field)
                if vals in (None, "", []):
                    continue
                for other_id in (vals if card == "many" else [vals]):
                    if direction == "out":
                        pending.append((label, nid, rtype, other, str(other_id), None))
                    else:
                        pending.append((other, str(other_id), rtype, label, nid, None))

    for sc in data.get("scenarios", []) or []:
        if not sc.get("id"):
            continue
        S.upsert("SysScenario", sc["id"], {k: v for k, v in sc.items() if k != "chapters"})
        counts["scenarios"] = counts.get("scenarios", 0) + 1
        for ch in sc.get("chapters", []) or []:
            cid = f"{sc['id']}-CH-{ch.get('number')}"
            props = {k: v for k, v in ch.items() if k != "exercisesFeatures"}
            props.update(id=cid, scenarioId=sc["id"])
            S.upsert("SysScenarioChapter", cid, props)
            pending.append(("SysScenario", sc["id"], "HAS_CHAPTER", "SysScenarioChapter", cid, None))
            for fid in ch.get("exercisesFeatures", []) or []:
                pending.append(("SysScenarioChapter", cid, "EXERCISES", "SysFeature", fid, None))
            counts["scenarioChapters"] = counts.get("scenarioChapters", 0) + 1

    for tp in data.get("trainingPrograms", []) or []:
        if not tp.get("id"):
            continue
        S.upsert("SysTrainingProgram", tp["id"], {k: v for k, v in tp.items() if k != "modules"})
        counts["trainingPrograms"] = counts.get("trainingPrograms", 0) + 1
        for mod in tp.get("modules", []) or []:
            if not mod.get("id"):
                continue
            props = {k: v for k, v in mod.items() if k != "teachesFeatures"}
            props["programId"] = tp["id"]
            S.upsert("SysTrainingModule", mod["id"], props)
            pending.append(("SysTrainingProgram", tp["id"], "HAS_MODULE", "SysTrainingModule", mod["id"], None))
            for fid in mod.get("teachesFeatures", []) or []:
                pending.append(("SysTrainingModule", mod["id"], "TEACHES", "SysFeature", fid, None))
            counts["trainingModules"] = counts.get("trainingModules", 0) + 1

    for key, (fl, rtype, tl, ff, tf) in EDGE_SPECS.items():
        for e in data.get(key, []) or []:
            if e.get(ff) and e.get(tf):
                pending.append((fl, str(e[ff]), rtype, tl, str(e[tf]), None))
        if data.get(key):
            counts[key] = len(data[key])

    for edge in pending:
        S.link(*edge)

    restored = 0
    for r in raw_rels or []:
        if keep_ids is not None and r.get("from") not in keep_ids and r.get("to") not in keep_ids:
            continue
        fl, rt, tl = r.get("fromLabel") or "", r.get("type") or "", r.get("toLabel") or ""
        if not (_SYS_LABEL_RE.match(fl) and _REL_TYPE_RE.match(rt) and _SYS_LABEL_RE.match(tl)):
            continue
        if r.get("from") is None or r.get("to") is None:
            continue
        S.link(fl, str(r["from"]), rt, tl, str(r["to"]), r.get("props") or {})
        restored += 1
    if raw_rels:
        counts["relationships"] = restored
    return counts


def cmd_seed(args, root, S):
    path = _resolve(root, args.file)
    if not path.exists():
        raise CommandError(f"ERROR: {path} not found")
    inst = getattr(args, "instance", "") or ""
    mod = getattr(args, "module", "") or ""
    full = bool(getattr(args, "full_restore", False))
    if full and (inst or mod):
        raise CommandError("ERROR: --full-restore cannot be combined with --instance or --module")

    first_run = False
    if not inst and not mod and not full:
        first_run = S.count_nodes() == 0
        if not first_run:
            _err("⛔  STOP — unfiltered seed overwrites ALL instances' data.\n\n"
                 "    To restore only YOUR session's nodes (safe):\n"
                 f"      sys_edge.py seed {args.file} --instance <your-instance>\n\n"
                 "    If you own the graph and need a full restore:\n"
                 f"      sys_edge.py seed {args.file} --full-restore\n")
            raise CommandError("", 1)
    if full:
        _err("  [full-restore mode — all instances will be overwritten]")
    elif first_run:
        _err("  [first run — graph is empty, seeding everything]")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CommandError(f"ERROR: cannot read {path}: {e}")
    if not isinstance(data, dict):
        raise CommandError(f"ERROR: {path} is not a SysEdge seed/backup object")
    version = data.get("_schema_version", 0) or 0
    if int(version) > SCHEMA_VERSION:
        raise CommandError(f"⛔  Backup schema version {version} is newer than this CLI "
                           f"(schema v{SCHEMA_VERSION}).\n    Upgrade sys_edge.py before restoring this backup.")
    unknown = sorted(k for k in data if k not in _KNOWN_KEYS)
    raw_rels = data.get("_relationships") or []
    data = _filter_seed_data(data, inst, mod)

    if getattr(args, "dry_run", False):
        print("\n  [DRY RUN — nothing written]\n")
        total = 0
        for k, v in sorted(data.items()):
            if isinstance(v, list) and v:
                print(f"  {k:<25} {len(v):6d}  (would seed)")
                total += len(v)
        if raw_rels:
            print(f"  {'_relationships':<25} {len(raw_rels):6d}  (would restore)")
        print(f"\n  total: {total} items\n\n  Remove --dry-run to apply.\n")
        return

    if not getattr(args, "no_backup", False) and not first_run and S.count_nodes():
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        pre = Path(root) / "data" / f"sys-preseed-{ts}.json"
        print(f"  [pre-seed backup → {pre}]")
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_text(json.dumps(_export(S), indent=2, default=str), encoding="utf-8")

    keep = None
    if inst or mod:
        keep = {n.get("id") for v in data.values() if isinstance(v, list)
                for n in v if isinstance(n, dict) and n.get("id")}
    with S.write():
        if full:
            wiped = S.count_nodes()
            S.conn.execute("DELETE FROM edges")
            S.conn.execute("DELETE FROM nodes")
        counts = _seed_into(S, data, raw_rels, keep)
        if full:
            counts["wiped"] = wiped
    if unknown:
        _err(f"  ⚠  keys not handled by the SQLite backend were ignored: {', '.join(unknown)}")
    for k, v in sorted(counts.items()):
        print(f"  {k:20s} {v}")
    print(f"\n✓ Seed complete from {path}")


def _export(S: Store) -> dict:
    """Backup dict in the Neo4j CLI's format (schema v3, with _relationships)."""
    out: dict = {"_schema_version": SCHEMA_VERSION}
    rel_cache: dict = {}

    def rels(rtype, fl, tl):
        key = (rtype, fl, tl)
        if key not in rel_cache:
            rel_cache[key] = S.edges(rtype, fl, tl)
        return rel_cache[key]

    for key in BACKUP_ORDER:
        if key in NODE_SPECS:
            label, _strip, derived = NODE_SPECS[key]
            nodes = S.nodes(label)
            for field, card, direction, rtype, other, mode in derived:
                if mode != "both":
                    continue
                pairs = rels(rtype, label, other) if direction == "out" else rels(rtype, other, label)
                m: dict = {}
                for a, b in pairs:
                    mine, theirs = (a, b) if direction == "out" else (b, a)
                    m.setdefault(mine, []).append(theirs)
                for n in nodes:
                    vals = m.get(n["id"], [])
                    if card == "many":
                        n[field] = sorted(vals)
                    elif vals:
                        n[field] = sorted(vals)[0]
            out[key] = nodes
        elif key in EDGE_SPECS:
            fl, rtype, tl, ff, tf = EDGE_SPECS[key]
            out[key] = [{ff: a, tf: b} for a, b in rels(rtype, fl, tl)]
        elif key == "scenarios":
            chapters: dict = {}
            for sid, cid in rels("HAS_CHAPTER", "SysScenario", "SysScenarioChapter"):
                chapters.setdefault(sid, []).append(cid)
            ex: dict = {}
            for cid, fid in rels("EXERCISES", "SysScenarioChapter", "SysFeature"):
                ex.setdefault(cid, []).append(fid)
            scs = S.nodes("SysScenario")
            for sc in scs:
                chs = []
                for cid in chapters.get(sc["id"], []):
                    ch = S.node("SysScenarioChapter", cid) or {}
                    node = {k: v for k, v in ch.items() if k not in ("id", "scenarioId")}
                    node["exercisesFeatures"] = ex.get(cid, [])
                    chs.append(node)
                chs.sort(key=lambda c: (str(c.get("number", ""))))
                sc["chapters"] = chs
            out[key] = scs
        elif key == "trainingPrograms":
            mods: dict = {}
            for pid, mid in rels("HAS_MODULE", "SysTrainingProgram", "SysTrainingModule"):
                mods.setdefault(pid, []).append(mid)
            teach: dict = {}
            for mid, fid in rels("TEACHES", "SysTrainingModule", "SysFeature"):
                teach.setdefault(mid, []).append(fid)
            tps = S.nodes("SysTrainingProgram")
            for tp in tps:
                lst = []
                for mid in mods.get(tp["id"], []):
                    m = S.node("SysTrainingModule", mid) or {}
                    node = {k: v for k, v in m.items() if k != "programId"}
                    node["teachesFeatures"] = teach.get(mid, [])
                    lst.append(node)
                tp["modules"] = lst
            out[key] = tps
    out["_relationships"] = [
        {"fromLabel": r[0], "from": r[1], "type": r[2], "props": json.loads(r[5] or "{}"),
         "toLabel": r[3], "to": r[4]}
        for r in S.q("SELECT from_label, from_id, type, to_label, to_id, props FROM edges "
                     "ORDER BY from_label, from_id, type, to_label, to_id")
    ]
    return out


def _backup_node_total(export: dict) -> int:
    return sum(len(v) for k, v in export.items() if isinstance(v, list) and k not in _EDGE_LIST_KEYS)


def cmd_backup(args, root, S):
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = _resolve(root, args.output) if getattr(args, "output", "") else \
        Path(root) / "data" / f"sys-backup-{ts}.json"
    if not getattr(args, "allow_empty", False) and S.count_nodes() == 0:
        raise CommandError("✗ No SysEdge nodes in the graph — nothing backed up (no file written).\n"
                           f"  Check SYSEDGE_SQLITE_PATH ({S.path}), or pass --allow-empty to write an empty backup.")
    export = _export(S)
    nodes_now = _backup_node_total(export)
    try:
        prev = sorted(out_path.parent.glob("sys-backup-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        prev = [p for p in prev if p.resolve() != out_path.resolve()]
        if prev:
            prev_nodes = _backup_node_total(json.loads(prev[0].read_text(encoding="utf-8")))
            if prev_nodes and nodes_now < 0.8 * prev_nodes:
                _err(f"  ⚠  {nodes_now} nodes vs {prev_nodes} in {prev[0].name} "
                     f"({100 - 100 * nodes_now // prev_nodes}% fewer) — check the database path "
                     f"before relying on this backup")
    except (OSError, ValueError):
        pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")
    print(f"✓ Backup written to {out_path}")
    total = 0
    for k, v in sorted(export.items()):
        if isinstance(v, list):
            total += len(v)
            if v:
                print(f"  {k:<25} {len(v)}")
    print(f"  total nodes/edges: {total}")


# ── status / briefing / worklog / test-gaps ─────────────────────────────────

def cmd_status(args, root, S):
    today = date.today().isoformat()
    WS = 70
    instances = [r[0] for r in S.q(
        "SELECT DISTINCT json_extract(props,'$.instance') AS i FROM nodes "
        "WHERE label='SysModule' AND coalesce(i,'') <> '' ORDER BY i")]
    enh_map: dict = {}
    for e in S.nodes("SysEnhancement"):
        if e.get("status") == "done":
            continue
        r = enh_map.setdefault(e.get("instance"), {"must": 0, "should": 0, "could": 0, "wip": 0, "total": 0})
        p = e.get("priority")
        if p in ("Must", "Should", "Could"):
            r[p.lower()] += 1
        r["wip"] += e.get("status") == "in-progress"
        r["total"] += 1
    open_defs = [d for d in S.nodes("SysDefect") if d.get("status") != "closed"]
    def_map: dict = {}
    for d in open_defs:
        r = def_map.setdefault(d.get("instance") or "?", {"urgent": 0, "total": 0})
        r["urgent"] += d.get("severity") in ("critical", "high")
        r["total"] += 1
    feats = {f["id"]: f for f in S.nodes("SysFeature")
             if f.get("status") not in ("Superseded", "Deprecated")}
    mod_inst = {m["id"]: m.get("instance") for m in S.nodes("SysModule")}
    cov = _feature_coverage(S, feats)
    cov_map: dict = {}
    for mid, fid in S.edges("PROVIDES", "SysModule", "SysFeature"):
        if fid not in feats:
            continue
        c = cov[fid]
        r = cov_map.setdefault(mod_inst.get(mid), {"total": 0, "any_cov": 0, "cmp": 0, "int_": 0, "uc": 0})
        r["total"] += 1
        r["any_cov"] += c["tests"] > 0
        r["cmp"] += c["cmp"] > 0
        r["int_"] += c["int"] > 0
        r["uc"] += (c["uc"] + c["e2e"]) > 0
    in_prog = sorted((e for e in S.nodes("SysEnhancement") if e.get("status") == "in-progress"),
                     key=lambda e: (e.get("instance") or "", e["id"]))
    counts = {r[0]: r[1] for r in S.q("SELECT label, count(*) FROM nodes GROUP BY label")}

    print("═" * WS)
    print(f"  SYSTEM STATUS  v{VERSION}  ({today})")
    print("═" * WS)
    print(f"\n  {'Instance':<14} {'Must':>4} {'Shld':>4} {'Cld':>4} {'WIP':>4}  "
          f"{'Def':>4}  {'Cover':>5}  cmp  int   uc")
    print(f"  {'─'*14} {'─'*4} {'─'*4} {'─'*4} {'─'*4}  {'─'*4}  {'─'*5}  {'─'*3}  {'─'*3}  {'─'*3}")
    all_must = all_urgent = 0
    for inst in instances:
        e = enh_map.get(inst, {"must": 0, "should": 0, "could": 0, "wip": 0, "total": 0})
        d = def_map.get(inst, {"urgent": 0, "total": 0})
        c = cov_map.get(inst, {"total": 0, "any_cov": 0, "cmp": 0, "int_": 0, "uc": 0})
        pct = int(100 * c["any_cov"] / c["total"]) if c["total"] else 0
        cov_mark = "✓" if pct == 100 else ("~" if pct > 0 else "✗")
        def_flag = "⚠" if d["urgent"] else " "
        must_s = f"{e['must']:4d}" if e["must"] else "   ."
        def_s = f"{d['total']:4d}" if d["total"] else "   ."
        print(f"  {inst:<14} {must_s} {e['should']:4d} {e['could']:4d} {e['wip']:4d}  {def_s}{def_flag} "
              f" {cov_mark}{pct:3d}%  {c['cmp']:3d}  {c['int_']:3d}  {c['uc']:3d}")
        all_must += e["must"]
        all_urgent += d["urgent"]
    print()
    if in_prog:
        print(f"  IN PROGRESS ({len(in_prog)})")
        for e in in_prog:
            print(f"    {e['id']:<12} [{(e.get('instance') or ''):<12}]  {(e.get('title') or '')[:45]}")
        print()
    if open_defs:
        open_defs.sort(key=lambda d: (SEV_ORDER.get(d.get("severity"), 3), d["id"]))
        print(f"  OPEN DEFECTS ({len(open_defs)})")
        for d in open_defs:
            print(f"    {SEV_MARK.get(d.get('severity'), '●●○○')}  {d['id']:<12} "
                  f"[{(d.get('instance') or '?'):<12}]  {(d.get('title') or '')[:40]}")
        print()
    else:
        print("  OPEN DEFECTS — none ✓\n")
    total_enh = sum(r["total"] for r in enh_map.values())
    total_feat = sum(r["total"] for r in cov_map.values())
    total_cov = sum(r["any_cov"] for r in cov_map.values())
    overall = int(100 * total_cov / total_feat) if total_feat else 0
    print(f"  {'─'*WS}")
    print(f"  {len(instances)} instances  ·  {total_enh} open enhancements ({all_must} Must)  ·  "
          f"{len(open_defs)} defects ({all_urgent} urgent)  ·  {overall}% overall coverage")
    print(f"  graph: {counts.get('SysUserStory', 0)} stories  {counts.get('SysUseCase', 0)} UCs  "
          f"{counts.get('SysFeature', 0)} features  {counts.get('SysTest', 0)} tests  "
          f"[sqlite: {S.path}]")
    if all_must:
        print(f"\n  ⚠  {all_must} Must-priority enhancements — run worklog --instance <name>")
    if all_urgent:
        print(f"  ⚠  {all_urgent} critical/high defects open")
    print("═" * WS)


def _instance_defects(S: Store, mod_ids: list, instance: str) -> list:
    """Open defects scoped to an instance's modules (AFFECTS feature or DETECTED_IN module).

    Defects with no links at all are included when their own instance matches.
    """
    mods = {m["id"]: m for m in S.nodes("SysModule")}
    feats = {f["id"]: f for f in S.nodes("SysFeature")}
    mof = _module_of_feature(S)
    scope = set(mod_ids)
    rows = []
    for d in S.nodes("SysDefect"):
        if d.get("status") == "closed":
            continue
        affected = S.out_ids("SysDefect", d["id"], "AFFECTS", "SysFeature")
        detected = S.out_ids("SysDefect", d["id"], "DETECTED_IN", "SysModule")
        fid = mid = None
        for f in affected:
            if mof.get(f) in scope:
                fid, mid = f, mof[f]
                break
        if not mid:
            mid = next((m for m in detected if m in scope), None)
        if not mid and not (not affected and not detected and instance and d.get("instance") == instance):
            continue
        m = mods.get(mid) or {}
        rows.append({
            "did": d["id"], "title": d.get("title") or "", "sev": d.get("severity") or "medium",
            "status": d.get("status") or "open", "src": d.get("source"), "occ": d.get("occurrences"),
            "seen": d.get("lastSeen"), "claimedBy": d.get("claimedBy"), "claimedAt": d.get("claimedAt"),
            "fid": fid, "fname": (feats.get(fid) or {}).get("name") if fid else None,
            "mid": mid, "mname": m.get("name"),
            "paths": m.get("codePaths") or m.get("paths") or [],
        })
    rows.sort(key=lambda r: (SEV_ORDER.get(r["sev"], 3), r["did"]))
    return rows


def cmd_briefing(args, root, S):
    instance = args.instance
    today = date.today().isoformat()
    sys_domains = {d for _, d in S.edges("CONTAINS", "SysSystem", "SysDomain")}
    domain_of = {m: d for d, m in S.edges("CONTAINS", "SysDomain", "SysModule")}
    domains = {d["id"]: d for d in S.nodes("SysDomain")}
    modules = []
    for m in S.nodes("SysModule", instance=instance):
        did = domain_of.get(m["id"])
        dname = (domains.get(did) or {}).get("name") or "" if did in sys_domains else ""
        modules.append({"id": m["id"], "name": m.get("name") or "", "type": m.get("type"), "domain": dname})
    modules.sort(key=lambda m: (m["domain"], m["name"]))
    if not modules:
        print(f"No modules found for instance '{instance}'.")
        print(f"  Run: python3 scripts/sys_edge.py seed <backup.json> --instance {instance}")
        return
    mod_ids = [m["id"] for m in modules]
    feats = {f["id"]: f for f in S.nodes("SysFeature")}
    by_mod: dict = {mid: [] for mid in mod_ids}
    for mid, fid in S.edges("PROVIDES", "SysModule", "SysFeature"):
        f = feats.get(fid)
        if mid in by_mod and f and f.get("status") not in ("Superseded", "Deprecated"):
            by_mod[mid].append(f)
    all_fids = [f["id"] for v in by_mod.values() for f in v]
    cov = _feature_coverage(S, all_fids)

    def uf(f):
        return f.get("isUserFacing", True) is not False

    defects = _instance_defects(S, mod_ids, instance)
    enhs = []
    mof = _module_of_feature(S)
    for e in S.nodes("SysEnhancement", instance=instance):
        st = e.get("status") or "proposed"
        if st not in ("proposed", "approved", "in-progress"):
            continue
        fids = S.out_ids("SysEnhancement", e["id"], "EXTENDS", "SysFeature")
        enhs.append({"eid": e["id"], "title": e.get("title") or "", "status": st,
                     "priority": e.get("priority") or "Should", "fid": fids[0] if fids else None,
                     "mid": mof.get(fids[0]) if fids else None})
    enhs.sort(key=lambda e: ({"in-progress": 0, "approved": 1, "proposed": 2}.get(e["status"], 3),
                             PRIO_ORDER.get(e["priority"], 2), e["eid"]))

    # Evidence for gaps: scenario chapters / training modules exercising the feature
    ch_sc = {c: s for s, c in S.edges("HAS_CHAPTER", "SysScenario", "SysScenarioChapter")}
    tm_tp = {m: p for p, m in S.edges("HAS_MODULE", "SysTrainingProgram", "SysTrainingModule")}
    ev: dict = {}
    for cid, fid in S.edges("EXERCISES", "SysScenarioChapter", "SysFeature"):
        sc = S.node("SysScenario", ch_sc.get(cid, "")) or {}
        if sc.get("name"):
            ev.setdefault(fid, ({}, {}))[0][sc["name"]] = 1
    for mid, fid in S.edges("TEACHES", "SysTrainingModule", "SysFeature"):
        tp = S.node("SysTrainingProgram", tm_tp.get(mid, "")) or {}
        if tp.get("name"):
            ev.setdefault(fid, ({}, {}))[1][tp["name"]] = 1

    std_sys = {a for _, a in S.edges("GOVERNED_BY", "SysSystem", "SysArchStd")}
    conform: dict = {}
    for mid, aid in S.edges("CONFORMS_TO", "SysModule", "SysArchStd"):
        if mid in mod_ids and aid in std_sys:
            conform.setdefault(aid, []).append(mid)
    arch = []
    for aid, mids in conform.items():
        a = S.node("SysArchStd", aid) or {}
        arch.append({"aid": aid, "aname": a.get("name") or "", "cat": a.get("category"), "conforming": mids})
    arch.sort(key=lambda a: (a["cat"] or "", a["aid"]))
    orphan_tests = S.conn.execute("""
        SELECT count(*) FROM nodes t WHERE t.label='SysTest'
          AND NOT EXISTS (SELECT 1 FROM edges WHERE to_label='SysTest' AND to_id=t.id AND type='CONTAINS_TEST')
          AND NOT EXISTS (SELECT 1 FROM edges WHERE from_label='SysTest' AND from_id=t.id AND type='VERIFIES')
    """).fetchone()[0]

    print("═" * W)
    print(f"  SYSTEM GRAPH BRIEFING — {instance}  ({today})")
    print("═" * W)
    uf_rows = [f for v in by_mod.values() for f in v if uf(f)]
    total_tested = sum(1 for f in uf_rows if cov[f["id"]]["tests"] > 0)
    infra = sum(1 for v in by_mod.values() for f in v if not uf(f))
    infra_note = f"  ({infra} infra excluded)" if infra else ""
    print(f"\nMODULES ({len(modules)})  —  features: {len(uf_rows)}  tested: {total_tested}/{len(uf_rows)}{infra_note}")
    print()
    compact = getattr(args, "compact", False)
    light = _lightweight(root)

    def tier_str(covered, total, short):
        mark = "✓" if covered == total else ("~" if covered > 0 else "✗")
        return f"{mark}{short} {covered}/{total}"

    for mod in modules:
        mid = mod["id"]
        rows = sorted(by_mod.get(mid, []), key=lambda f: f["id"])
        uf_m = [f for f in rows if uf(f)]
        tested = sum(1 for f in uf_m if cov[f["id"]]["tests"] > 0)
        all_pass = bool(uf_m) and tested == len(uf_m)
        pct = f"{tested}/{len(uf_m)}" if uf_m else "0/0"
        tick = " ✓" if all_pass else ""
        mtype = f" [{mod['type']}]" if mod.get("type") else ""
        print(f"  {mid:<22} {mod['name']:<32} {pct}{tick}{mtype}")
        if rows:
            n = len(rows)
            shown = [tier_str(sum(cov[f["id"]]["cmp"] > 0 for f in rows), n, "cmp"),
                     tier_str(sum(cov[f["id"]]["int"] > 0 for f in rows), n, "int")]
            if not light:
                shown += [tier_str(sum(cov[f["id"]]["uc"] > 0 for f in rows), n, "uc "),
                          tier_str(sum(cov[f["id"]]["e2e"] > 0 for f in rows), n, "e2e")]
            print(f"    {'':22} {'  '.join(shown)}")
        if compact and all_pass:
            print()
            continue
        for f in rows:
            t2 = "✓" if cov[f["id"]]["tests"] > 0 else "✗"
            tag = " [infra]" if not uf(f) else ""
            print(f"    {t2} {f['id']:<14} {f.get('name') or ''}{tag}")
        print()

    gaps = [(mid, f) for mid in mod_ids for f in sorted(by_mod[mid], key=lambda f: f["id"])
            if cov[f["id"]]["tests"] == 0 and uf(f)]
    if gaps:
        print(f"COVERAGE GAPS ({len(gaps)})")
        for mid, f in gaps:
            scns, trns = ev.get(f["id"], ({}, {}))
            tag = ""
            if scns:
                tag += f"  ← scenario: {', '.join(scns)}"
            if trns:
                tag += f"  ← training: {', '.join(trns)}"
            urgency = "!" if (scns or trns) else " "
            print(f"  {urgency}✗ {f['id']:<14} {(f.get('name') or ''):<38} {mid}{tag}")
        print()

    if enhs:
        print(f"ENHANCEMENTS ({len(enhs)})")
        for st in ("approved", "in-progress", "proposed"):
            items = [e for e in enhs if e["status"] == st]
            by_feat: dict = {}
            for e in items:
                if e["fid"]:
                    by_feat.setdefault(e["fid"], []).append(e)
            seen: set = set()
            for e in items:
                fid = e["fid"]
                prio = f"[{e['priority']}]"
                st_tag = "  [in-progress]" if st == "in-progress" else ""
                if fid and fid not in seen:
                    seen.add(fid)
                    group = by_feat[fid]
                    if len(group) > 1:
                        ids = ", ".join(g["eid"] for g in group)
                        print(f"    {fid:<14} {prio:<8} [{len(group)} ENHs: {ids[:60]}]{st_tag}")
                    else:
                        print(f"    {e['eid']:<16} {prio:<8} {e['title'][:60]:<60} → {fid}{st_tag}")
                elif not fid:
                    print(f"    {e['eid']:<16} {prio:<8} {e['title'][:60]}{st_tag}")
        print()
    else:
        print("ENHANCEMENTS — none\n")

    if defects:
        print(f"OPEN DEFECTS ({len(defects)})")
        for d in defects:
            src_tag = " [log]" if d.get("src") == "log" else ""
            occ_tag = f" ×{d['occ']}" if d.get("occ") and str(d["occ"]).isdigit() and int(d["occ"]) > 1 else ""
            ref = f"→ {d['fid']}" if d.get("fid") else f"[{d.get('mid') or 'unlinked'}]"
            claim = f" [in-progress: {d['claimedBy']}]" if d["status"] == "in-progress" and d.get("claimedBy") else ""
            print(f"  {d['did']:<18} [{d['sev']:<8}] {d['title'][:40]:<40} {ref}{src_tag}{occ_tag}{claim}")
        print()
    else:
        print("OPEN DEFECTS — none\n")

    if arch:
        print("ARCHITECTURE STANDARDS")
        last = None
        for a in arch:
            cat = a["cat"] or "General"
            if cat != last:
                print(f"  {cat}")
                last = cat
            print(f"    {a['aid']:<18} {a['aname']:<38} ✓ {', '.join(a['conforming'])}")
        print()
    if orphan_tests > 10:
        print(f"\n  ⚠  {orphan_tests} unlinked tests detected — run scan-tests to register them,"
              f" then link-feature to connect them to features")
    print("─" * W)
    print("  sys_edge.py link-feature / link-endpoint / link-symbol to update")
    print("─" * W)


def cmd_worklog(args, root, S):
    instance = args.instance
    today = date.today().isoformat()
    since = getattr(args, "since", "") or ""
    show_all = getattr(args, "all", False)
    mod_ids = [m["id"] for m in S.nodes("SysModule", instance=instance)]
    mods = {m["id"]: m for m in S.nodes("SysModule")}
    feats = {f["id"]: f for f in S.nodes("SysFeature")}
    mof = _module_of_feature(S)
    defects = _instance_defects(S, mod_ids, instance) if mod_ids else \
        _instance_defects(S, [], instance)
    notes = sorted((n for n in S.nodes("SysNote", instance=instance) if n.get("status") == "active"),
                   key=lambda n: n.get("createdAt") or "", reverse=True)
    proposals = sorted((p for p in S.nodes("SysProposal", instance=instance)
                        if p.get("status") in ("draft", "accepted")),
                       key=lambda p: (0 if p.get("status") == "accepted" else 1,
                                      PRIO_ORDER.get(p.get("priority") or "Should", 2), p["id"]))
    feedback_count = len(S.nodes("SysFeedback", instance=instance))
    verified = _verified_features(S)
    enhs = []
    untested = []
    closed_recent = []
    three_days = (date.today() - timedelta(days=3)).isoformat()
    for e in S.nodes("SysEnhancement", instance=instance):
        st = e.get("status")
        if st == "done":
            if e.get("completedAt") and e["completedAt"] >= three_days:
                closed_recent.append(e)
            continue
        if st not in ("proposed", "approved", "in-progress"):
            continue
        fids = S.out_ids("SysEnhancement", e["id"], "EXTENDS", "SysFeature")
        nt = [f for f in fids if f not in verified and (feats.get(f) or {}).get("status") != "Superseded"]
        if nt:
            untested.append({"eid": e["id"], "title": e.get("title") or "", "status": st, "fids": nt})
        if since and (e.get("createdAt") or "") < since:
            continue
        flist = []
        for fid in fids:
            mid = mof.get(fid)
            if mid:
                m = mods.get(mid) or {}
                flist.append({"fid": fid, "fname": (feats.get(fid) or {}).get("name") or "",
                              "mid": mid, "mname": m.get("name") or "",
                              "paths": m.get("codePaths") or m.get("paths") or []})
        blockers = []
        for bid in S.out_ids("SysEnhancement", e["id"], "BLOCKED_BY", "SysEnhancement"):
            b = S.node("SysEnhancement", bid) or {}
            blockers.append({"bid": bid, "bst": b.get("status")})
        enhs.append({"eid": e["id"], "title": e.get("title") or "", "status": st,
                     "priority": e.get("priority") or "Should", "desc": e.get("description"),
                     "deployChecklist": e.get("deployChecklist"), "startedAt": e.get("startedAt"),
                     "features": flist, "blockers": blockers})
    if not show_all and mod_ids:
        enhs = [e for e in enhs if not e["features"] or any(f["mid"] in mod_ids for f in e["features"])]
    enhs.sort(key=lambda e: ({"in-progress": 0, "approved": 1}.get(e["status"], 2),
                             PRIO_ORDER.get(e["priority"], 2), e["eid"]))
    closed_recent.sort(key=lambda e: e.get("completedAt") or "", reverse=True)
    stale_docs = []
    if instance in ("content", "deploy"):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        stale_docs = [d for d in S.nodes("SysDocArtifact", instance=instance)
                      if not d.get("lastReviewedAt") or d["lastReviewedAt"] < cutoff]

    since_tag = f"  (since {since})" if since else ""
    print("═" * W)
    print(f"  WORK LOG — {instance}  ({today}){since_tag}{'  [all]' if show_all else ''}")
    print("  What to work on this session")
    print("═" * W)

    if notes:
        print(f"\nSESSION NOTES ({len(notes)})  — expire with: expire-note --id NOTE-xxx")
        for n in notes:
            age = ""
            try:
                age = f"  [{(date.today() - datetime.fromisoformat(n['createdAt'][:10]).date()).days}d old]"
            except Exception:
                pass
            exp = f"  expires {n['expiresAt'][:10]}" if n.get("expiresAt") else ""
            print(f"\n  📝 {n['id']}{age}{exp}")
            body = (n.get("body") or "").strip()
            for line in body.splitlines()[:4]:
                print(f"     {line}")
            if len(body.splitlines()) > 4:
                print(f"     … ({len(body.splitlines()) - 4} more lines)")

    if proposals:
        print(f"\nOPEN PROPOSALS ({len(proposals)})  — design work awaiting decision or filing")
        for p in proposals:
            mark = "✦" if p.get("status") == "accepted" else "◇"
            print(f"\n  {mark} {p['id']}  [{p.get('status')}]  [{p.get('priority') or 'Should'}]")
            print(f"     {(p.get('title') or '')[:70]}")
            desc = (p.get("description") or "").strip()
            if len(desc) > 10:
                print(f"     {desc[:150]}{'…' if len(desc) > 150 else ''}")

    if defects:
        print(f"\nOPEN DEFECTS ({len(defects)})")
        for d in defects:
            sev = d["sev"]
            occ = f"  ×{d['occ']}" if d.get("occ") and str(d["occ"]).isdigit() and int(d["occ"]) > 1 else ""
            src = " [log]" if d.get("src") == "log" else ""
            st = "  [in-progress]" if d["status"] == "in-progress" else ""
            print(f"\n  {SEV_MARK.get(sev, '●●○○')} {d['did']}  [{sev}]{src}{occ}{st}")
            print(f"     {d['title'][:70]}")
            if d.get("fname") is not None and d.get("fid"):
                print(f"     Feature : {d['fid']}  {d['fname']}")
            else:
                print("     Feature : (not linked — use link-defect to associate)")
            if d.get("mname"):
                print(f"     Module  : {d['mid']}  {d['mname']}")
                for p in (d.get("paths") or [])[:3]:
                    print(f"     Code    : {p}")
            if d["status"] == "in-progress":
                by = d.get("claimedBy") or "?"
                print(f"     Claimed : {by}  at {str(d.get('claimedAt') or '')[:19]}")
            if d.get("seen"):
                print(f"     Last seen: {str(d['seen'])[:19]}")
    else:
        print("\nOPEN DEFECTS — none ✓")

    def group(label, items):
        if not items:
            return
        order = list(dict.fromkeys((e["features"][0]["mid"] if e["features"] else "(unlinked)") for e in items))
        print(f"\nENHANCEMENTS — {label} ({len(items)})")
        for mk in order:
            sub = [e for e in items if (e["features"][0]["mid"] if e["features"] else "(unlinked)") == mk]
            if mk != "(unlinked)" and len(order) > 1:
                print(f"\n  ── {mk} ──")
            for e in sub:
                mark = {"Must": "★★★", "Should": "★★☆", "Could": "★☆☆"}.get(e["priority"], "★★☆")
                print(f"\n  {mark} {e['eid']}  [{e['priority']}]")
                print(f"     {e['title'][:70]}")
                desc = (e.get("desc") or "").strip()
                if len(desc) > 10:
                    print(f"     {desc[:150]}{'…' if len(desc) > 150 else ''}")
                if e["features"]:
                    for f in e["features"][:3]:
                        print(f"     Feature : {f['fid']}  {f['fname']}")
                    shown: set = set()
                    for f in e["features"]:
                        if f["mid"] not in shown:
                            shown.add(f["mid"])
                            print(f"     Module  : {f['mid']}  {f['mname']}")
                            for p in f["paths"][:2]:
                                print(f"     Code    : {p}")
                else:
                    print("     Feature : (not yet linked — use link-enhancement)")
                if e["status"] == "in-progress" and e.get("startedAt"):
                    print(f"     Started : {str(e['startedAt'])[:19]}")
                if e.get("deployChecklist") and e["status"] == "in-progress":
                    print("     Deploy:")
                    for line in e["deployChecklist"].splitlines():
                        print(f"       {line}")
                for b in e["blockers"]:
                    if b["bst"] not in ("done", None):
                        print(f"     ⛔ BLOCKED BY: {b['bid']}  [{b['bst']}]")

    if enhs:
        group("IN PROGRESS", [e for e in enhs if e["status"] == "in-progress"])
        group("APPROVED — ready to implement", [e for e in enhs if e["status"] == "approved"])
        group("PROPOSED", [e for e in enhs if e["status"] == "proposed"])
    else:
        print("\nENHANCEMENTS — none pending")

    if untested:
        print(f"\n{'─' * W}")
        print(f"  ⚠  OPEN ENHANCEMENTS WITH UNTESTED FEATURES ({len(untested)})")
        print("     These ENHs have linked features with 0 verifying tests.")
        print(f"{'─' * W}")
        for u in untested:
            fs = ", ".join(u["fids"][:4]) + (f" … +{len(u['fids']) - 4}" if len(u["fids"]) > 4 else "")
            print(f"  {u['eid']}  [{u['status']}]  {u['title'][:55]}")
            print(f"     No tests: {fs}")
        print("  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")

    print(f"\n{'─' * W}")
    total = len(defects) + len(enhs) + len(proposals) + len(notes)
    print(f"  {total} item(s) · defects {len(defects)} · enhancements {len(enhs)}"
          f" · proposals {len(proposals)} · notes {len(notes)}")
    print(f"  sys_edge.py close-defect --id <id>  |  start-enhancement --id <id> --instance {instance}")
    print(f"{'─' * W}")
    if closed_recent:
        print(f"\nRECENTLY CLOSED — {today}")
        for e in closed_recent[:10]:
            print(f"  ✓ {e['id']}  {(e.get('title') or '')[:60]}")
        print()
    if stale_docs:
        print(f"\n{'─' * W}\n  DOC ARTIFACTS STALE >30 DAYS ({len(stale_docs)})\n{'─' * W}")
        for d in stale_docs:
            print(f"  ⚠  {d['id']:<28}  [{d.get('docType')}]  last: {(d.get('lastReviewedAt') or 'never')[:10]}")
            print(f"     {d.get('path')}")
    fb = f"  ({feedback_count} entr{'y' if feedback_count == 1 else 'ies'} submitted)" if feedback_count else ""
    print(f"\n▶▶ GRAPH FEEDBACK{fb} — record observations any time this session")
    print(f"   python3 scripts/sys_edge.py feedback --instance {instance} --category <cat> --body \"...\"")
    print("   Categories: general | usability | gap | workflow | positive")


def _test_gaps_packages(S: Store):
    today = date.today().isoformat()
    verified = set(S.edges("VERIFIES", "SysTest", "SysFeature"))
    t_feats: dict = {}
    for tid, fid in verified:
        t_feats.setdefault(tid, set()).add(fid)
    contains: dict = {}
    for pid, tid in S.edges("CONTAINS_TEST", "SysTestPackage", "SysTest"):
        contains.setdefault(pid, []).append(tid)
    pkgs = []
    for p in S.nodes("SysTestPackage"):
        tids = contains.get(p["id"], [])
        fs = set().union(*(t_feats.get(t, set()) for t in tids)) if tids else set()
        pkgs.append({"id": p["id"], "area": p.get("area") or "", "cat": p.get("testCategory") or "integration",
                     "totalTests": len(tids), "features": len(fs)})
    print("═" * W)
    print(f"  P6 TEST PACKAGE GAPS — feature-link coverage  ({today})")
    print("═" * W)
    for cat in ("component", "integration", "usecase"):
        items = [p for p in pkgs if p["cat"] == cat]
        if not items:
            continue
        unlinked = [p for p in items if p["features"] == 0 and p["totalTests"] > 0]
        partial = [p for p in items if 0 < p["features"] < 3 and p["totalTests"] > 5]
        print(f"\n{'─' * W}")
        print(f"  {cat.upper()} ({len(items)} packages  — {len(unlinked)} fully unlinked  {len(partial)} partial)")
        print(f"{'─' * W}")
        if unlinked:
            print("  Packages with tests but NO feature links:")
            for p in sorted(unlinked, key=lambda x: -x["totalTests"])[:20]:
                name = p["id"].split("/")[-1].replace(".py", "")
                print(f"    {p['area']:<8} {name:<50} {p['totalTests']:4d} tests")
            if len(unlinked) > 20:
                print(f"    … and {len(unlinked) - 20} more")
        if partial:
            print("  Packages with few feature links (worth extending):")
            for p in sorted(partial, key=lambda x: x["features"])[:10]:
                name = p["id"].split("/")[-1].replace(".py", "")
                print(f"    {p['area']:<8} {name:<50} {p['features']:2d} features / {p['totalTests']:4d} tests")
    print(f"\n{'─' * W}")
    print("  To link a test package to features:")
    print("    python3 scripts/sys_edge.py link-feature --feature F-xxx --tests 'test_file.py::TestClass'")
    print(f"{'─' * W}")


def cmd_test_gaps(args, root, S):
    if getattr(args, "p6", False):
        _test_gaps_packages(S)
        return
    instance = getattr(args, "instance", "") or ""
    tier_arg = getattr(args, "tier", "all") or "all"
    today = date.today().isoformat()
    mods = S.nodes("SysModule", instance=instance)
    mod_by_id = {m["id"]: m for m in mods}
    feats = {f["id"]: f for f in S.nodes("SysFeature")}
    by_mod: dict = {}
    for mid, fid in S.edges("PROVIDES", "SysModule", "SysFeature"):
        if mid in mod_by_id and fid in feats:
            by_mod.setdefault(mid, []).append(feats[fid])
    if not by_mod:
        print(f"No features found for instance '{instance}'.")
        return
    all_rows = [(mid, f) for mid in sorted(by_mod) for f in sorted(by_mod[mid], key=lambda f: f["id"])]
    cov = _feature_coverage(S, [f["id"] for _, f in all_rows])
    pkgs = S.nodes("SysTestPackage")

    def mod_pkgs(m):
        tpaths = m.get("testPaths") or m.get("paths") or []
        area = m["id"].split("MOD-", 1)[1] if "MOD-" in m["id"] else None
        out = []
        for p in pkgs:
            ppath = p.get("path") or ""
            if any(tp.split("/")[0] in ppath for tp in tpaths if ppath) or (area and p.get("area") == area):
                out.append(p["id"])
        return out

    print("═" * W)
    print(f"  TEST GAPS — {instance}  ({today})")
    print("═" * W)
    tiers = [
        ("component", "cmp", "Unit tests (Go *_test.go, vitest)", "scan-go-tests + link-feature"),
        ("integration", "int", "Integration/API tests (Python pytest)", "test_pX_api.py or test_pX_functional.py"),
        ("usecase", "uc", "UI flow tests — one Playwright test per UC", "test_pX_*_ui.py — see uc-ui-testing.md"),
        ("e2e", "e2e", "E2E tests — full User Story journeys (master)", "test_*_e2e.py spanning multiple tools"),
    ]
    if _lightweight(root) and tier_arg == "all":
        tiers = [t for t in tiers if t[0] in ("component", "integration")]
    if tier_arg != "all":
        tiers = [t for t in tiers if t[0] == tier_arg]
    elif instance and instance != "master":
        tiers = [t for t in tiers if t[0] != "e2e"]
    total_f = len(all_rows)
    for name, key, desc, hint in tiers:
        gaps = [(mid, f) for mid, f in all_rows
                if cov[f["id"]][key] == 0 and f.get("status") != "Superseded"]
        print(f"\n{'─' * W}")
        print(f"  {'✓' if not gaps else '✗'} {name.upper()} GAPS  ({len(gaps)} of {total_f} features uncovered)")
        print(f"    {desc}")
        print(f"{'─' * W}")
        if not gaps:
            print(f"  ✓ All features have {name} test coverage")
            continue
        gmods: dict = {}
        for mid, f in gaps:
            gmods.setdefault(mid, []).append(f)
        for mid, rows in sorted(gmods.items()):
            m = mod_by_id[mid]
            print(f"\n  {mid}  {m.get('name') or ''}  ({len(rows)} gaps)")
            for p in (m.get("codePaths") or m.get("paths") or [])[:2]:
                print(f"    Code: {p}")
            names = [p.split("/")[-1].replace(".py", "") for p in mod_pkgs(m) if p][:3]
            if names:
                print(f"    Existing test files: {', '.join(names)}")
            for f in rows:
                c = cov[f["id"]]
                have = [t for t in ("cmp", "int", "uc") if c[t] > 0]
                has = f"  [has: {','.join(have)}]" if have else ""
                print(f"    ✗ {f['id']:<16} {f.get('name') or ''}{has}")
        print(f"\n  → Hint: {hint}")
    print(f"\n{'═' * W}")
    missing = sum(1 for _, f in all_rows
                  if not all(cov[f["id"]][k] > 0 for _, k, _, _ in tiers) and f.get("status") != "Superseded")
    print(f"  {instance}: {missing} feature(s) missing at least one tier of test coverage")
    print(f"{'═' * W}")

    verified = _verified_features(S)
    mof = _module_of_feature(S)
    eps = []
    for eid, fid in S.edges("IMPLEMENTS", "SysEndpoint", "SysFeature"):
        if mof.get(fid) in mod_by_id and fid not in verified:
            ep = S.node("SysEndpoint", eid) or {}
            eps.append((mof[fid], ep.get("method") or "?", ep.get("path") or "?", fid))
    if eps:
        print(f"\n{'─' * W}\n  ⚠ ENDPOINTS WITH NO TEST COVERAGE ({len(eps)})")
        print(f"    Linked to features that have no verifying tests.\n{'─' * W}")
        for _, method, path, fid in sorted(eps):
            print(f"    {method:6} {path:<45} → {fid}")
        print("  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")
        print(f"{'═' * W}")

    days = int(getattr(args, "days", 30) or 30)
    since = (date.today() - timedelta(days=days)).isoformat()
    recent = []
    for e in S.nodes("SysEnhancement", instance=instance):
        if e.get("status") != "done" or (e.get("completedAt") or "") < since:
            continue
        fids = [f for f in S.out_ids("SysEnhancement", e["id"], "EXTENDS", "SysFeature")
                if f not in verified and (feats.get(f) or {}).get("status") != "Superseded"]
        if fids:
            recent.append((str(e.get("completedAt") or "")[:10], e, fids))
    if recent:
        print(f"\n{'─' * W}\n  ⚠ RECENTLY CLOSED ENHs WITH UNTESTED FEATURES (last {days}d)")
        print(f"    These features were implemented but have no verifying tests.\n{'─' * W}")
        for closed, e, fids in sorted(recent, key=lambda r: r[0], reverse=True):
            fs = ", ".join(fids[:4]) + (f" … +{len(fids) - 4}" if len(fids) > 4 else "")
            print(f"    {e['id']}  closed {closed}  {(e.get('title') or '')[:45]}")
            print(f"         Untested: {fs}")
        print("  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")
        print(f"{'═' * W}")


# ── Enhancements ─────────────────────────────────────────────────────────────

def cmd_create_enhancement(args, root, S):
    prio = getattr(args, "priority", "") or "Should"
    with S.write():
        open_enhs = [e for e in S.nodes("SysEnhancement", instance=args.instance) if e.get("status") != "done"]
        eid = S.alloc_id("SysEnhancement", "ENH-")
        for ex in open_enhs:
            ov = _title_overlap(args.title, ex.get("title"))
            if ov >= 0.5:
                _err(f"  ⚠  Possible duplicate: {ex['id']} has {int(ov * 100)}% title overlap: \"{ex.get('title')}\"")
        S.update("SysEnhancement", eid, {
            "title": args.title, "description": getattr(args, "description", "") or "",
            "instance": args.instance, "priority": prio, "status": "proposed",
            "createdAt": _now_iso(), "source": getattr(args, "source", "") or ""})
        for fid in _ids(getattr(args, "feature", "")):
            if not S.link("SysEnhancement", eid, "EXTENDS", "SysFeature", fid):
                _err(f"  ⚠  feature {fid} not found — EXTENDS edge skipped")
    print(f"✓ {eid}  [{args.instance}]  [{prio}]  {args.title}")


def _batch(args, fn, root, S):
    ids = _ids(getattr(args, "ids", "") or "")
    if not ids:
        return fn(args, root, S, getattr(args, "id", ""))
    failed = 0
    for eid in ids:
        try:
            fn(args, root, S, eid, batch=True)
        except CommandError as e:
            if str(e):
                _err(str(e))
            failed += 1
    if failed:
        raise CommandError("", 1)


def cmd_start_enhancement(args, root, S):
    _batch(args, _start_one, root, S)


def _start_one(args, root, S, eid, batch=False):
    with S.write():
        e = S.node("SysEnhancement", eid)
        if not e:
            raise CommandError(f"⚠ {eid} not found")
        if e.get("status") == "in-progress":
            raise CommandError(f"⚠  {eid} is already in-progress (started {str(e.get('startedAt') or '')[:16]}) — "
                               f"run abandon-enhancement first if you want to claim it")
        if e.get("status") == "done":
            raise CommandError(f"⚠  {eid} is already done — cannot restart")
        patch = {"status": "in-progress", "startedAt": _now_iso()}
        if getattr(args, "instance", ""):
            patch["startedBy"] = args.instance
        S.update("SysEnhancement", eid, patch)
    inst = getattr(args, "instance", "")
    if inst and e.get("instance") and e["instance"] != inst:
        _err(f"⚠  Instance mismatch: {eid} belongs to '{e['instance']}'")
    print(f"✓ {eid} in-progress  —  {e.get('title') or ''}")
    if not batch:
        print("""
  ── Test discipline (required before close-enhancement) ──────────────
  1. Implement the change
  2. Integration tests   — run for every changed feature
  3. UI flow tests       — run Playwright for all affected use cases
  4. Regression          — run the broader suite to catch side effects
  5. record-run          — after every test run (graph tracks history)
  ─────────────────────────────────────────────────────────────────────""")


def cmd_abandon_enhancement(args, root, S):
    with S.write():
        e = S.node("SysEnhancement", args.id)
        if not e:
            raise CommandError(f"⚠ {args.id} not found")
        if e.get("status") != "in-progress":
            raise CommandError(f"⚠ {args.id} is '{e.get('status')}', not 'in-progress' — nothing changed")
        S.update("SysEnhancement", args.id, {"status": "proposed", "startedAt": None, "startedBy": None})
    print(f"✓ {args.id} returned to proposed  —  {e.get('title') or ''}")


def _reachable(S: Store, label: str, nid: str, depth: int = 2) -> set:
    """(label, id) of nodes reachable over outgoing edges within depth hops."""
    seen: set = set()
    frontier = [(label, nid)]
    for _ in range(depth):
        nxt = []
        for fl, fi in frontier:
            for r in S.q("SELECT to_label, to_id FROM edges WHERE from_label=? AND from_id=?", (fl, fi)):
                key = (r[0], r[1])
                if key not in seen:
                    seen.add(key)
                    nxt.append(key)
        frontier = nxt
    return seen


def cmd_close_enhancement(args, root, S):
    ids = _ids(getattr(args, "ids", "") or "")
    if ids:
        args.skip_checklist = True
    _batch(args, _close_one, root, S)


def _close_one(args, root, S, eid, batch=False):
    graph_only = getattr(args, "graph_only", False)
    test_only = getattr(args, "test_only", False)
    with S.write():
        e = S.node("SysEnhancement", eid)
        if not e:
            raise CommandError(f"⚠ {eid} not found")
        vtypes = _ids(getattr(args, "verify_entity", "") or "")
        if vtypes:
            reach = _reachable(S, "SysEnhancement", eid)
            for vt in vtypes:
                if vt not in VALID_LABELS:
                    _err(f"⚠  --verify-entity: unknown node type {vt!r} — valid: {', '.join(sorted(VALID_LABELS))}")
                    continue
                if not any(lbl == vt for lbl, _ in reach):
                    raise CommandError(f"⚠  --verify-entity: no {vt} nodes reachable from {eid} — "
                                       f"link them first or omit --verify-entity")
        fids = S.out_ids("SysEnhancement", eid, "EXTENDS", "SysFeature")
        verified = _verified_features(S)
        if fids and not graph_only and not getattr(args, "force", False):
            if not any(f in verified for f in fids):
                raise CommandError(
                    f"⛔  {eid}: {len(fids)} linked feature(s) but 0 verifying tests.\n"
                    f"   Link tests:  sys_edge.py link-feature --feature F-xxx --tests 'test_file.py::TestClass'\n"
                    f"   Or pass --force to close without tests (documents test debt).")
        S.update("SysEnhancement", eid, {"status": "done", "completedAt": _now_iso()})
        inst = getattr(args, "instance", "") or e.get("instance") or ""
        orphan_ucs = 0
        if inst:
            orphan_ucs = sum(1 for uc in S.nodes("SysUseCase", instance=inst)
                             if not S.out_ids("SysUseCase", uc["id"], "REQUIRES", "SysFeature"))
    title = e.get("title") or ""
    caller = getattr(args, "instance", "")
    if caller and e.get("instance") and e["instance"] != caller:
        _err(f"⚠  Instance mismatch: {eid} belongs to '{e['instance']}', you are closing from '{caller}'")
    if orphan_ucs:
        _err(f"⚠  {orphan_ucs} UC(s) in '{inst}' have no REQUIRES→Feature edges — "
             f"run link-usecase or link-endpoint to wire them before ending this session")
    if any(k in title.lower() for k in ("uc quality", "use case quality", "auth boundaries",
                                        "exception paths", "acceptance criteria", "uc design")):
        n = sum(1 for f in fids if f not in verified)
        if n:
            _err(f"⚠  UC quality enhancement closed but {n} linked feature(s) still have 0 tests — "
                 f"run audit-test after writing tests to verify coverage")
    print(f"✓ {eid} done  —  {title}")
    if getattr(args, "skip_checklist", False) or batch:
        return
    skip_code = graph_only or test_only or not fids
    print("\n  Pre-close checklist — confirm before ending your session:")
    if not skip_code:
        print("  □ link-endpoint run for any new API endpoints added this session?")
        print("  □ link-symbol run for any new code symbols added?")
    print("  □ UC REQUIRES→Feature edges wired? (link-usecase)")
    if not graph_only:
        print("  □ Tests linked to features? (link-feature)")
        print("  □ Integration tests run and passing?")
        print("  □ UI flow tests (Playwright) run for all affected use cases?")
        print("  □ Regression check done — no unexpected failures in broader suite?")
        print("  □ record-run completed after final test run?")
    print("  □ User stories still accurate for what was built?")
    note = (" [--graph-only: code checklist suppressed]" if graph_only else
            " [--test-only: endpoint/symbol checklist suppressed]" if test_only else
            " [no linked features: endpoint/symbol checklist suppressed]" if not fids else "")
    print(f"  (suppress with --skip-checklist{note})\n")


def _not_found_hint(S: Store, nid: str, expected: str) -> str:
    labels = [l for l in S.labels_of(nid) if l != expected]
    if not labels:
        return f"⚠ {nid} not found"
    msg = f"⚠ {nid} is not a {expected} (found: {', '.join(labels)})"
    for lbl, cmd in (("SysUseCase", "show-usecase"), ("SysDefect", "show-defect"),
                     ("SysEnhancement", "show-enhancement"), ("SysFeature", "show-feature"),
                     ("SysProposal", "show-proposal")):
        if lbl in labels:
            msg += f"\n  Did you mean: {cmd} --id {nid}"
            break
    return msg


def _features_with_module(S: Store, fids) -> list:
    mof = _module_of_feature(S)
    out = []
    for fid in fids:
        f = S.node("SysFeature", fid) or {}
        out.append({"fid": fid, "fname": f.get("name") or "", "mid": mof.get(fid) or ""})
    return out


def cmd_show_enhancement(args, root, S):
    e = S.node("SysEnhancement", args.id)
    if not e:
        raise CommandError(_not_found_hint(S, args.id, "SysEnhancement"))
    feats = _features_with_module(S, S.out_ids("SysEnhancement", args.id, "EXTENDS", "SysFeature"))
    print(f"\n{'═' * W}")
    print(f"  {e['id']}  [{e.get('instance', '')}]  [{e.get('priority', '?')}]  status: {e.get('status', '?')}")
    print(f"  {e.get('title', '')}")
    print(f"{'─' * W}")
    for line in (e.get("description") or "").splitlines():
        print(f"  {line}")
    if e.get("source"):
        print(f"\n  Source   : {e['source']}")
    if feats:
        print("\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f['fname']}  [{f['mid']}]")
    blockers = S.out_ids("SysEnhancement", args.id, "BLOCKED_BY", "SysEnhancement")
    if blockers:
        print(f"\n  Blocked by: {', '.join(blockers)}")
    if e.get("deployChecklist"):
        print("\n  Deploy checklist:")
        for line in e["deployChecklist"].splitlines():
            print(f"    {line}")
    if e.get("startedBy"):
        print(f"  {'startedBy':<12}: {e['startedBy']}")
    for ts in ("createdAt", "startedAt", "completedAt"):
        if e.get(ts):
            print(f"  {ts:<12}: {str(e[ts])[:19]}")
    print("═" * W)


# ── Defects ──────────────────────────────────────────────────────────────────

def cmd_link_defect(args, root, S):
    feature = getattr(args, "feature", "") or ""
    sev = getattr(args, "severity", "") or "medium"
    did = getattr(args, "id", "") or ""
    with S.write():
        if feature:
            if not S.node("SysFeature", feature):
                _err(f"  ⚠  feature {feature} not found — AFFECTS edge will be skipped")
            for ex_id in S.in_ids("SysFeature", feature, "AFFECTS", "SysDefect"):
                ex = S.node("SysDefect", ex_id) or {}
                if ex.get("status") != "closed" and _title_overlap(args.title, ex.get("title")) >= 0.5:
                    _err(f"  ⚠  Possible duplicate: {ex_id} already open on {feature} "
                         f"with similar title: \"{ex.get('title')}\"")
        if not did:
            did = S.alloc_id("SysDefect", "DEF-")
            _err(f"  auto-assigned id: {did}")
        S.upsert("SysDefect", did, {
            "title": args.title, "severity": sev, "status": "open", "createdAt": _now_iso(),
            "instance": getattr(args, "instance", "") or "",
            "description": getattr(args, "description", "") or ""})
        if feature:
            S.link("SysDefect", did, "AFFECTS", "SysFeature", feature)
    args.id = did
    tag = f"  →  AFFECTS  →  {feature}" if feature else \
        "  (no feature linked — use link-defect --feature to associate)"
    print(f"✓ {did}  [{sev}]  {args.title}{tag}")


def cmd_close_defect(args, root, S):
    with S.write():
        d = S.node("SysDefect", args.id)
        if not d:
            raise CommandError(f"⚠ {args.id} not found — nothing closed")
        S.update("SysDefect", args.id, {"status": "closed", "closedAt": _now_iso()})
    print(f"✓ {args.id} closed")
    print("  □ Confirm: integration tests run and passing after fix?")
    print("  □ Confirm: UI flow / regression tests checked for regressions?")
    print("  □ Confirm: record-run completed after final test run?")


def cmd_claim_defect(args, root, S):
    by = getattr(args, "instance", "") or ""
    with S.write():
        d = S.node("SysDefect", args.id)
        if not d:
            raise CommandError(f"⚠ {args.id} not found")
        if d.get("status") == "closed":
            raise CommandError(f"⚠ {args.id} is closed — nothing to claim")
        if d.get("status") == "in-progress" and d.get("claimedBy") and d["claimedBy"] != by:
            _err(f"  ⚠  {args.id} was already claimed by '{d['claimedBy']}' at "
                 f"{str(d.get('claimedAt') or '')[:19]} — re-claiming")
        S.update("SysDefect", args.id, {"status": "in-progress", "claimedAt": _now_iso(), "claimedBy": by})
    print(f"✓ {args.id} in-progress  —  {d.get('title') or ''}")


def cmd_unclaim_defect(args, root, S):
    with S.write():
        d = S.node("SysDefect", args.id)
        if not d:
            raise CommandError(f"⚠ {args.id} not found")
        if d.get("status") != "in-progress":
            raise CommandError(f"⚠ {args.id} is '{d.get('status')}', not 'in-progress' — nothing changed")
        S.update("SysDefect", args.id, {"status": "open", "claimedAt": None, "claimedBy": None})
    print(f"✓ {args.id} returned to open  —  {d.get('title') or ''}")


def cmd_show_defect(args, root, S):
    d = S.node("SysDefect", args.id)
    if not d:
        raise CommandError(_not_found_hint(S, args.id, "SysDefect"))
    feats = _features_with_module(S, S.out_ids("SysDefect", args.id, "AFFECTS", "SysFeature"))
    sev = d.get("severity", "medium")
    print(f"\n{'═' * W}")
    print(f"  {d['id']}  [{SEV_MARK.get(sev, '●●○○')} {sev}]  status: {d.get('status', '?')}")
    print(f"  {d.get('title', '')}")
    print(f"{'─' * W}")
    if d.get("instance"):
        print(f"  Instance : {d['instance']}")
    if d.get("source"):
        print(f"  Source   : {d['source']}")
    if d.get("status") == "in-progress":
        print(f"  Claimed  : {d.get('claimedBy') or '?'}  at {str(d.get('claimedAt') or '')[:19]}")
    if d.get("description"):
        print(f"\n  {d['description']}")
    if feats:
        print("\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f['fname']}  [{f['mid']}]")
    for ts in ("createdAt", "closedAt", "lastSeen"):
        if d.get(ts):
            print(f"  {ts:<12}: {str(d[ts])[:19]}")
    if d.get("occurrences") and str(d["occurrences"]).isdigit() and int(d["occurrences"]) > 1:
        print(f"  occurrences: {d['occurrences']}")
    print("═" * W)


# ── Notes ────────────────────────────────────────────────────────────────────

def cmd_add_note(args, root, S):
    with S.write():
        nid = S.alloc_id("SysNote", "NOTE-")
        S.update("SysNote", nid, {"instance": args.instance, "body": args.body, "status": "active",
                                  "createdAt": _now_iso(), "expiresAt": getattr(args, "expires", "") or ""})
    print(f"✓ {nid}  [{args.instance}]")
    print(f"  {args.body[:100]}{'…' if len(args.body) > 100 else ''}")


def cmd_show_notes(args, root, S):
    wanted = {"active", "archived"} if getattr(args, "all", False) else {"active"}
    notes = sorted((n for n in S.nodes("SysNote", instance=args.instance) if n.get("status") in wanted),
                   key=lambda n: n.get("createdAt") or "", reverse=True)
    if not notes:
        print(f"No active notes for '{args.instance}'.")
        return
    print(f"\n{'═' * W}")
    print(f"  NOTES — {args.instance}  ({len(notes)} entries)")
    print(f"{'═' * W}")
    for n in notes:
        st = f"  [{n['status']}]" if n.get("status") != "active" else ""
        exp = f"  expires {n['expiresAt'][:10]}" if n.get("expiresAt") else ""
        print(f"\n  {n['id']}{st}{exp}")
        print(f"  Created: {str(n.get('createdAt'))[:19]}")
        for line in (n.get("body") or "").splitlines():
            print(f"  {line}")
    print("═" * W)


# ── Search / features ────────────────────────────────────────────────────────

def cmd_search(args, root, S):
    q = (getattr(args, "query", "") or "").strip().lower()
    if not q:
        raise CommandError("ERROR: --query is required")
    types = _ids(getattr(args, "type", "") or "") or \
        ["SysFeature", "SysEnhancement", "SysDefect", "SysUseCase", "SysUserStory"]
    limit = getattr(args, "limit", 20) or 20
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    hits = []
    for t in types:
        if t not in VALID_LABELS:
            _err(f"  ⚠  unknown type '{t}' — skipping")
            continue
        for r in S.q("""
            SELECT id, coalesce(json_extract(props,'$.title'), json_extract(props,'$.name'), '') AS title,
                   coalesce(json_extract(props,'$.status'), '') AS status,
                   coalesce(json_extract(props,'$.instance'), '') AS inst
            FROM nodes WHERE label = ?
              AND (lower(coalesce(json_extract(props,'$.title'), json_extract(props,'$.name'), '')) LIKE ? ESCAPE '\\'
                   OR lower(coalesce(json_extract(props,'$.description'), '')) LIKE ? ESCAPE '\\')
            ORDER BY id LIMIT ?""", (t, pattern, pattern, int(limit))):
            hits.append((r["id"], t, str(r["title"]), str(r["status"]), str(r["inst"])))
    if not hits:
        print(f"  No results for '{q}'")
        return
    print(f"\n  Search: '{q}'  ({len(hits)} hits)\n")
    for nid, t, title, status, inst in hits:
        st = f"  [{status}]" if status and status != "active" else ""
        it = f"  [{inst}]" if inst else ""
        print(f"  {nid:<18} {t.replace('Sys', ''):<16} {title[:50]}{st}{it}")
    print()


def cmd_features(args, root, S):
    if not S.node("SysModule", args.module):
        raise CommandError(f"⚠ module {args.module} not found")
    fids = S.out_ids("SysModule", args.module, "PROVIDES", "SysFeature")
    cov = _feature_coverage(S, fids)
    for fid in fids:
        f = S.node("SysFeature", fid) or {}
        tick = "✓" if cov[fid]["tests"] > 0 else "✗"
        tag = " [infra]" if f.get("isUserFacing", True) is False else ""
        print(f"  {tick} {fid:<16} {f.get('name') or ''}{tag}")


def cmd_show_feature(args, root, S):
    f = S.node("SysFeature", args.id)
    if not f:
        raise CommandError(_not_found_hint(S, args.id, "SysFeature"))
    mids = S.in_ids("SysFeature", args.id, "PROVIDES", "SysModule")
    m = S.node("SysModule", mids[0]) if mids else None
    WF = 66
    print(f"\n{'═' * WF}")
    print(f"  {f['id']}  [{f.get('status', '?')}]  [{f.get('layer', '')}]")
    print(f"  {f.get('name', '')}")
    print(f"{'─' * WF}")
    if m:
        print(f"  Module  : {m['id']}  {m.get('name') or ''}")
    if f.get("description"):
        print(f"\n  {f['description']}")
    tests = S.in_ids("SysFeature", args.id, "VERIFIES", "SysTest")
    if tests:
        print(f"\n  Tests ({len(tests)}):")
        for tid in tests[:8]:
            t = S.node("SysTest", tid) or {}
            print(f"    {tid}  [{t.get('testType') or ''}]")
        if len(tests) > 8:
            print(f"    … and {len(tests) - 8} more")
    ucs = S.in_ids("SysFeature", args.id, "REQUIRES", "SysUseCase")
    if ucs:
        print(f"\n  Use cases ({len(ucs)}):")
        for uid in ucs:
            print(f"    {uid}  {((S.node('SysUseCase', uid) or {}).get('title') or '')[:50]}")
    defs = [S.node("SysDefect", d) or {} for d in S.in_ids("SysFeature", args.id, "AFFECTS", "SysDefect")]
    defs = [d for d in defs if d.get("status") != "closed"]
    if defs:
        print(f"\n  Open defects ({len(defs)}):")
        for d in defs:
            print(f"    {d['id']}  [{d.get('severity', '?')}]  {(d.get('title') or '')[:50]}")
    enhs = [S.node("SysEnhancement", e) or {} for e in S.in_ids("SysFeature", args.id, "EXTENDS", "SysEnhancement")]
    enhs = [e for e in enhs if e.get("status") != "done"]
    if enhs:
        print(f"\n  Open enhancements ({len(enhs)}):")
        for e in enhs:
            print(f"    {e['id']}  {(e.get('title') or '')[:50]}")
    print("═" * WF)


# ── check-test-links ─────────────────────────────────────────────────────────

def _repo_id(root: Path) -> str:
    rid = os.environ.get("SYSEDGE_REPO", "").strip()
    if not rid:
        try:
            url = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True,
                                 text=True, cwd=root, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            url = ""
        parts = [x for x in re.split(r"[/:]", re.sub(r"\.git$", "", url)) if x]
        if len(parts) >= 2:
            rid = "/".join(parts[-2:])
    return rid or Path(root).name


def _repo_rel_path(root: Path, path) -> str:
    p = str(path or "").strip().replace("\\", "/")
    if not p:
        return ""
    rroot = str(Path(root).resolve()).replace("\\", "/").rstrip("/") + "/"
    if re.match(r"^[A-Za-z]:/", p) or p.startswith("/"):
        try:
            resolved = str(Path(p).resolve()).replace("\\", "/")
        except OSError:
            resolved = p
        for cand in (p, resolved):
            if cand.startswith(rroot):
                return cand[len(rroot):]
        return p
    while p.startswith("./"):
        p = p[2:]
    return p


@functools.lru_cache(maxsize=8)
def _repo_file_index(root: str) -> dict:
    index: dict = {}
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=root, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for rel in out.splitlines():
        index.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    return index


def _resolve_test_file(root: Path, here: str, file, repo: str = "") -> tuple:
    """(status, path): ok | fixed | foreign | missing | nofile — same rules as sys_edge.py."""
    raw = str(file or "").strip()
    if not raw:
        return "nofile", ""
    if repo and repo != here:
        return "foreign", raw
    rel = _repo_rel_path(root, raw)
    if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        return "foreign", rel
    if (Path(root) / rel).exists():
        return ("ok" if rel == raw else "fixed"), rel
    matches = [m for m in _repo_file_index(str(root)).get(rel.rsplit("/", 1)[-1], [])
               if m.endswith("/" + rel)]
    if len(matches) == 1:
        return "fixed", matches[0]
    return "missing", rel


def _rekey(S: Store, label: str, old: str, new: str, patch: dict) -> bool:
    """Move a node to a new id, re-pointing its edges. True when merged into an existing node."""
    existed = S.node(label, new) is not None
    if not existed:
        S.upsert(label, new, S.node(label, old) or {})
    S.update(label, new, dict(patch, id=new))
    S.conn.execute("UPDATE OR IGNORE edges SET from_id=? WHERE from_label=? AND from_id=?", (new, label, old))
    S.conn.execute("UPDATE OR IGNORE edges SET to_id=? WHERE to_label=? AND to_id=?", (new, label, old))
    S.delete_node(label, old)
    return existed


def cmd_check_test_links(args, root, S):
    fix = getattr(args, "fix", False)
    yes = getattr(args, "yes", False)
    verbose = getattr(args, "verbose", False)
    here = _repo_id(root)
    links: dict = {}
    for tid, _ in S.edges("VERIFIES", "SysTest", "SysFeature"):
        links[tid] = links.get(tid, 0) + 1
    tests = [{"id": t["id"], "file": t.get("file"), "repo": t.get("repo"), "links": links.get(t["id"], 0)}
             for t in S.nodes("SysTest")]
    pkgs = [{"id": p["id"], "file": p.get("path") or p["id"], "repo": p.get("repo"), "links": 0}
            for p in S.nodes("SysTestPackage")]

    def classify(rows):
        out: dict = {"ok": [], "fixed": [], "missing": [], "foreign": [], "nofile": []}
        for r in rows:
            tid = str(r["id"])
            stored = r["file"] or (tid.split("::", 1)[0] if "::" in tid else "")
            status, path = _resolve_test_file(root, here, stored, r.get("repo") or "")
            if status == "ok" and not r["file"]:
                status = "fixed"
            out[status].append(dict(r, file=r["file"] or stored, path=path))
        return out

    t_cls, p_cls = classify(tests), classify(pkgs)
    t_cls["nofile"] = [r for r in t_cls["nofile"] if not r["id"].startswith("T-")]
    linked = sum(1 for r in tests if r["links"])
    bad = sum(1 for k in ("fixed", "missing", "foreign", "nofile") for r in t_cls[k] if r["links"])
    print(f"\n  TEST LINK CHECK  (repo: {here})\n")
    print(f"  {'':<10}{'tests':>8}{'with links':>12}{'packages':>10}")
    for k in ("ok", "fixed", "missing", "foreign", "nofile"):
        print(f"  {k:<10}{len(t_cls[k]):>8}{sum(1 for r in t_cls[k] if r['links']):>12}{len(p_cls[k]):>10}")
    pct = (100.0 * bad / linked) if linked else 0.0
    print(f"\n  {bad} of {linked} linked tests ({pct:.1f}%) do not resolve to a file in this repo as stored.")
    for k, title in (("fixed", "path format — will be re-keyed"), ("missing", "file gone — will be deleted"),
                     ("foreign", "other repo — left untouched"), ("nofile", "no path — report only")):
        rows = t_cls[k] + p_cls[k]
        if not rows:
            continue
        print(f"\n  ── {k}: {title} ({len(rows)})")
        for r in rows[: (None if verbose else 8)]:
            arrow = f"  →  {r['path']}" if k == "fixed" else ""
            print(f"     {r['id'][:90]}{arrow}")
        if not verbose and len(rows) > 8:
            print(f"     … {len(rows) - 8} more (use --verbose)")
    to_fix = len(t_cls["fixed"]) + len(p_cls["fixed"])
    to_del = len(t_cls["missing"]) + len(p_cls["missing"])
    if not fix:
        print(f"\n  (dry run — --fix re-keys {to_fix} and deletes {to_del} nodes)\n")
        return
    if not (to_fix or to_del):
        print("\n  ✓ Nothing to repair\n")
        return
    if not yes:
        try:
            resp = input(f"\n  Re-key {to_fix} and delete {to_del} nodes? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() != "y":
            print("  Aborted.")
            return
    rekeyed = merged = 0
    with S.write():
        for label, rows, field in (("SysTestPackage", p_cls["fixed"], "path"), ("SysTest", t_cls["fixed"], "file")):
            for r in rows:
                new_id = r["id"].replace(r["file"], r["path"], 1) if r["file"] and r["file"] in r["id"] \
                    else _repo_rel_path(root, r["id"])
                if new_id == r["id"]:
                    S.update(label, r["id"], {field: r["path"], "repo": here})
                else:
                    merged += _rekey(S, label, r["id"], new_id, {field: r["path"], "repo": here})
                rekeyed += 1
        for r in t_cls["missing"]:
            S.delete_node("SysTest", r["id"])
        for r in p_cls["missing"]:
            S.delete_node("SysTestPackage", r["id"])
        for label, rows in (("SysTest", t_cls["ok"]), ("SysTestPackage", p_cls["ok"])):
            for r in rows:
                if not r.get("repo"):
                    S.update(label, r["id"], {"repo": here})
    print(f"\n  ✓ Re-keyed {rekeyed} nodes ({merged} merged into existing), deleted {to_del}. "
          f"Run a backup to keep the result.\n")


# ── Phase-2 commands: requirements authoring, linking, ADRs, analysis ────────


# ── Stories ──────────────────────────────────────────────────────────────────

def cmd_create_story(args, root, S):
    """Create or update a SysUserStory."""
    props = {
        "title": args.title,
        "actor": args.actor or "",
        "goal": args.goal or "",
        "priority": args.priority or "Should",
        "benefit": args.benefit or "",
        "acceptanceCriteria": getattr(args, "acceptance_criteria", "") or "",
    }
    with S.write():
        S.upsert("SysUserStory", args.id, props)
        for fid in [x.strip() for x in (args.features or "").split(",") if x.strip()]:
            S.link("SysUserStory", args.id, "REQUIRES", "SysFeature", fid)
    print(f"✓ {args.id}  {args.title}")


def cmd_update_story(args, root, S):
    """Update fields on an existing SysUserStory."""
    patch = {}
    if args.title:                              patch["title"]               = args.title
    if args.actor:                              patch["actor"]               = args.actor
    if args.goal:                               patch["goal"]                = args.goal
    if args.benefit:                            patch["benefit"]             = args.benefit
    if getattr(args, "narrative", ""):          patch["narrative"]           = args.narrative
    if getattr(args, "acceptance_criteria", ""): patch["acceptanceCriteria"] = args.acceptance_criteria
    if getattr(args, "out_of_scope", ""):       patch["outOfScope"]         = args.out_of_scope
    if not patch:
        print("Nothing to update — no fields provided."); return
    if not S.update("SysUserStory", args.id, patch):
        raise CommandError(f"Story '{args.id}' not found.")
    print(f"✓ {args.id}  updated {list(patch)}")


def cmd_show_story(args, root, S):
    """Show full details of a user story."""
    us = S.node("SysUserStory", args.id)
    if not us:
        raise CommandError(f"Story '{args.id}' not found.")
    print(f"\n{'─'*W}")
    print(f"  {us['id']}  {us.get('title','')}")
    print(f"{'─'*W}")
    print(f"  Actor:    {us.get('actor','—')}")
    print(f"  Goal:     {us.get('goal','—')}")
    print(f"  Priority: {us.get('priority','—')}")
    if us.get("benefit"):
        print(f"  Benefit:  {us['benefit']}")
    if us.get("acceptanceCriteria"):
        print(f"  AC:       {us['acceptanceCriteria']}")
    uc_ids = S.out_ids("SysUserStory", args.id, "REALIZED_BY", "SysUseCase")
    if uc_ids:
        print(f"\n  Use Cases ({len(uc_ids)}):")
        for uid in sorted(uc_ids):
            uc = S.node("SysUseCase", uid)
            print(f"    {uid}  {uc.get('title','') if uc else ''}")
    feat_ids = S.out_ids("SysUserStory", args.id, "REQUIRES", "SysFeature")
    if feat_ids:
        print(f"\n  Features ({len(feat_ids)}):")
        for fid in sorted(feat_ids):
            f = S.node("SysFeature", fid)
            print(f"    {fid}  {f.get('name','') if f else ''}")
    print()


def cmd_stories(args, root, S):
    """List user stories."""
    inst = getattr(args, "instance", "") or ""
    all_stories = S.nodes("SysUserStory")
    if inst:
        mod_ids = {n["id"] for n in S.nodes("SysModule", instance=inst)}
        feat_ids: set = set()
        for mid in mod_ids:
            feat_ids.update(S.out_ids("SysModule", mid, "PROVIDES", "SysFeature"))
        story_ids: set = set()
        for fid in feat_ids:
            story_ids.update(S.in_ids("SysFeature", fid, "REQUIRES", "SysUserStory"))
        all_stories = [s for s in all_stories if s["id"] in story_ids]
    all_stories.sort(key=lambda s: s["id"])
    print(f"\n  {'ID':<18} {'Pri':<8} {'Title'}")
    print(f"  {'─'*18} {'─'*8} {'─'*35}")
    for s in all_stories:
        print(f"  {s['id']:<18} {s.get('priority','?'):<8} {s.get('title','')[:50]}")
    print(f"\n  {len(all_stories)} stories\n")


# ── Use Cases ─────────────────────────────────────────────────────────────────

def cmd_create_usecase(args, root, S):
    """Create or update a SysUseCase."""
    if not args.instance:
        raise CommandError("ERROR: --instance is required for create-usecase")
    existing = S.node("SysUseCase", args.id)
    if not existing and not args.title:
        raise CommandError("ERROR: --title is required when creating a new use case")

    if not existing:
        props = {
            "title":            args.title,
            "name":             args.name or args.title.lower().replace(" ", "-"),
            "instance":         args.instance,
            "description":      args.description or "",
            "priority":         args.priority or "P1",
            "preconditions":    getattr(args, "preconditions", "") or "",
            "mainFlow":         getattr(args, "main_flow", "") or "",
            "postconditions":   getattr(args, "postconditions", "") or "",
            "authorizedRoles":  getattr(args, "authorized_roles", "") or "",
            "failureScenarios": getattr(args, "failure_scenarios", "") or "",
        }
    else:
        props = {}
        if args.title:                              props["title"]            = args.title
        if args.name:                               props["name"]             = args.name
        if args.description:                        props["description"]      = args.description
        if args.priority:                           props["priority"]         = args.priority
        if args.instance:                           props["instance"]         = args.instance
        if getattr(args, "preconditions", ""):      props["preconditions"]    = args.preconditions
        if getattr(args, "main_flow", ""):          props["mainFlow"]         = args.main_flow
        if getattr(args, "postconditions", ""):     props["postconditions"]   = args.postconditions
        if getattr(args, "authorized_roles", ""):   props["authorizedRoles"]  = args.authorized_roles
        if getattr(args, "failure_scenarios", ""):  props["failureScenarios"] = args.failure_scenarios

    with S.write():
        S.upsert("SysUseCase", args.id, props)
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            if not S.node("SysUserStory", sid):
                print(f"⚠  story '{sid}' not found — skipping link", file=sys.stderr)
                continue
            S.link("SysUserStory", sid, "REALIZED_BY", "SysUseCase", args.id)
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            S.link("SysUseCase", args.id, "REQUIRES", "SysFeature", fid)
    title = args.title or (existing.get("title", "") if existing else "")
    print(f"✓ {args.id}  {title}")


def cmd_update_usecase(args, root, S):
    """Update fields on an existing SysUseCase."""
    patch = {}
    if args.title:                              patch["title"]            = args.title
    if args.description:                        patch["description"]      = args.description
    if args.priority:                           patch["priority"]         = args.priority
    if getattr(args, "preconditions", ""):      patch["preconditions"]    = args.preconditions
    if getattr(args, "main_flow", ""):          patch["mainFlow"]         = args.main_flow
    if getattr(args, "postconditions", ""):     patch["postconditions"]   = args.postconditions
    if getattr(args, "authorized_roles", ""):   patch["authorizedRoles"]  = args.authorized_roles
    if getattr(args, "failure_scenarios", ""):  patch["failureScenarios"] = args.failure_scenarios
    if not patch:
        print("Nothing to update — no fields provided."); return
    if not S.update("SysUseCase", args.id, patch):
        raise CommandError(f"Use case '{args.id}' not found.")
    print(f"✓ {args.id}  updated {list(patch)}")


def cmd_show_usecase(args, root, S):
    """Show full details of a use case."""
    uc = S.node("SysUseCase", args.id)
    if not uc:
        raise CommandError(f"Use case '{args.id}' not found.")
    print(f"\n{'─'*W}")
    print(f"  {uc['id']}  {uc.get('title','')}  [{uc.get('priority','?')}]")
    print(f"{'─'*W}")
    print(f"  Instance:  {uc.get('instance','—')}")
    if uc.get("description"):
        print(f"  Desc:      {uc['description'][:120]}")
    if uc.get("preconditions"):
        print(f"  Pre:       {uc['preconditions'][:120]}")
    if uc.get("mainFlow"):
        print(f"  Flow:      {uc['mainFlow'][:200]}")
    if uc.get("postconditions"):
        print(f"  Post:      {uc['postconditions'][:120]}")
    if uc.get("authorizedRoles"):
        print(f"  Roles:     {uc['authorizedRoles']}")
    if uc.get("failureScenarios"):
        print(f"  Failures:  {uc['failureScenarios'][:120]}")
    story_ids = S.in_ids("SysUseCase", args.id, "REALIZED_BY", "SysUserStory")
    if story_ids:
        print(f"\n  Stories ({len(story_ids)}):")
        for sid in sorted(story_ids):
            s = S.node("SysUserStory", sid)
            print(f"    {sid}  {s.get('title','') if s else ''}")
    feat_ids = S.out_ids("SysUseCase", args.id, "REQUIRES", "SysFeature")
    if feat_ids:
        print(f"\n  Features ({len(feat_ids)}):")
        for fid in sorted(feat_ids):
            f = S.node("SysFeature", fid)
            print(f"    {fid}  {f.get('name','') if f else ''}")
    print()


def cmd_list_usecases(args, root, S):
    """List use cases filtered by instance, module, or story."""
    inst   = getattr(args, "instance", "") or ""
    mod_id = getattr(args, "module",   "") or ""
    story  = getattr(args, "story",    "") or ""

    all_ucs = S.nodes("SysUseCase")
    if inst:
        all_ucs = [u for u in all_ucs if u.get("instance") == inst]
    if mod_id:
        feat_ids = set(S.out_ids("SysModule", mod_id, "PROVIDES", "SysFeature"))
        filtered = []
        for u in all_ucs:
            uc_feats = set(S.out_ids("SysUseCase", u["id"], "REQUIRES", "SysFeature"))
            if uc_feats & feat_ids:
                filtered.append(u)
        all_ucs = filtered
    if story:
        linked = set(S.out_ids("SysUserStory", story, "REALIZED_BY", "SysUseCase"))
        all_ucs = [u for u in all_ucs if u["id"] in linked]

    all_ucs.sort(key=lambda u: u["id"])
    print(f"\n  {'ID':<20} {'Pri':<5} {'Inst':<14} {'Title'}")
    print(f"  {'─'*20} {'─'*5} {'─'*14} {'─'*35}")
    for u in all_ucs:
        print(f"  {u['id']:<20} {u.get('priority','?'):<5} {u.get('instance','?'):<14} {u.get('title','')[:50]}")
    print(f"\n  {len(all_ucs)} use cases\n")


# ── Features ──────────────────────────────────────────────────────────────────

def cmd_create_feature(args, root, S):
    """Create a SysFeature linked to a module."""
    props = {
        "name":        args.name,
        "status":      args.status or "Proposed",
        "description": args.description or "",
        "narrative":   args.narrative or "",
        "source":      args.source or "manual",
    }
    with S.write():
        S.upsert("SysFeature", args.id, props)
        S.link("SysModule", args.module, "PROVIDES", "SysFeature", args.id)
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            S.link("SysUserStory", sid, "REQUIRES", "SysFeature", args.id)
    print(f"✓ {args.id}  {args.name}")


def cmd_update_feature(args, root, S):
    """Update fields on a SysFeature."""
    patch = {}
    if args.name:        patch["name"]        = args.name
    if args.description: patch["description"] = args.description
    if args.narrative:   patch["narrative"]   = args.narrative
    if args.source:      patch["source"]      = args.source
    if args.status:      patch["status"]      = args.status
    if not patch and not args.module:
        print("Nothing to update — no fields provided."); return
    if patch and not S.update("SysFeature", args.id, patch):
        raise CommandError(f"Feature '{args.id}' not found.")
    if args.module:
        with S.write():
            S.link("SysModule", args.module, "PROVIDES", "SysFeature", args.id)
        print(f"  → linked module {args.module}")
    if patch:
        print(f"✓ {args.id}  updated {list(patch)}")


def cmd_retire_feature(args, root, S):
    """Mark a feature as Superseded."""
    patch = {"status": "Superseded"}
    if args.reason:
        patch["retireReason"] = args.reason
    if not S.update("SysFeature", args.id, patch):
        raise CommandError(f"Feature '{args.id}' not found.")
    print(f"✓ {args.id}  retired (Superseded)")


def cmd_show_feature_tests(args, root, S):
    """Show tests linked to a feature."""
    fid = args.feature
    f = S.node("SysFeature", fid)
    if not f:
        raise CommandError(f"Feature '{fid}' not found.")
    test_ids = S.in_ids("SysFeature", fid, "VERIFIES", "SysTest")
    print(f"\n  {fid}  {f.get('name','')}  [{f.get('status','?')}]")
    if not test_ids:
        print("  No tests linked.\n"); return
    tiers: dict = {}
    for tid in sorted(test_ids):
        t = S.node("SysTest", tid)
        tier = t.get("testType", "?") if t else "?"
        tiers.setdefault(tier, []).append((tid, t))
    for tier, tests in sorted(tiers.items()):
        print(f"\n  {tier} ({len(tests)}):")
        for tid, t in tests:
            fn = t.get("fn", "") if t else ""
            print(f"    {tid}  {fn}")
    print()


# ── Linking ───────────────────────────────────────────────────────────────────

def cmd_link_test(args, root, S):
    """Register a test and link it to a feature."""
    tid = f"{args.file}::{args.fn}"
    with S.write():
        S.upsert("SysTest", tid, {
            "file":     args.file,
            "fn":       args.fn,
            "testType": args.type or "integration",
        })
        S.link("SysTest", tid, "VERIFIES", "SysFeature", args.feature)
    print(f"✓ {tid}  → {args.feature}")


def cmd_link_story(args, root, S):
    """Add REQUIRES edges from a story to features."""
    fids = [x.strip() for x in args.features.split(",") if x.strip()]
    with S.write():
        for fid in fids:
            S.link("SysUserStory", args.story, "REQUIRES", "SysFeature", fid)
    print(f"✓ {args.story}  → {', '.join(fids)}")


def cmd_link_usecase(args, root, S):
    """Add feature/story links to a use case."""
    if not S.node("SysUseCase", args.id):
        raise CommandError(f"Use case '{args.id}' not found.")
    with S.write():
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            S.link("SysUseCase", args.id, "REQUIRES", "SysFeature", fid)
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            S.link("SysUserStory", sid, "REALIZED_BY", "SysUseCase", args.id)
    print(f"✓ {args.id}  links added")


def cmd_link_feature(args, root, S):
    """Bulk-link scanned tests to a feature by substring match."""
    patterns = [x.strip() for x in args.tests.split(",") if x.strip()]
    all_tests = S.nodes("SysTest")
    matched = []
    for t in all_tests:
        tid = t["id"]
        if any(p in tid for p in patterns):
            matched.append(t)
    if not matched:
        print(f"  No tests matched patterns: {patterns}"); return
    with S.write():
        for t in matched:
            if args.tier:
                S.update("SysTest", t["id"], {"testType": args.tier})
            S.link("SysTest", t["id"], "VERIFIES", "SysFeature", args.feature)
    print(f"✓ {len(matched)} tests linked to {args.feature}")
    for t in matched:
        print(f"  {t['id']}")


def cmd_link_blocks(args, root, S):
    """Mark one enhancement as BLOCKED_BY another."""
    S.link("SysEnhancement", args.id, "BLOCKED_BY", "SysEnhancement", args.blocked_by)
    print(f"✓ {args.id}  BLOCKED_BY  {args.blocked_by}")


def cmd_unlink_blocks(args, root, S):
    """Remove BLOCKED_BY edge(s) from an enhancement."""
    to_unblock = [x.strip() for x in args.unblock.split(",") if x.strip()]
    with S.write():
        for bid in to_unblock:
            S.q(
                "DELETE FROM edges WHERE rtype=? AND from_label=? AND from_id=? "
                "AND to_label=? AND to_id=?",
                ("BLOCKED_BY", "SysEnhancement", args.id, "SysEnhancement", bid),
            )
    print(f"✓ Removed BLOCKED_BY edge(s) from {args.id}: {', '.join(to_unblock)}")


def cmd_show_blockers(args, root, S):
    """Show BLOCKED_BY edges for an enhancement."""
    enh = S.node("SysEnhancement", args.id)
    if not enh:
        raise CommandError(f"Enhancement '{args.id}' not found.")
    blockers = S.out_ids("SysEnhancement", args.id, "BLOCKED_BY", "SysEnhancement")
    blocking = S.in_ids("SysEnhancement", args.id, "BLOCKED_BY", "SysEnhancement")
    print(f"\n  {args.id}  {enh.get('title','')}")
    if blockers:
        print(f"\n  Blocked by ({len(blockers)}):")
        for bid in sorted(blockers):
            b = S.node("SysEnhancement", bid)
            print(f"    {bid}  {b.get('title','') if b else ''}  [{b.get('status','?') if b else '?'}]")
    else:
        print("  No blockers.")
    if blocking:
        print(f"\n  Blocking ({len(blocking)}):")
        for bid in sorted(blocking):
            b = S.node("SysEnhancement", bid)
            print(f"    {bid}  {b.get('title','') if b else ''}")
    print()


def cmd_link_enhancement(args, root, S):
    """Link an enhancement to features via EXTENDS."""
    fids = [x.strip() for x in args.feature.split(",") if x.strip()]
    with S.write():
        for fid in fids:
            S.link("SysEnhancement", args.id, "EXTENDS", "SysFeature", fid)
    print(f"✓ {args.id}  EXTENDS  {', '.join(fids)}")


def cmd_unlink_enhancement(args, root, S):
    """Remove EXTENDS edges from an enhancement to features."""
    fids = [x.strip() for x in args.feature.split(",") if x.strip()]
    with S.write():
        for fid in fids:
            S.q(
                "DELETE FROM edges WHERE rtype=? AND from_label=? AND from_id=? "
                "AND to_label=? AND to_id=?",
                ("EXTENDS", "SysEnhancement", args.id, "SysFeature", fid),
            )
    print(f"✓ Removed EXTENDS edge(s) from {args.id}: {', '.join(fids)}")


def cmd_unlink_usecase(args, root, S):
    """Remove REQUIRES or REALIZED_BY edges from a use case."""
    with S.write():
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            S.q(
                "DELETE FROM edges WHERE rtype=? AND from_label=? AND from_id=? "
                "AND to_label=? AND to_id=?",
                ("REQUIRES", "SysUseCase", args.id, "SysFeature", fid),
            )
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            S.q(
                "DELETE FROM edges WHERE rtype=? AND from_label=? AND from_id=? "
                "AND to_label=? AND to_id=?",
                ("REALIZED_BY", "SysUserStory", sid, "SysUseCase", args.id),
            )
    print(f"✓ {args.id}  unlinked")


# ── Enhancement workflow additions ────────────────────────────────────────────

def cmd_wontfix_enhancement(args, root, S):
    """Close an enhancement as wont-fix."""
    patch: dict = {"status": "wont-fix"}
    if args.reason:
        patch["reason"] = args.reason
    if not S.update("SysEnhancement", args.id, patch):
        raise CommandError(f"Enhancement '{args.id}' not found.")
    print(f"✓ {args.id}  wont-fix")


def cmd_defer_enhancement(args, root, S):
    """Mark an enhancement as deferred."""
    patch: dict = {"status": "deferred"}
    if args.reason:
        patch["reason"] = args.reason
    if not S.update("SysEnhancement", args.id, patch):
        raise CommandError(f"Enhancement '{args.id}' not found.")
    print(f"✓ {args.id}  deferred")


def cmd_reopen_enhancement(args, root, S):
    """Return an enhancement to proposed state."""
    if not S.update("SysEnhancement", args.id, {"status": "proposed"}):
        raise CommandError(f"Enhancement '{args.id}' not found.")
    print(f"✓ {args.id}  reopened → proposed")


def cmd_approve_enhancement(args, root, S):
    """Promote a proposed enhancement to approved."""
    if not S.update("SysEnhancement", args.id, {"status": "approved"}):
        raise CommandError(f"Enhancement '{args.id}' not found.")
    print(f"✓ {args.id}  approved")


def cmd_list_enhancements(args, root, S):
    """List enhancements filtered by instance and status."""
    inst     = getattr(args, "instance", "") or ""
    status   = getattr(args, "status", "open") or "open"
    priority = getattr(args, "priority", "") or ""

    all_enhs = S.nodes("SysEnhancement")
    if inst:
        all_enhs = [e for e in all_enhs if e.get("instance") == inst]
    if status != "all":
        if status == "open":
            all_enhs = [e for e in all_enhs
                        if e.get("status", "proposed") not in ("done", "wont-fix", "deferred")]
        else:
            all_enhs = [e for e in all_enhs if e.get("status") == status]
    if priority:
        all_enhs = [e for e in all_enhs if e.get("priority") == priority]

    all_enhs.sort(key=lambda e: (PRIO_ORDER.get(e.get("priority", "Could"), 9), e["id"]))
    print(f"\n  {'ID':<14} {'Status':<14} {'Pri':<8} {'Title'}")
    print(f"  {'─'*14} {'─'*14} {'─'*8} {'─'*40}")
    for e in all_enhs:
        print(f"  {e['id']:<14} {e.get('status','proposed'):<14} "
              f"{e.get('priority','?'):<8} {e.get('title','')[:55]}")
    print(f"\n  {len(all_enhs)} enhancements\n")


def cmd_list_defects(args, root, S):
    """List defects filtered by instance and status."""
    inst   = getattr(args, "instance", "") or ""
    status = getattr(args, "status", "open") or "open"

    all_defs = S.nodes("SysDefect")
    if inst:
        all_defs = [d for d in all_defs if d.get("instance") == inst]
    if status != "all":
        if status == "open":
            all_defs = [d for d in all_defs if d.get("status", "open") != "closed"]
        else:
            all_defs = [d for d in all_defs if d.get("status") == status]

    all_defs.sort(key=lambda d: (SEV_ORDER.get(d.get("severity", "low"), 9), d["id"]))
    print(f"\n  {'ID':<12} {'Sev':<10} {'Status':<14} {'Title'}")
    print(f"  {'─'*12} {'─'*10} {'─'*14} {'─'*40}")
    for d in all_defs:
        sev  = d.get("severity", "medium")
        mark = SEV_MARK.get(sev, "○○○○")
        print(f"  {d['id']:<12} {mark} {sev:<5} {d.get('status','open'):<14} {d.get('title','')[:50]}")
    print(f"\n  {len(all_defs)} defects\n")


def cmd_update_defect(args, root, S):
    """Patch fields on a defect."""
    patch = {}
    if args.title:                               patch["title"]              = args.title
    if args.description:                         patch["description"]        = args.description
    if args.severity:                            patch["severity"]           = args.severity
    if getattr(args, "acceptance_criteria", ""): patch["acceptanceCriteria"] = args.acceptance_criteria
    if not patch:
        print("Nothing to update — no fields provided."); return
    if not S.update("SysDefect", args.id, patch):
        raise CommandError(f"Defect '{args.id}' not found.")
    print(f"✓ {args.id}  updated {list(patch)}")


# ── ADRs ──────────────────────────────────────────────────────────────────────

def cmd_create_adr(args, root, S):
    """Create or update a SysArchDecision (ADR) node."""
    props = {
        "title":        args.title,
        "status":       args.status or "proposed",
        "date":         args.date or _now_iso()[:10],
        "decision":     args.decision or getattr(args, "description", "") or "",
        "context":      args.context or "",
        "consequences": args.consequences or "",
    }
    with S.write():
        S.upsert("SysArchDecision", args.id, props)
        sys_nodes = S.nodes("SysSystem")
        if sys_nodes:
            S.link("SysSystem", sys_nodes[0]["id"], "GOVERNED_BY", "SysArchDecision", args.id)
        for std_id in [x.strip() for x in (args.addresses or "").split(",") if x.strip()]:
            S.link("SysArchDecision", args.id, "ADDRESSES", "SysArchStd", std_id)
        for sup in [x.strip() for x in (args.supersedes or "").split(",") if x.strip()]:
            S.link("SysArchDecision", args.id, "SUPERSEDES", "SysArchDecision", sup)
            S.update("SysArchDecision", sup, {"status": "superseded"})
    print(f"✓ {args.id}  [{args.status or 'proposed'}]  {args.title}")


def cmd_seed_standards(args, root, S):
    """Load the architecture standards library from YAML."""
    try:
        import yaml as _yaml
    except ImportError:
        raise CommandError(
            "seed-standards needs PyYAML (pip install pyyaml). "
            "Alternatively use the Neo4j backend."
        )
    if args.file:
        path = Path(args.file)
    else:
        candidates = [
            Path(__file__).parent / "examples" / "arch-standards.yaml",
            Path(__file__).parent.parent / "examples" / "arch-standards.yaml",
            root / "examples" / "arch-standards.yaml",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if not path:
            raise CommandError(
                "Bundled arch-standards.yaml not found. "
                "Pass the path explicitly: seed-standards <path>"
            )
    data = _yaml.safe_load(path.read_text(encoding="utf-8"))
    count = 0
    with S.write():
        for _domain, items in data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                sid = item.get("id") or item.get("name", "").replace(" ", "-")
                if not sid:
                    continue
                S.upsert("SysArchStd", sid, {
                    "name":           item.get("name") or item.get("title") or sid,
                    "title":          item.get("title") or item.get("name") or sid,
                    "type":           item.get("type", "standard"),
                    "source":         item.get("source", ""),
                    "rationale":      item.get("rationale") or item.get("description") or "",
                    "complianceNote": item.get("complianceNote", ""),
                    "enforcement":    item.get("enforcement", ""),
                    "evaluation":     item.get("evaluation", ""),
                    "tier":           item.get("tier", ""),
                    "appliesToTypes": ",".join(item.get("appliesToTypes", [])),
                })
                count += 1
                for addr in (item.get("addressed_by") or []):
                    S.link("SysArchDecision", addr, "ADDRESSES", "SysArchStd", sid)
    print(f"✓ {count} architecture standards seeded from {path.name}")


# ── Analysis ──────────────────────────────────────────────────────────────────

def cmd_coverage_report(args, root, S):
    """4-tier V-model gap analysis: US→UC→Feature→Test traceability."""
    inst = getattr(args, "instance", "") or ""
    tier = getattr(args, "tier", "all") or "all"

    all_stories = S.nodes("SysUserStory")
    all_ucs     = S.nodes("SysUseCase")
    all_feats   = [f for f in S.nodes("SysFeature")
                   if f.get("status") not in ("Superseded", "Deprecated")]

    if inst:
        all_ucs = [u for u in all_ucs if u.get("instance") == inst]
        mod_ids = {m["id"] for m in S.nodes("SysModule", instance=inst)}
        feat_ids_in_inst: set = set()
        for mid in mod_ids:
            feat_ids_in_inst.update(S.out_ids("SysModule", mid, "PROVIDES", "SysFeature"))
        all_feats = [f for f in all_feats if f["id"] in feat_ids_in_inst]

    t1_gaps = [s for s in all_stories
               if not S.out_ids("SysUserStory", s["id"], "REALIZED_BY", "SysUseCase")]
    t2_gaps = [u for u in all_ucs
               if not S.out_ids("SysUseCase", u["id"], "REQUIRES", "SysFeature")]
    t3_gaps = [f for f in all_feats
               if not S.in_ids("SysFeature", f["id"], "VERIFIES", "SysTest")]

    t4_gaps = []
    for s in all_stories:
        has_e2e = False
        for uid in S.out_ids("SysUserStory", s["id"], "REALIZED_BY", "SysUseCase"):
            for fid in S.out_ids("SysUseCase", uid, "REQUIRES", "SysFeature"):
                for tid in S.in_ids("SysFeature", fid, "VERIFIES", "SysTest"):
                    t = S.node("SysTest", tid)
                    if t and t.get("testType") == "e2e":
                        has_e2e = True; break
                if has_e2e: break
            if has_e2e: break
        if not has_e2e:
            t4_gaps.append(s)

    def header(label):
        return f"\n  {'─'*W}\n  {label}\n  {'─'*W}"

    show_all = (tier == "all")
    if show_all or tier == "1":
        print(header(f"T1  Story→UC  ({len(t1_gaps)} gaps / {len(all_stories)} stories)"))
        for s in t1_gaps:
            print(f"  ✗ {s['id']:<18} {s.get('title','')[:50]}")
    if show_all or tier == "2":
        print(header(f"T2  UC→Feature  ({len(t2_gaps)} gaps / {len(all_ucs)} UCs)"))
        for u in t2_gaps:
            print(f"  ✗ {u['id']:<20} {u.get('title','')[:50]}")
    if show_all or tier == "3":
        print(header(f"T3  Feature→Test  ({len(t3_gaps)} gaps / {len(all_feats)} features)"))
        for f in t3_gaps:
            print(f"  ✗ {f['id']:<18} {f.get('name','')[:50]}")
    if show_all or tier == "4":
        print(header(f"T4  Story→E2E  ({len(t4_gaps)} gaps / {len(all_stories)} stories)"))
        for s in t4_gaps:
            print(f"  ✗ {s['id']:<18} {s.get('title','')[:50]}")

    total = len(t1_gaps) + len(t2_gaps) + len(t3_gaps) + len(t4_gaps)
    print(f"\n  Total gaps: {total}\n")


def cmd_trace_matrix(args, root, S):
    """Export traceability matrix as CSV or JSON."""
    import csv, hashlib

    inst   = getattr(args, "instance", "") or ""
    fmt    = getattr(args, "format", "csv") or "csv"
    today  = _now_iso()[:10]
    scope  = getattr(args, "module", "") or inst or "all"

    if fmt == "xlsx":
        raise CommandError("xlsx format not available in SQLite backend — use --format csv or --format json")

    out_path = Path(args.output) if getattr(args, "output", None) else \
        Path("docs/exports") / f"trace-matrix-{scope}-{today}.{fmt}"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    uc_inst_filter = {u["id"] for u in S.nodes("SysUseCase", instance=inst)} if inst else None
    for story in sorted(S.nodes("SysUserStory"), key=lambda s: s["id"]):
        uc_ids = S.out_ids("SysUserStory", story["id"], "REALIZED_BY", "SysUseCase")
        if uc_inst_filter is not None:
            uc_ids = [uid for uid in uc_ids if uid in uc_inst_filter]
        if not uc_ids:
            rows.append({"story_id": story["id"], "story_title": story.get("title", ""),
                         "uc_id": "", "uc_title": "", "feature_id": "", "feature_name": "",
                         "test_id": "", "test_type": "", "test_file": ""}); continue
        for uid in sorted(uc_ids):
            uc = S.node("SysUseCase", uid) or {}
            feat_ids = S.out_ids("SysUseCase", uid, "REQUIRES", "SysFeature")
            if not feat_ids:
                rows.append({"story_id": story["id"], "story_title": story.get("title", ""),
                             "uc_id": uid, "uc_title": uc.get("title", ""),
                             "feature_id": "", "feature_name": "", "test_id": "",
                             "test_type": "", "test_file": ""}); continue
            for fid in sorted(feat_ids):
                feat = S.node("SysFeature", fid) or {}
                test_ids = S.in_ids("SysFeature", fid, "VERIFIES", "SysTest")
                if not test_ids:
                    rows.append({"story_id": story["id"], "story_title": story.get("title", ""),
                                 "uc_id": uid, "uc_title": uc.get("title", ""),
                                 "feature_id": fid, "feature_name": feat.get("name", ""),
                                 "test_id": "", "test_type": "", "test_file": ""}); continue
                for tid in sorted(test_ids):
                    t = S.node("SysTest", tid) or {}
                    rows.append({"story_id": story["id"], "story_title": story.get("title", ""),
                                 "uc_id": uid, "uc_title": uc.get("title", ""),
                                 "feature_id": fid, "feature_name": feat.get("name", ""),
                                 "test_id": tid, "test_type": t.get("testType", ""),
                                 "test_file": t.get("file", "")})

    cols = ["story_id", "story_title", "uc_id", "uc_title",
            "feature_id", "feature_name", "test_id", "test_type", "test_file"]

    if fmt == "csv":
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=cols).writeheader()
            csv.DictWriter(fh, fieldnames=cols).writerows(rows)
    else:
        import json
        out_path.write_text(
            json.dumps({"source": f"sqlite:{scope}", "columns": cols, "rows": rows}, indent=2),
            encoding="utf-8",
        )

    digest   = hashlib.sha256(out_path.read_bytes()).hexdigest()
    manifest = out_path.with_suffix(out_path.suffix + ".sha256")
    manifest.write_text(f"{digest}  {out_path.name}\n", encoding="utf-8")
    print(f"✓ {len(rows)} rows → {out_path}\n  sha256 {digest}")


def cmd_export(args, root, S):
    """Export graph slice as Markdown document (stories or use-cases)."""
    export_type = args.type
    inst        = getattr(args, "instance", "") or ""
    mod_id      = getattr(args, "module",   "") or ""
    today       = _now_iso()[:10]
    scope       = mod_id or inst or "all"

    if export_type not in ("stories", "use-cases"):
        raise CommandError(
            f"Export type '{export_type}' requires the Neo4j backend. "
            "SQLite backend supports: stories, use-cases."
        )

    out_path = Path(args.output) if getattr(args, "output", None) else \
        Path(f"docs/exports/{export_type}-{scope}-{today}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list = []
    if export_type == "stories":
        lines.append(f"# User Stories — {scope}  ({today})\n\n")
        stories = S.nodes("SysUserStory")
        if inst:
            mod_ids = {m["id"] for m in S.nodes("SysModule", instance=inst)}
            feat_ids: set = set()
            for mid in mod_ids:
                feat_ids.update(S.out_ids("SysModule", mid, "PROVIDES", "SysFeature"))
            story_ids: set = set()
            for fid in feat_ids:
                story_ids.update(S.in_ids("SysFeature", fid, "REQUIRES", "SysUserStory"))
            stories = [s for s in stories if s["id"] in story_ids]
        for s in sorted(stories, key=lambda x: x["id"]):
            lines.append(f"## {s['id']} — {s.get('title', '?')}\n\n")
            lines.append(f"**Actor:** {s.get('actor', '?')}  \n")
            lines.append(f"**Goal:** {s.get('goal', '?')}  \n")
            if s.get("benefit"):
                lines.append(f"**Benefit:** {s['benefit']}  \n")
            if s.get("acceptanceCriteria"):
                lines.append(f"\n**Acceptance Criteria:** {s['acceptanceCriteria']}\n")
            lines.append("\n---\n\n")
    else:
        lines.append(f"# Use Cases — {scope}  ({today})\n\n")
        ucs = S.nodes("SysUseCase")
        if inst:
            ucs = [u for u in ucs if u.get("instance") == inst]
        if mod_id:
            feat_ids_mod = set(S.out_ids("SysModule", mod_id, "PROVIDES", "SysFeature"))
            ucs = [u for u in ucs
                   if set(S.out_ids("SysUseCase", u["id"], "REQUIRES", "SysFeature")) & feat_ids_mod]
        for u in sorted(ucs, key=lambda x: x["id"]):
            lines.append(f"## {u['id']} — {u.get('title', '?')}  [{u.get('priority', 'P1')}]\n\n")
            if u.get("description"):
                lines.append(f"{u['description']}\n\n")
            if u.get("preconditions"):
                lines.append(f"**Pre:** {u['preconditions']}  \n")
            if u.get("mainFlow"):
                lines.append(f"\n**Main Flow:**\n\n{u['mainFlow']}\n\n")
            if u.get("postconditions"):
                lines.append(f"**Post:** {u['postconditions']}  \n")
            if u.get("authorizedRoles"):
                lines.append(f"**Roles:** {u['authorizedRoles']}  \n")
            if u.get("failureScenarios"):
                lines.append(f"**Failures:** {u['failureScenarios']}  \n")
            feat_ids = S.out_ids("SysUseCase", u["id"], "REQUIRES", "SysFeature")
            if feat_ids:
                lines.append(f"\n**Features:** {', '.join(sorted(feat_ids))}\n")
            lines.append("\n---\n\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"✓ {export_type}  →  {out_path}")


def cmd_health_report(args, root, S):
    """Overall health summary of the graph."""
    counts = {
        "SysSystem":      len(S.nodes("SysSystem")),
        "SysDomain":      len(S.nodes("SysDomain")),
        "SysModule":      len(S.nodes("SysModule")),
        "SysFeature":     len([f for f in S.nodes("SysFeature")
                               if f.get("status") not in ("Superseded", "Deprecated")]),
        "SysUserStory":   len(S.nodes("SysUserStory")),
        "SysUseCase":     len(S.nodes("SysUseCase")),
        "SysTest":        len(S.nodes("SysTest")),
        "SysEnhancement": len(S.nodes("SysEnhancement")),
        "SysDefect (open)": len([d for d in S.nodes("SysDefect") if d.get("status") != "closed"]),
        "SysArchDecision": len(S.nodes("SysArchDecision")),
        "SysArchStd":     len(S.nodes("SysArchStd")),
    }
    stories = S.nodes("SysUserStory")
    feats   = [f for f in S.nodes("SysFeature")
               if f.get("status") not in ("Superseded", "Deprecated")]
    stories_with_uc  = sum(1 for s in stories
                           if S.out_ids("SysUserStory", s["id"], "REALIZED_BY", "SysUseCase"))
    feats_with_tests = sum(1 for f in feats
                           if S.in_ids("SysFeature", f["id"], "VERIFIES", "SysTest"))
    print(f"\n  {'─'*W}")
    print(f"  SysEdge Health Report  {_now_iso()[:10]}")
    print(f"  {'─'*W}\n  Graph nodes:")
    for label, n in counts.items():
        bar = "█" * min(n, 30)
        print(f"    {label:<26} {n:>5}  {bar}")
    s_pct = (stories_with_uc / len(stories) * 100) if stories else 0
    f_pct = (feats_with_tests / len(feats) * 100) if feats else 0
    print(f"\n  Coverage:")
    print(f"    Stories with UCs       {stories_with_uc:>5}/{len(stories):<5}  {s_pct:.0f}%")
    print(f"    Features with tests    {feats_with_tests:>5}/{len(feats):<5}  {f_pct:.0f}%")
    print()


def cmd_full_inventory(args, root, S):
    """Complete inventory: all modules with feature and test counts."""
    inst = getattr(args, "instance", "") or ""
    modules = S.nodes("SysModule")
    if inst:
        modules = [m for m in modules if m.get("instance") == inst]
    modules.sort(key=lambda m: m["id"])

    print(f"\n  {'─'*W}")
    print(f"  Full Inventory{f'  [{inst}]' if inst else ''}")
    print(f"  {'─'*W}")
    print(f"\n  {'Module':<20} {'Features':<10} {'WithTests':<12} {'Coverage'}")
    print(f"  {'─'*20} {'─'*10} {'─'*12} {'─'*10}")
    for mod in modules:
        feat_ids = S.out_ids("SysModule", mod["id"], "PROVIDES", "SysFeature")
        active   = [f for fid in feat_ids for f in [S.node("SysFeature", fid)]
                    if f and f.get("status") not in ("Superseded", "Deprecated")]
        with_tests = sum(1 for f in active
                         if S.in_ids("SysFeature", f["id"], "VERIFIES", "SysTest"))
        pct = f"{with_tests/len(active)*100:.0f}%" if active else "—"
        print(f"  {mod['id']:<20} {len(active):<10} {with_tests:<12} {pct}")
    print()


# ── Phase 3 — test-run tracking ───────────────────────────────────────────────

def _parse_pytest_json(path: str) -> dict:
    """Return {test_node_id: outcome} from a pytest-json-report file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        outcomes: dict = {}
        for test in data.get("tests", []):
            node_id = test.get("nodeid", "")
            outcomes[node_id] = "passed" if test.get("outcome") == "passed" else "failed"
        return outcomes
    except Exception as e:
        raise CommandError(f"Could not parse --results-json '{path}': {e}")


def cmd_record_run(args, root, S):
    pkg_id      = args.package
    passed      = args.passed
    failed      = args.failed
    skipped     = getattr(args, "skipped",      0)   or 0
    xfailed     = getattr(args, "xfailed",      0)   or 0
    duration    = getattr(args, "duration",     0.0) or 0.0
    notes       = getattr(args, "notes",        "") or ""
    skip_reason = getattr(args, "skip_reason",  "") or ""
    results_json = getattr(args, "results_json", "") or ""

    per_test: dict = {}
    if results_json:
        per_test = _parse_pytest_json(results_json)

    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    now    = _now_iso()
    status = "passed" if failed == 0 else "failed"
    area   = pkg_id.split("/")[-1].replace(".py", "")

    with S.write():
        S.upsert("SysTestPackage", pkg_id, {
            "path": pkg_id, "area": area,
            "lastRun": now, "lastPassed": passed, "lastFailed": failed,
            "lastSkipped": skipped, "lastXFailed": xfailed,
            "lastDuration": duration, "lastStatus": status,
        })
        S.upsert("SysTestRun", run_id, {
            "packageId": pkg_id, "ranAt": now,
            "passed": passed, "failed": failed,
            "skipped": skipped, "xfailed": xfailed,
            "durationS": duration, "status": status,
            "notes": notes, "skipReason": skip_reason,
        })
        S.link("SysTestPackage", pkg_id, "HAS_RUN", "SysTestRun", run_id)

        updated = 0
        if per_test:
            for tid, outcome in per_test.items():
                row = S.q("SELECT label FROM nodes WHERE label='SysTest' AND id=?", (tid,))
                if row:
                    S.update("SysTest", tid, {"lastTestedAt": now, "lastStatus": outcome})
                    updated += 1
            pkg_tests = S.q(
                "SELECT e.to_id FROM edges e WHERE e.rtype='CONTAINS_TEST' "
                "AND e.from_label='SysTestPackage' AND e.from_id=?", pkg_id)
            for r in pkg_tests:
                t = S.node("SysTest", r[0])
                if t and not t.get("lastTestedAt"):
                    S.update("SysTest", r[0], {"lastTestedAt": now, "lastStatus": status})
                    updated += 1
        else:
            all_tests = S.q("SELECT id, props FROM nodes WHERE label='SysTest'")
            for row in all_tests:
                d = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                file_ = d.get("file", "")
                if file_ == pkg_id or file_.startswith(pkg_id):
                    S.update("SysTest", row[0], {"lastTestedAt": now, "lastStatus": status})
                    updated += 1

    sym     = "✓" if failed == 0 else f"✗ {failed} failed"
    skip_s  = f"  {skipped}s"  if skipped  else ""
    xfail_s = f"  {xfailed}x"  if xfailed  else ""
    note_s  = f"  [{notes}]"   if notes    else ""
    print(f"  {sym} {passed}✓{skip_s}{xfail_s}  {pkg_id}{note_s}")
    if updated:
        print(f"     updated lastTestedAt on {updated} test nodes")


def cmd_run_history(args, root, S):
    package   = getattr(args, "package",  "") or ""
    instance  = getattr(args, "instance", "") or ""
    runs_back = getattr(args, "runs",     10) or 10

    if package:
        pkg_ids = [package]
    else:
        rows = S.q("SELECT id, props FROM nodes WHERE label='SysTestPackage'")
        pkg_ids = []
        for r in rows:
            d = json.loads(r[1]) if isinstance(r[1], str) else r[1]
            if instance and d.get("instance") != instance:
                continue
            pkg_ids.append(r[0])
        pkg_ids.sort()

    if not pkg_ids:
        print("No test packages with run history found.")
        return

    print(f"\nRUN HISTORY  (last {runs_back} runs per package)\n")
    for pid in pkg_ids:
        run_ids = [r[0] for r in S.q(
            "SELECT e.to_id FROM edges e WHERE e.rtype='HAS_RUN' "
            "AND e.from_label='SysTestPackage' AND e.from_id=?", pid)]
        if not run_ids:
            continue
        runs = []
        for rid in run_ids:
            n = S.node("SysTestRun", rid)
            if n:
                runs.append(n)
        runs.sort(key=lambda x: x.get("ranAt", ""), reverse=True)
        runs = runs[:runs_back]
        if not runs:
            continue
        statuses  = [r.get("status", "") for r in runs]
        any_fail  = any(s == "failed" for s in statuses)
        any_pass  = any(s == "passed" for s in statuses)
        flaky     = "  ⚡ FLAKY" if any_fail and any_pass else ""
        pass_rate = int(100 * statuses.count("passed") / len(statuses)) if statuses else 0
        bar       = ("█" * (pass_rate // 10)).ljust(10, "░")
        print(f"  {pid}")
        print(f"    pass rate: {bar} {pass_rate}%  ({len(runs)} runs){flaky}")
        for r in runs:
            sym = "✓" if r.get("status") == "passed" else "✗"
            ts  = (r.get("ranAt") or "")[:16].replace("T", " ")
            dur = f"{r['durationS']:.1f}s" if r.get("durationS") else "—"
            p, f = r.get("passed") or 0, r.get("failed") or 0
            print(f"    {sym}  {ts}  {p}✓ {f}✗  {dur}")
        print()


def cmd_prune_runs(args, root, S):
    days    = getattr(args, "days",    30) or 30
    keep    = getattr(args, "keep",    5)  or 5
    dry_run = getattr(args, "dry_run", False)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    pkg_rows  = S.q("SELECT id FROM nodes WHERE label='SysTestPackage'")
    to_delete: list = []

    for pr in pkg_rows:
        pkg_id  = pr[0]
        run_ids = [r[0] for r in S.q(
            "SELECT e.to_id FROM edges e WHERE e.rtype='HAS_RUN' "
            "AND e.from_label='SysTestPackage' AND e.from_id=?", pkg_id)]
        runs = []
        for rid in run_ids:
            n = S.node("SysTestRun", rid)
            if n:
                runs.append((rid, n.get("ranAt", "")))
        runs.sort(key=lambda x: x[1], reverse=True)
        old = [r[0] for r in runs[keep:] if r[1] < cutoff]
        to_delete.extend(old)

    if not to_delete:
        print(f"  Nothing to prune (all runs within {days} days or within keep={keep} window).")
        return

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"  {prefix}Pruning {len(to_delete)} SysTestRun nodes "
          f"(older than {days}d, keeping last {keep} per package):")
    if not dry_run:
        with S.write():
            for rid in to_delete:
                S.delete_node("SysTestRun", rid)
        print(f"\n  ✓ Deleted {len(to_delete)} SysTestRun nodes.")
    else:
        print(f"\n  [DRY RUN] Would delete {len(to_delete)} nodes — rerun without --dry-run to apply.")


def cmd_test_status(args, root, S):
    days     = getattr(args, "days",     7) or 7
    instance = getattr(args, "instance", "") or ""

    pkg_rows = S.q("SELECT id, props FROM nodes WHERE label='SysTestPackage'")
    packages = []
    for r in pkg_rows:
        d = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        if instance and d.get("instance") != instance:
            continue
        d["_id"] = r[0]
        packages.append(d)

    never   = [p for p in packages if not p.get("lastRun")]
    ran     = [p for p in packages if p.get("lastRun")]
    failing = [p for p in ran if (p.get("lastFailed") or 0) > 0]

    print(f"\nTEST PACKAGES — {len(ran)}/{len(packages)} run  "
          f"({len(failing)} failing  {len(never)} never run)")
    if ran:
        print()
        last_area = None
        for p in sorted(ran, key=lambda x: (x.get("area") or "", x.get("_id") or "")):
            area = p.get("area") or "?"
            if area != last_area:
                print(f"  {area}")
                last_area = area
            ts     = (p.get("lastRun") or "")[:19]
            tick   = "✓" if (p.get("lastFailed") or 0) == 0 else "✗"
            dur    = f" {p['lastDuration']:.0f}s" if p.get("lastDuration") else ""
            skip_s = f" {p['lastSkipped']}s"  if p.get("lastSkipped") else ""
            xf_s   = f" {p['lastXFailed']}x"  if p.get("lastXFailed")  else ""
            name   = p.get("_id", "").split("/")[-1].replace(".py", "")
            print(f"    {tick} {name:<55} {ts}  "
                  f"{p.get('lastPassed') or 0}✓{skip_s}{xf_s} {p.get('lastFailed') or 0}✗{dur}")

    cutoff    = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    all_tests = S.q("SELECT props FROM nodes WHERE label='SysTest'")
    never_run = 0
    stale     = 0
    for r in all_tests:
        d    = json.loads(r[0]) if isinstance(r[0], str) else r[0]
        last = d.get("lastTestedAt")
        if not last:
            never_run += 1
        elif last < cutoff:
            stale += 1

    print(f"\n  never run: {never_run} tests")
    print(f"  stale (>{days}d): {stale} tests\n")


# ── Phase 3 — utility commands ────────────────────────────────────────────────

def cmd_impact(args, root, S):
    path   = getattr(args, "path",   "") or ""
    symbol = getattr(args, "symbol", "") or ""
    if not path and not symbol:
        raise CommandError("ERROR: specify --path and/or --symbol")

    W = 66
    print(f"\n{'═'*W}\n  IMPACT ANALYSIS\n{'═'*W}")

    if path:
        test_rows = S.q(
            "SELECT n.id, n.props, e.to_id FROM nodes n "
            "JOIN edges e ON e.from_label='SysTest' AND e.from_id=n.id AND e.rtype='VERIFIES' "
            "WHERE n.label='SysTest' AND n.props LIKE ?",
            f"%{path}%")
        feat_map: dict = {}
        for r in test_rows:
            feat_map.setdefault(r[2], []).append(r[0])

        print(f"\n  File: {path}")
        if feat_map:
            print(f"  Via tests ({len(feat_map)} feature links):")
            for fid, tids in sorted(feat_map.items())[:10]:
                fn    = S.node("SysFeature", fid)
                fname = (fn or {}).get("name", "")
                print(f"    {fid:<16} {fname[:40]}  ({len(tids)} tests)")
        else:
            print(f"  ✗ No feature links found for path '{path}'")

    if symbol:
        print(f"\n  Symbol: {symbol}")
        print("  ✗ Symbol-level tracing requires scan-code — not yet available in SQLite backend")

    print(f"\n{'═'*W}")


def cmd_link_commit(args, root, S):
    sha       = args.sha.strip()
    message   = getattr(args, "message",      "") or ""
    enh_id    = getattr(args, "enhancement",  "") or ""
    defect_id = getattr(args, "defect",       "") or ""
    instance  = getattr(args, "instance",     "") or ""

    if not enh_id and not defect_id:
        raise CommandError("ERROR: provide --enhancement or --defect")

    note_id = f"NOTE-COMMIT-{sha[:12]}"
    with S.write():
        S.upsert("SysNote", note_id, {
            "sha": sha, "message": message, "instance": instance,
            "createdAt": _now_iso(), "noteType": "commit",
        })
        if enh_id:
            if not S.node("SysEnhancement", enh_id):
                raise CommandError(f"Enhancement {enh_id} not found")
            S.link("SysEnhancement", enh_id, "HAS_NOTE", "SysNote", note_id)
        if defect_id:
            if not S.node("SysDefect", defect_id):
                raise CommandError(f"Defect {defect_id} not found")
            S.link("SysDefect", defect_id, "HAS_NOTE", "SysNote", note_id)

    targets = " ".join(filter(None, [enh_id, defect_id]))
    msg_str = f"  {message[:50]}" if message else ""
    print(f"✓ {sha[:12]}{msg_str}  → {targets}")


def cmd_add_standard(args, root, S):
    existing = S.node("SysArchStd", args.id)
    if existing:
        _err(f"⚠ {args.id} already exists — use update commands or edit directly")
        sys.exit(1)
    types   = [t.strip() for t in (args.applies_to or "").split(",") if t.strip()]
    enforce = getattr(args, "enforcement", "mandatory") or "mandatory"
    with S.write():
        S.upsert("SysArchStd", args.id, {
            "title": args.title, "description": args.description,
            "category": args.category or "quality", "status": "adopted",
            "appliesToTypes": types or ["SysUseCase"],
            "appliesWhen": getattr(args, "applies_when", "") or "",
            "source": "project", "tier": "project",
            "evaluationMethod": getattr(args, "evaluation", "manual") or "manual",
            "enforcement": enforce,
        })
    cat    = args.category or "quality"
    method = getattr(args, "evaluation", "manual") or "manual"
    print(f"✓ {args.id} created  [{cat}]  tier=project  evaluation={method}  enforcement={enforce}")


# ── Phase 3 — scan-tests ──────────────────────────────────────────────────────

import re as _re

_ENTITY_REF_RE = _re.compile(
    r'\b(F-[A-Z0-9][A-Z0-9-]*|US-[A-Z0-9][A-Z0-9-]*|UC-[A-Z0-9][A-Z0-9-]*'
    r'|ENH-[A-Z0-9][A-Z0-9-]*|DEF-[A-Z0-9][A-Z0-9-]*)\b'
)


def _scan_python_tests(file_path: str) -> list:
    """Return list of test function names from a Python file using ast."""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=file_path)
        fns = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    fns.append(node.name)
        return fns
    except (SyntaxError, Exception):
        return []


def cmd_scan_tests(args, root, S):
    path = getattr(args, "path", "") or ""
    if not path:
        raise CommandError("ERROR: --path is required for SQLite backend scan-tests")

    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = root / file_path
    if not file_path.exists():
        raise CommandError(f"ERROR: file not found: {file_path}")

    pkg_id = str(path)
    area   = file_path.stem

    valid_ids: set = set()
    for label in ("SysFeature", "SysUserStory", "SysUseCase"):
        for row in S.q("SELECT id FROM nodes WHERE label=?", (label,)):
            valid_ids.add(row[0])

    suffix = file_path.suffix.lower()
    if suffix != ".py":
        raise CommandError(f"SQLite scan-tests supports .py files only (got {suffix})")

    fn_names = _scan_python_tests(str(file_path))
    if not fn_names:
        print(f"  {pkg_id}: no test functions found")
        return

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        source = ""
    refs = [m for m in _ENTITY_REF_RE.findall(source) if m in valid_ids]

    with S.write():
        S.upsert("SysTestPackage", pkg_id, {
            "path": pkg_id, "area": area, "canonicalRoot": "tests",
        })
        for fn in fn_names:
            tid = f"{path}::{fn}"
            S.upsert("SysTest", tid, {
                "file": path, "fn": fn, "testType": "integration",
                "packageId": pkg_id,
            })
            S.link("SysTestPackage", pkg_id, "CONTAINS_TEST", "SysTest", tid)
            for ref_id in refs:
                if ref_id.startswith("F-"):
                    S.link("SysTest", tid, "VERIFIES", "SysFeature", ref_id)

    link_note = f" (+{len(refs)} auto-links)" if refs else ""
    print(f"  {pkg_id}: {len(fn_names)} test functions registered{link_note}")


# ── Phase 3 — session-driven AI review ───────────────────────────────────────

def _sqlite_build_coverage_subject(S, us_id: str, uc_id: str) -> dict | None:
    if us_id:
        us = S.node("SysUserStory", us_id)
        if not us:
            return None
        uc_pairs = S.out_ids("SysUserStory", us_id, "REALIZED_BY")
        linked_ucs = []
        for (lbl, uid) in uc_pairs:
            if lbl != "SysUseCase":
                continue
            uc = S.node("SysUseCase", uid) or {}
            feat_pairs = S.out_ids("SysUseCase", uid, "REQUIRES")
            test_count = 0
            for (fl, fid) in feat_pairs:
                test_count += len(S.in_ids("SysFeature", fid, "VERIFIES"))
            linked_ucs.append({
                "id": uid, "title": uc.get("title", ""),
                "description": uc.get("description", ""), "testCount": test_count,
                "mainFlow": uc.get("mainFlow", ""),
                "preconditions": uc.get("preconditions", ""),
                "authorizedRoles": uc.get("authorizedRoles", ""),
                "failureScenarios": uc.get("failureScenarios", ""),
            })
        context_lines = [
            f"User Story: {us.get('title', '')} ({us_id})",
            f"Actor: {us.get('actor') or 'User'}",
            f"Goal: {us.get('goal') or ''}",
        ]
        if us.get("benefit"):
            context_lines.append(f"So that: {us['benefit']}")
        if us.get("acceptanceCriteria"):
            context_lines.append(f"Acceptance criteria: {us['acceptanceCriteria']}")
        if linked_ucs:
            uc_lines = "\n".join(
                f"  [{u['id']}] {u['title']} (tests: {u['testCount']})"
                + (f"\n    Main flow: {u['mainFlow']}" if u.get("mainFlow") else "")
                + (f"\n    Preconditions: {u['preconditions']}" if u.get("preconditions") else "")
                + (f"\n    Authorized roles: {u['authorizedRoles']}" if u.get("authorizedRoles") else "")
                + (f"\n    Failure scenarios: {u['failureScenarios']}" if u.get("failureScenarios") else "")
                + (f"\n    {u['description']}" if u.get("description") else "")
                for u in linked_ucs
            )
            context_lines.append(f"\nRealising use cases:\n{uc_lines}")
        return {
            "subjectId": us_id, "subjectType": "SysUserStory",
            "title": us.get("title", ""), "context": "\n".join(context_lines),
            "linkedUseCases": linked_ucs,
        }

    if uc_id:
        uc = S.node("SysUseCase", uc_id)
        if not uc:
            return None
        feat_pairs = S.out_ids("SysUseCase", uc_id, "REQUIRES")
        feat_ids = [fid for (fl, fid) in feat_pairs if fl == "SysFeature"]
        test_count = sum(len(S.in_ids("SysFeature", fid, "VERIFIES")) for fid in feat_ids)
        story_pairs = S.in_ids("SysUseCase", uc_id, "REALIZED_BY")
        story_ids = [sid for (sl, sid) in story_pairs if sl == "SysUserStory"]
        context_lines = [
            f"Use Case: {uc.get('title', '')} ({uc_id})",
            f"Instance: {uc.get('instance') or 'unknown'}",
            f"Parent stories: {', '.join(story_ids) or '(none)'}",
            f"Features: {', '.join(feat_ids) or '(none)'}",
            f"Tests via UC→Feature→Test path: {test_count}",
        ]
        if uc.get("preconditions"):
            context_lines.append(f"Preconditions: {uc['preconditions']}")
        if uc.get("mainFlow"):
            context_lines.append(f"Main flow: {uc['mainFlow']}")
        if uc.get("authorizedRoles"):
            context_lines.append(f"Authorized roles: {uc['authorizedRoles']}")
        if uc.get("failureScenarios"):
            context_lines.append(f"Failure scenarios: {uc['failureScenarios']}")
        if uc.get("description"):
            context_lines.append(f"\nDescription:\n{uc['description']}")
        return {
            "subjectId": uc_id, "subjectType": "SysUseCase",
            "title": uc.get("title", ""), "context": "\n".join(context_lines),
            "linkedFeatures": feat_ids, "testCount": test_count,
        }
    return None


def _sqlite_fetch_as_req_standards(S) -> list[dict]:
    rows = S.q(
        "SELECT id, props FROM nodes WHERE label='SysArchStd' AND id LIKE 'AS-REQ-%' ORDER BY id")
    stds = []
    for r in rows:
        d = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        stds.append({"id": r[0], "title": d.get("title", ""), "desc": d.get("description", "")})
    return stds


def _sqlite_emit_coverage_request(args, subjects: list[dict], standards: list[dict]):
    if not subjects:
        print("  No subjects found — nothing to review.")
        return
    batch_size = getattr(args, "batch_size", 1) or 1
    output_path = getattr(args, "output_request", "") or ""
    groups = [subjects[i:i + batch_size] for i in range(0, len(subjects), batch_size)]

    response_format = {
        "subjects": [{
            "subjectId": "<US-xxx or UC-xxx>", "subjectType": "<SysUserStory|SysUseCase>",
            "overall": "PASS|WARN|FAIL",
            "dimensions": [{"id": "<AS-REQ-NNN>", "status": "PASS|WARN|FAIL",
                            "finding": "<specific sentence>",
                            "recommendation": "<concrete action if WARN/FAIL, else empty>"}],
        }],
    }
    std_block = "\n\n".join(
        f"### {s['id']} — {s['title']}\n{s['desc']}" for s in standards)

    for idx, group in enumerate(groups):
        prompt_lines = [
            "You are reviewing requirements adequacy for user stories and use cases.",
            "",
            "Return STRICT JSON in this format:",
            json.dumps(response_format, indent=2),
            "",
            "Rules:",
            "- PASS: fully satisfied",
            "- WARN: partially satisfied — present but incomplete or ambiguous",
            "- FAIL: clearly absent or clearly wrong",
            "- finding must be specific",
            "- recommendation must be concrete when status is WARN or FAIL",
            "- Return raw JSON only, no markdown fences",
            "",
            "## Architecture standards",
            std_block,
            "",
            "## Subjects to review",
        ]
        for sub in group:
            prompt_lines.append(
                f"\n### {sub['subjectId']} [{sub['subjectType']}] {sub.get('title','')}")
            prompt_lines.append(sub["context"])

        request = {
            "schemaVersion": 2, "command": "coverage-review",
            "instruction": "Review each subject against the AS-REQ standards and return the JSON result.",
            "responseFormat": response_format,
            "standards": standards, "subjects": group,
            "prompt": "\n".join(prompt_lines),
        }

        if output_path:
            p = Path(output_path)
            if len(groups) > 1:
                p = p.with_name(f"{p.stem}.{idx + 1}{p.suffix}")
            p.write_text(json.dumps(request, indent=2), encoding="utf-8")
            print(f"  Coverage review request written to: {p}")
        else:
            print("\n" + "=" * 62)
            print("  COVERAGE REVIEW REQUEST — paste into Claude Code session")
            print("=" * 62)
            print(request["prompt"])
            print("\n" + "=" * 62)
            print("  MACHINE-READABLE REQUEST JSON (save output and use --apply-results)")
            print("=" * 62)
            print(json.dumps(request, indent=2))

    print("\n  After the session responds, save the result JSON and run:")
    print("    python3 scripts/sys_edge.py coverage-review --apply-results <result.json>")


def cmd_coverage_review(args, root, S):
    apply_results = getattr(args, "apply_results", "") or ""
    if apply_results:
        p = Path(apply_results)
        if not p.exists():
            raise CommandError(f"result file not found: {apply_results}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise CommandError(f"could not parse {apply_results}: {e}")

        subjects = data.get("subjects", []) if isinstance(data, dict) else data
        if not subjects:
            print("  No subjects found in result file.")
            return

        W = 72
        any_fail = False
        stds = {s["id"]: s["title"] for s in _sqlite_fetch_as_req_standards(S)}
        for sub in subjects:
            subject_id = sub.get("subjectId")
            if not subject_id:
                continue
            dims = sub.get("dimensions", [])
            title = sub.get("title", "")
            pass_count = warn_count = fail_count = 0
            recs = []

            print(f"\n{'═' * W}")
            print(f"  coverage-review: {subject_id}")
            if title:
                print(f"  {title[:68]}")
            print(f"{'═' * W}\n")

            for d in dims:
                sid = d.get("id", "")
                status = d.get("status", "FAIL").upper()
                finding = d.get("finding", "")
                rec = d.get("recommendation", "")
                std_title = stds.get(sid, sid)
                print(f"  [{status:<4}]  {sid}  {std_title}")
                if finding:
                    print(f"          {finding[:110]}")
                if rec:
                    print(f"          → {rec[:108]}")
                print()
                if status == "PASS":
                    pass_count += 1
                elif status == "WARN":
                    warn_count += 1
                    if rec:
                        recs.append(f"[{sid}] {rec}")
                else:
                    fail_count += 1
                    if rec:
                        recs.append(f"[{sid}] {rec}")

            if recs:
                print(f"{'─' * W}")
                print("  Recommendations")
                print(f"{'─' * W}")
                for i, r in enumerate(recs, 1):
                    print(f"  {i}. {r}")
                print()

            overall = ("ALL PASS" if fail_count == 0 and warn_count == 0
                       else ("NEEDS WORK" if fail_count > 0 else "REVIEW WARNINGS"))
            print(f"{'═' * W}")
            print(f"  {fail_count} FAIL  ·  {warn_count} WARN  ·  {pass_count} PASS   "
                  f"(overall: {overall})")
            print(f"{'═' * W}\n")

            result_label = ("PASS" if fail_count == 0 and warn_count == 0
                            else ("FAIL" if fail_count > 0 else "WARN"))
            subject_type = sub.get("subjectType", "")
            label = "SysUseCase" if subject_type == "SysUseCase" else "SysUserStory"
            with S.write():
                S.update(label, subject_id, {
                    "coverageReviewLastRun": _now_iso(),
                    "coverageReviewResult": result_label,
                })
            if fail_count > 0:
                any_fail = True

        if any_fail:
            sys.exit(1)
        return

    # ── Emit mode ─────────────────────────────────────────────────────────────
    us_id = getattr(args, "us",       "") or ""
    uc_id = getattr(args, "uc",       "") or ""
    inst  = getattr(args, "instance", "") or ""

    standards = _sqlite_fetch_as_req_standards(S)
    if not standards:
        print("⚠  No AS-REQ standards found — run seed-standards first", file=sys.stderr)

    subjects: list[dict] = []
    if us_id or uc_id:
        sub = _sqlite_build_coverage_subject(S, us_id, uc_id)
        if sub:
            subjects.append(sub)
    elif inst:
        uc_rows = S.q(
            "SELECT id, props FROM nodes WHERE label='SysUseCase' "
            "AND json_extract(props,'$.instance')=?", (inst,))
        story_ids_seen: set[str] = set()
        for r in uc_rows:
            for (sl, sid) in S.in_ids("SysUseCase", r[0], "REALIZED_BY"):
                if sl == "SysUserStory" and sid not in story_ids_seen:
                    story_ids_seen.add(sid)
                    sub = _sqlite_build_coverage_subject(S, sid, "")
                    if sub:
                        subjects.append(sub)
    else:
        for row in S.q("SELECT id FROM nodes WHERE label='SysUserStory' ORDER BY id"):
            sub = _sqlite_build_coverage_subject(S, row[0], "")
            if sub:
                subjects.append(sub)

    _sqlite_emit_coverage_request(args, subjects, standards)


def _sqlite_auto_check(S, eid: str, label: str, std_id: str, node: dict) -> str:
    """Run an automated graph check for one AS-REQ standard. Returns PASS/WARN/FAIL."""
    if std_id == "AS-REQ-001":
        if label != "SysUserStory":
            return "PASS"
        return "PASS" if (node.get("goal") and node.get("actor")) else "FAIL"
    if std_id == "AS-REQ-002":
        if label != "SysUserStory":
            return "PASS"
        return "PASS" if S.out_ids("SysUserStory", eid, "REALIZED_BY") else "FAIL"
    if std_id == "AS-REQ-003":
        if label != "SysUseCase":
            return "PASS"
        return "PASS" if S.out_ids("SysUseCase", eid, "REQUIRES") else "FAIL"
    if std_id == "AS-REQ-004":
        if label not in ("SysUseCase", "SysFeature"):
            return "PASS"
        feat_ids = [eid] if label == "SysFeature" else [
            fid for (fl, fid) in S.out_ids("SysUseCase", eid, "REQUIRES")]
        if not feat_ids:
            return "WARN"
        untested = [fid for fid in feat_ids
                    if not S.in_ids("SysFeature", fid, "VERIFIES")]
        if not untested:
            return "PASS"
        return "WARN" if len(untested) < len(feat_ids) else "FAIL"
    if std_id == "AS-REQ-005":
        if label != "SysUseCase":
            return "PASS"
        return "PASS" if node.get("preconditions") else "WARN"
    if std_id == "AS-REQ-006":
        if label != "SysUseCase":
            return "PASS"
        return "PASS" if node.get("mainFlow") else "WARN"
    if std_id == "AS-REQ-007":
        if label != "SysUseCase":
            return "PASS"
        return "PASS" if node.get("failureScenarios") else "WARN"
    return "PASS"


def cmd_quality_review(args, root, S):
    entity_id   = getattr(args, "entity",   "") or ""
    instance    = getattr(args, "instance", "") or ""
    type_filter = getattr(args, "type",     "") or ""

    if not entity_id and not instance:
        raise CommandError("ERROR: provide --entity or --instance")

    stds_rows = S.q("SELECT id, props FROM nodes WHERE label='SysArchStd' ORDER BY id")
    stds = []
    for r in stds_rows:
        d = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        stds.append({"id": r[0], **d})

    entities: list[tuple[str, str]] = []
    if entity_id:
        for lbl in ("SysUseCase", "SysUserStory", "SysFeature", "SysEnhancement"):
            if S.node(lbl, entity_id):
                entities.append((lbl, entity_id))
                break
        if not entities:
            raise CommandError(f"Entity {entity_id} not found")
    else:
        labels = [type_filter] if type_filter else ["SysUseCase", "SysUserStory"]
        for lbl in labels:
            for row in S.q("SELECT id, props FROM nodes WHERE label=?", (lbl,)):
                d = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                if instance and d.get("instance") != instance:
                    continue
                entities.append((lbl, row[0]))

    W = 72
    any_fail = False
    for (label, eid) in entities:
        node = S.node(label, eid) or {}
        applicable = [s for s in stds
                      if label in (s.get("appliesToTypes") or [label])]
        if not applicable:
            continue

        print(f"\n{'═' * W}")
        print(f"  quality-review: {eid}  [{label}]")
        if node.get("title"):
            print(f"  {node['title'][:68]}")
        print(f"{'═' * W}\n")

        pass_count = warn_count = fail_count = manual_count = 0
        for std in applicable:
            method  = std.get("evaluationMethod", "manual")
            enforce = std.get("enforcement", "mandatory")
            sid     = std["id"]
            title   = std.get("title", "")

            if method == "automated":
                status = _sqlite_auto_check(S, eid, label, sid, node)
                mark = "✓" if status == "PASS" else ("~" if status == "WARN" else "✗")
                print(f"  [{mark}] {sid}  {title}  [automated]")
                if status == "FAIL":
                    fail_count += 1
                    if enforce == "mandatory":
                        any_fail = True
                elif status == "WARN":
                    warn_count += 1
                else:
                    pass_count += 1
            else:
                manual_count += 1
                print(f"  [?] {sid}  {title}  [{method}]")

        print()
        print(f"  automated: {pass_count}✓ {warn_count}~ {fail_count}✗   "
              f"manual/ai: {manual_count} items")
        if manual_count:
            print("  → Run coverage-review to evaluate manual/ai standards in the session")
        print(f"{'═' * W}\n")

    if any_fail:
        sys.exit(1)


def cmd_audit_status(args, root, S):
    instance = getattr(args, "instance", "") or ""
    days     = getattr(args, "days",     14) or 14
    scope    = getattr(args, "scope",    "both") or "both"

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    cutoff = (_dt.now(_tz.utc) - _td(days=days)).isoformat()

    W = 72
    print(f"\n{'═' * W}")
    print(f"  AUDIT STATUS  (flag if older than {days}d)")
    print(f"{'═' * W}\n")

    def _show(label: str, scope_name: str):
        rows = S.q("SELECT id, props FROM nodes WHERE label=? ORDER BY id", (label,))
        items = []
        for r in rows:
            d = json.loads(r[1]) if isinstance(r[1], str) else r[1]
            if instance and d.get("instance") != instance:
                continue
            items.append((r[0], d))
        if not items:
            return
        print(f"  {scope_name}s ({len(items)})")
        for (eid, d) in items:
            cr = d.get("coverageReviewLastRun", "")
            at = d.get("auditTestLastRun", "")
            cr_flag = "⚠" if not cr or cr < cutoff else "✓"
            at_flag = "⚠" if not at or at < cutoff else "✓"
            cr_str  = (cr or "never")[:10]
            at_str  = (at or "never")[:10]
            title   = d.get("title", "")[:40]
            print(f"    {eid:<18} {cr_flag} cov:{cr_str:<12} {at_flag} audit:{at_str}  {title}")
        print()

    if scope in ("us", "both"):
        _show("SysUserStory", "User Story")
    if scope in ("uc", "both"):
        _show("SysUseCase", "Use Case")


# ── Phase 5 — audit-test ─────────────────────────────────────────────────────

def _sqlite_fetch_audit_test_standards(S, tier_prefix: str) -> list[dict]:
    rows = S.q("SELECT id, props FROM nodes WHERE label='SysArchStd' AND id LIKE ? ORDER BY id",
               (f"{tier_prefix}%",))
    return [{"id": r[0], **(json.loads(r[1]) if isinstance(r[1], str) else r[1])} for r in rows]


def _sqlite_build_audit_subject(S, uc_id: str, feat_id: str, us_id: str,
                                 tier: str, file_arg: str, root: Path) -> "tuple[dict | None, str]":
    """Gather context + test file content for audit-test. Returns (subject, error_msg)."""
    tier_label = {
        "usecase": "UC / UI Flow", "integration": "Component / Integration",
        "e2e": "E2E / User Story",
    }.get(tier, tier)
    subject_id = uc_id or feat_id or us_id
    context_lines: list[str] = []
    feature_ids: list[str] = []
    subject_type = ""

    if uc_id:
        subject_type = "SysUseCase"
        uc = S.node("SysUseCase", uc_id)
        if not uc:
            return None, f"ERROR: UC {uc_id} not found"
        feat_pairs = S.out_ids("SysUseCase", uc_id, "REQUIRES")
        feature_ids = [fid for (fl, fid) in feat_pairs if fl == "SysFeature"]
        story_pairs = S.in_ids("SysUseCase", uc_id, "REALIZED_BY")
        context_lines = [
            f"Use Case: {uc.get('title', '')} ({uc_id})",
            f"Instance: {uc.get('instance') or 'unknown'}",
            f"Parent stories: {', '.join(sid for (_, sid) in story_pairs) or '(none)'}",
        ]
        for field in ("preconditions", "authorizedRoles", "failureScenarios", "mainFlow", "description"):
            if uc.get(field):
                context_lines.append(f"{field}: {uc[field]}")
    elif feat_id:
        subject_type = "SysFeature"
        feat = S.node("SysFeature", feat_id)
        if not feat:
            return None, f"ERROR: Feature {feat_id} not found"
        feature_ids = [feat_id]
        context_lines = [f"Feature: {feat.get('name', '')} ({feat_id})"]
        if feat.get("description"):
            context_lines.append(f"Description: {feat['description']}")
    elif us_id:
        subject_type = "SysUserStory"
        us = S.node("SysUserStory", us_id)
        if not us:
            return None, f"ERROR: User story {us_id} not found"
        feat_pairs = S.out_ids("SysUserStory", us_id, "REQUIRES")
        feature_ids = [fid for (fl, fid) in feat_pairs if fl == "SysFeature"]
        uc_pairs = S.out_ids("SysUserStory", us_id, "REALIZED_BY")
        context_lines = [
            f"User Story: {us.get('title', '')} ({us_id})",
            f"Actor: {us.get('actor') or 'User'}",
            f"Goal: {us.get('goal') or ''}",
        ]
        for field in ("benefit", "acceptanceCriteria", "outOfScope"):
            if us.get(field):
                context_lines.append(f"{field}: {us[field]}")
        if uc_pairs:
            context_lines.append(f"Realising use cases: {', '.join(uid for (_, uid) in uc_pairs)}")

    # Discover test file
    test_file_path = file_arg
    if not test_file_path and feature_ids:
        tier_cats = {
            "usecase": ["usecase", "ui", "ui_flow"],
            "integration": ["integration", "api", "functional"],
            "e2e": ["e2e", "end_to_end"],
        }.get(tier, [])
        seen_files: dict[str, None] = {}
        for fid in feature_ids:
            for (tl, tid) in S.in_ids("SysFeature", fid, "VERIFIES"):
                if tl == "SysTest" and "::" in tid:
                    fp = tid.split("::")[0]
                    t_node = S.node("SysTest", tid) or {}
                    cat = t_node.get("testType", "")
                    if not tier_cats or cat in tier_cats:
                        seen_files[fp] = None
                    else:
                        seen_files.setdefault(fp, None)
        if not seen_files:
            return None, f"No tests registered for {subject_id} — run scan-tests first."
        subj_norm = subject_id.lower().replace("-", "").replace("_", "")
        preferred = [fp for fp in seen_files
                     if subj_norm in fp.lower().replace("_", "").replace("-", "")]
        test_file_path = preferred[0] if preferred else next(iter(seen_files))

    if not test_file_path:
        return None, f"No tests registered for {subject_id} — run scan-tests first."

    p = Path(test_file_path) if Path(test_file_path).is_absolute() else root / test_file_path
    if not p.exists():
        return None, f"ERROR: Test file not found: {p}\n  Use --file <path> to specify directly."

    test_content = p.read_text(encoding="utf-8")
    TRUNCATE = 8000
    if len(test_content) > TRUNCATE and p.suffix == ".py":
        try:
            tree = ast.parse(test_content)
            lines = [f"# Summary of {p.name} ({len(test_content):,} chars)\n"]
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    lines.append(f"\nclass {node.name}:")
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                            lines.append(f"    def {item.name}(self, ...):  # line {item.lineno}")
            test_excerpt = "\n".join(lines)
        except Exception:
            test_excerpt = test_content[:TRUNCATE] + "\n... [truncated]"
    else:
        test_excerpt = test_content[:TRUNCATE] if len(test_content) > TRUNCATE else test_content

    return {
        "subjectId": subject_id, "subjectType": subject_type, "tier": tier,
        "tierLabel": tier_label, "context": "\n".join(context_lines),
        "testFilePath": str(p), "testFileName": p.name,
        "testExcerpt": test_excerpt, "featureIds": feature_ids,
    }, ""


def cmd_audit_test(args, root, S):
    uc_id         = getattr(args, "uc",             "") or ""
    feat_id       = getattr(args, "feature",        "") or ""
    us_id         = getattr(args, "us",             "") or ""
    file_arg      = getattr(args, "file",           "") or ""
    tier_arg      = getattr(args, "tier",           "") or ""
    apply_results = getattr(args, "apply_results",  "") or ""
    output_path   = getattr(args, "output_request", "") or ""
    preflight     = getattr(args, "preflight", False)

    if apply_results:
        p = Path(apply_results)
        if not p.exists():
            raise CommandError(f"result file not found: {apply_results}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise CommandError(f"could not parse {apply_results}: {e}")
        subjects = data.get("subjects", []) if isinstance(data, dict) else data
        W = 72
        any_fail = False
        for sub in subjects:
            sid = sub.get("subjectId")
            if not sid:
                continue
            dims = sub.get("dimensions", [])
            tier = sub.get("tier", "")
            tier_prefix = {"usecase": "AS-TEST-UC-", "integration": "AS-TEST-COMP-",
                           "e2e": "AS-TEST-US-"}.get(tier, "AS-TEST-UC-")
            stds = {s["id"]: s.get("title", "")
                    for s in _sqlite_fetch_audit_test_standards(S, tier_prefix)}
            pass_c = warn_c = fail_c = 0
            print(f"\n{'═' * W}\n  audit-test: {sid}  [{sub.get('tierLabel', tier)}]\n{'═' * W}\n")
            for d in dims:
                did    = d.get("id", "")
                status = d.get("status", "FAIL").upper()
                finding = d.get("finding", "")
                action  = d.get("action", "")
                std_title = stds.get(did, did)
                print(f"  [{status:<4}]  {did}  {std_title}")
                if finding:
                    print(f"          {finding[:110]}")
                if action:
                    print(f"          → {action[:108]}")
                print()
                if status == "PASS":
                    pass_c += 1
                elif status == "WARN":
                    warn_c += 1
                else:
                    fail_c += 1
            overall = ("ALL PASS" if fail_c == 0 and warn_c == 0
                       else ("NEEDS WORK" if fail_c > 0 else "REVIEW WARNINGS"))
            print(f"{'═' * W}")
            print(f"  {fail_c} FAIL  ·  {warn_c} WARN  ·  {pass_c} PASS   (overall: {overall})")
            print(f"{'═' * W}\n")
            lbl = {"SysUseCase": "SysUseCase", "SysFeature": "SysFeature",
                   "SysUserStory": "SysUserStory"}.get(sub.get("subjectType", ""), "SysUseCase")
            result_label = ("PASS" if fail_c == 0 and warn_c == 0
                            else ("FAIL" if fail_c > 0 else "WARN"))
            with S.write():
                S.update(lbl, sid, {"auditTestLastRun": _now_iso(),
                                     "auditTestResult": result_label})
            if fail_c > 0:
                any_fail = True
        if any_fail:
            sys.exit(1)
        return

    if not (uc_id or feat_id or us_id):
        raise CommandError("ERROR: provide --uc, --feature, or --us")

    tier = tier_arg or ("usecase" if uc_id else "integration" if feat_id else "e2e")
    tier_prefix = {"usecase": "AS-TEST-UC-", "integration": "AS-TEST-COMP-",
                   "e2e": "AS-TEST-US-"}.get(tier, "AS-TEST-UC-")

    stds = _sqlite_fetch_audit_test_standards(S, tier_prefix)
    if not stds:
        raise CommandError(f"No SysArchStd nodes with prefix {tier_prefix} — run seed-standards first")

    subject, err = _sqlite_build_audit_subject(S, uc_id, feat_id, us_id, tier, file_arg, root)
    if err:
        raise CommandError(err)

    if preflight:
        p = Path(subject["testFilePath"])
        print(f"preflight: {subject['subjectId']} → {p}  ({'exists' if p.exists() else 'NOT FOUND'})")
        return

    response_format = {
        "subjects": [{"subjectId": "<id>", "subjectType": "<type>", "tier": "<tier>",
                      "overall": "PASS|WARN|FAIL",
                      "dimensions": [{"id": "<AS-TEST-...>", "status": "PASS|WARN|FAIL",
                                      "finding": "<specific>",
                                      "action": "<concrete if WARN/FAIL>"}]}]}
    std_block = "\n\n".join(
        f"### {s['id']} — {s.get('title','')}\n{s.get('description','')}"
        for s in stds
    )
    prompt_lines = [
        "You are evaluating a test file against architecture standards.",
        "", "Return STRICT JSON in this format:", json.dumps(response_format, indent=2), "",
        "Rules:",
        "- PASS: test file adequately satisfies this dimension",
        "- WARN: partially satisfied — something present but incomplete",
        "- FAIL: clearly absent or clearly incorrect",
        "- finding must reference actual test names, lines, or patterns seen or absent",
        "- action must be a concrete implementation instruction when WARN or FAIL",
        "- Return raw JSON only, no markdown fences", "",
        "## Architecture standards", std_block, "", "## Subject",
        f"\n### {subject['subjectId']} [{subject['subjectType']}] — {subject['tierLabel']}",
        subject["context"],
        f"\nTest file: {subject['testFilePath']}", "\n```python", subject["testExcerpt"], "```",
    ]
    request = {
        "schemaVersion": 2, "command": "audit-test",
        "standards": [{"id": s["id"], "title": s.get("title", ""),
                        "description": s.get("description", "")}
                       for s in stds],
        "subjects": [subject], "prompt": "\n".join(prompt_lines),
    }
    if output_path:
        Path(output_path).write_text(json.dumps(request, indent=2), encoding="utf-8")
        print(f"  Audit request written to: {output_path}")
    else:
        print("\n" + "=" * 62 + "\n  AUDIT TEST REQUEST — paste into Claude Code session\n" + "=" * 62)
        print(request["prompt"])
        print("\n" + "=" * 62 + "\n  MACHINE-READABLE REQUEST JSON\n" + "=" * 62)
        print(json.dumps(request, indent=2))
    print("\n  After the session responds, save result JSON and run:")
    print("    python3 scripts/sys_edge.py audit-test --apply-results <result.json>")


# ── Phase 5 — session-close ───────────────────────────────────────────────────

def cmd_session_close(args, root, S):
    instance  = getattr(args, "instance",  "") or ""
    no_backup = getattr(args, "no_backup", False)

    print("\n" + "═" * 62)
    print(f"  SESSION CLOSE — {instance or 'all instances'}")
    print("═" * 62 + "\n")

    def _count(label, closed_statuses):
        total = 0
        for r in S.q("SELECT props FROM nodes WHERE label=?", (label,)):
            d = json.loads(r[0]) if isinstance(r[0], str) else r[0]
            if instance and d.get("instance") != instance:
                continue
            if d.get("status") not in closed_statuses:
                total += 1
        return total

    open_enhs = _count("SysEnhancement", {"done", "wontfix"})
    open_defs = _count("SysDefect", {"closed"})
    print(f"  open work: {open_enhs} enhancements, {open_defs} defects")

    if not no_backup:
        from datetime import datetime as _dt
        import argparse as _ap
        ts = _dt.now().strftime("%Y-%m-%dT%H-%M-%S")
        bk_args = _ap.Namespace(output=f"data/sys-backup-{ts}.json", allow_empty=False)
        try:
            cmd_backup(bk_args, root, S)
        except (CommandError, SystemExit):
            _err("  ⚠  backup skipped (run backup manually)")

    print("\n  ✓ Session closed\n")


# ── Phase 5 — config ──────────────────────────────────────────────────────────

def cmd_config(args, root, S):
    import re as _re
    yaml_path = root / "sysedge.yaml"

    if args.action == "get":
        cfg: dict = {}
        if yaml_path.exists():
            for line in yaml_path.read_text(encoding="utf-8").splitlines():
                m = _re.match(r'^(\w[\w-]*):\s*(.+)$', line.strip())
                if m:
                    cfg[m.group(1)] = m.group(2).strip()
        keys = [args.key] if getattr(args, "key", "") else sorted(cfg) or ["backend", "model"]
        for k in keys:
            print(f"{k}: {cfg.get(k, '')}")
        return

    key   = getattr(args, "key",   "") or ""
    value = getattr(args, "value", "") or ""
    if not key or not value:
        raise CommandError("ERROR: config set requires key and value")
    if yaml_path.exists():
        text = yaml_path.read_text(encoding="utf-8")
        if _re.search(rf'^{key}:', text, _re.MULTILINE):
            text = _re.sub(rf'^{key}:.*$', f'{key}: {value}', text, flags=_re.MULTILINE)
        else:
            text = text.rstrip() + f"\n{key}: {value}\n"
    else:
        text = f"{key}: {value}\n"
    yaml_path.write_text(text, encoding="utf-8")
    print(f"✓ {key}: {value}  ({yaml_path})")


# ── Phase 5 — set-external-ref ────────────────────────────────────────────────

_EXTERNAL_SYSTEM_RE = re.compile(r'^[a-z][a-z0-9_-]{0,19}$')
_EXTERNAL_REF_LABELS = {
    "SysUserStory", "SysUseCase", "SysFeature", "SysDefect", "SysEnhancement",
}


def cmd_set_external_ref(args, root, S):
    system = args.system.lower()
    if not _EXTERNAL_SYSTEM_RE.match(system):
        raise CommandError("ERROR: --system must be a short lowercase name, e.g. jira, ado, polarion")
    ref    = f"{system}:{args.ref}"
    remove = getattr(args, "remove", False)

    node = None
    found_label = None
    for lbl in _EXTERNAL_REF_LABELS:
        n = S.node(lbl, args.id)
        if n:
            node = n
            found_label = lbl
            break
    if not node:
        raise CommandError(f"⚠ {args.id} not found (story, use case, feature, defect or enhancement)")

    refs = list(node.get("externalRefs") or [])
    if remove:
        refs = [r for r in refs if r != ref]
    elif ref not in refs:
        refs.append(ref)

    with S.write():
        S.update(found_label, args.id, {"externalRefs": refs})
    print(f"✓ {args.id} external refs: {', '.join(refs) or '(none)'}")


# ── Phase 5 — baseline ────────────────────────────────────────────────────────

_BASELINE_VOLATILE = {"lastTestedAt", "scannedAt", "coverageReviewLastRun",
                       "auditTestLastRun", "coverageReviewCacheKey"}


def _sqlite_baseline_snapshot(S, instance: str, module: str) -> dict:
    snap: dict = {"stories": {}, "usecases": {}, "features": {}, "tests": {}}

    def _clean(d):
        return {k: v for k, v in d.items() if k not in _BASELINE_VOLATILE}

    for (label, key) in (("SysUserStory", "stories"), ("SysUseCase", "usecases"),
                          ("SysFeature", "features"), ("SysTest", "tests")):
        for row in S.q("SELECT id, props FROM nodes WHERE label=?", (label,)):
            d = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            if instance and label == "SysUseCase" and d.get("instance") != instance:
                continue
            snap[key][row[0]] = _clean(d)
    return snap


def _sqlite_baseline_diff(a: dict, b: dict) -> dict:
    out: dict = {}
    for cat in ("stories", "usecases", "features", "tests"):
        x, y = a.get(cat, {}), b.get(cat, {})
        changed: dict = {}
        for k in sorted(set(x) & set(y)):
            fields = sorted(
                f for f in set(x[k]) | set(y[k])
                if f not in _BASELINE_VOLATILE
                and json.dumps(x[k].get(f), sort_keys=True, default=str) !=
                   json.dumps(y[k].get(f), sort_keys=True, default=str)
            )
            if fields:
                changed[k] = fields
        out[cat] = {"added": sorted(set(y) - set(x)),
                    "removed": sorted(set(x) - set(y)),
                    "changed": changed}
    return out


def cmd_baseline(args, root, S):
    import hashlib as _hl
    baseline_dir = root / "data" / "baselines"

    def _bpath(name: str) -> Path:
        return baseline_dir / f"{name}.json"

    def _load(name: str) -> dict:
        p = _bpath(name)
        if not p.exists():
            raise CommandError(f"Baseline '{name}' not found ({p})")
        text = p.read_text(encoding="utf-8")
        stored = S.node("SysBaseline", name)
        if stored and stored.get("sha256"):
            digest = _hl.sha256(text.encode("utf-8")).hexdigest()
            if stored["sha256"] != digest:
                raise CommandError(
                    f"✗ Baseline '{name}' file does not match recorded sha256 — it was modified")
        return json.loads(text)

    if args.action == "list":
        rows = S.q("SELECT id, props FROM nodes WHERE label='SysBaseline' ORDER BY id")
        if not rows:
            print("  No baselines yet — create one with: baseline create --name R1.0")
            return
        for r in rows:
            d = json.loads(r[1]) if isinstance(r[1], str) else r[1]
            print(f"  {r[0]:<20} {d.get('createdAt','')[:19]}  "
                  f"{d.get('scope','all'):<10} {d.get('counts','')}  "
                  f"sha256:{d.get('sha256','')[:12]}")
        return

    if args.action == "create":
        name = getattr(args, "name", "") or ""
        if not name or not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$', name):
            raise CommandError("ERROR: --name required (letters, digits, . _ -; max 64)")
        p = _bpath(name)
        if p.exists():
            raise CommandError(f"Baseline '{name}' already exists — baselines are immutable")
        snap = _sqlite_baseline_snapshot(
            S,
            getattr(args, "instance", "") or "",
            getattr(args, "module", "") or "",
        )
        body = {
            "baseline": name, "createdAt": _now_iso(),
            "scope": (getattr(args, "module", "") or getattr(args, "instance", "") or "all"),
            "note": getattr(args, "note", "") or "", "snapshot": snap,
        }
        text   = json.dumps(body, indent=2, sort_keys=True, default=str)
        digest = _hl.sha256(text.encode("utf-8")).hexdigest()
        counts = " ".join(f"{k}={len(v)}" for k, v in snap.items())
        baseline_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        with S.write():
            S.upsert("SysBaseline", name, {
                "createdAt": body["createdAt"], "scope": body["scope"],
                "sha256": digest, "path": str(p),
                "counts": counts, "note": body["note"],
            })
        print(f"✓ Baseline {name}  ({counts})\n  {p}  sha256:{digest}")
        return

    if args.action == "show":
        b = _load(getattr(args, "name", "") or "")
        print(f"  {b['baseline']}  {b['createdAt'][:19]}  scope={b.get('scope','all')}")
        if b.get("note"):
            print(f"  Note: {b['note']}")
        for k, v in b["snapshot"].items():
            print(f"    {k:<10} {len(v)}")
        return

    if args.action == "diff":
        name    = getattr(args, "name",    "") or ""
        against = getattr(args, "against", "") or ""
        a = _load(name)["snapshot"]
        if against:
            b, against_label = _load(against)["snapshot"], against
        else:
            b = _sqlite_baseline_snapshot(
                S,
                getattr(args, "instance", "") or "",
                getattr(args, "module", "") or "",
            )
            against_label = "current graph"
        diff = _sqlite_baseline_diff(a, b)
        if getattr(args, "json", False):
            print(json.dumps(diff, indent=2))
            return
        print(f"\n  Baseline {name} → {against_label}\n")
        total = 0
        for cat, d in diff.items():
            n = len(d["added"]) + len(d["removed"]) + len(d["changed"])
            total += n
            if not n:
                continue
            print(f"  {cat}: +{len(d['added'])} -{len(d['removed'])} ~{len(d['changed'])}")
            for k in d["added"][:10]:
                print(f"    + {k}")
            for k in d["removed"][:10]:
                print(f"    - {k}")
            for k, fields in list(d["changed"].items())[:10]:
                print(f"    ~ {k}: {', '.join(fields)}")
        if total == 0:
            print("  No changes.")
        print()


# ── Phase 6 — defect alias, enhancement update, stale-test pruning, endpoint, reset, mcp, viz ──

def cmd_update_enhancement(args, root, S):
    existing = S.node("SysEnhancement", args.id)
    if not existing:
        _err(f"⚠ {args.id} not found"); sys.exit(1)
    updates = {}
    _PROTECTED = {"status", "completedAt", "startedAt", "closedAt"}
    for attr, field in [
        ("title", "title"), ("description", "description"),
        ("priority", "priority"), ("source", "source"),
        ("instance", "instance"),
    ]:
        val = getattr(args, attr, "") or ""
        if val:
            updates[field] = val
    for attr, field in [
        ("deploy_checklist", "deployChecklist"),
        ("acceptance_criteria", "acceptanceCriteria"),
    ]:
        val = getattr(args, attr, "") or ""
        if val:
            updates[field] = val
    for p in _PROTECTED:
        updates.pop(p, None)
    if not updates:
        _err("Nothing to update — specify at least one of --title --description "
             "--priority --source --instance --deploy-checklist --acceptance-criteria")
        return
    with S.write():
        S.update("SysEnhancement", args.id, updates)
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_prune_stale_tests(args, root, S):
    feat_prefix = getattr(args, "feature", "") or ""
    dry = getattr(args, "dry_run", False)
    yes = getattr(args, "yes", False)

    stale: list[dict] = []

    # 1. Legacy T- prefix nodes
    test_rows = S.q("SELECT id FROM nodes WHERE label='SysTest' AND id LIKE 'T-%'")
    for r in test_rows:
        tid = r[0]
        feat_links = S.out_ids("SysTest", tid, "VERIFIES")
        for (fl, fid) in feat_links:
            if feat_prefix and not fid.startswith(feat_prefix):
                continue
            stale.append({"tid": tid, "fid": fid, "reason": "legacy T- prefix"})

    # 2. Path-format nodes whose class no longer exists
    all_test_rows = S.q(
        "SELECT id FROM nodes WHERE label='SysTest' AND id NOT LIKE 'T-%'")
    class_cache: dict[str, set | None] = {}
    for r in all_test_rows:
        tid = r[0]
        parts = tid.split("::")
        if len(parts) < 3:
            continue
        file_path, class_name = parts[0], parts[1]
        if not class_name.startswith("Test"):
            continue
        if file_path not in class_cache:
            full = root / file_path
            if not full.exists():
                class_cache[file_path] = None
            else:
                try:
                    classes: set[str] = set()
                    for line in full.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("class "):
                            cls = line.split("class ", 1)[1].split(":")[0].split("(")[0].strip()
                            classes.add(cls)
                    class_cache[file_path] = classes
                except Exception:
                    class_cache[file_path] = None
        existing_classes = class_cache[file_path]
        if existing_classes is None or class_name not in existing_classes:
            feat_links = S.out_ids("SysTest", tid, "VERIFIES")
            for (fl, fid) in feat_links:
                if feat_prefix and not fid.startswith(feat_prefix):
                    continue
                reason = "file deleted" if existing_classes is None else f"class {class_name} removed"
                stale.append({"tid": tid, "fid": fid, "reason": reason})

    if not stale:
        print("  ✓ No stale test links found.")
        return

    prefix = "[DRY RUN] " if (dry or not yes) else ""
    print(f"  {prefix}{len(stale)} stale VERIFIES edge(s):")
    for item in stale[:20]:
        print(f"    {item['tid']}  →  {item['fid']}  [{item['reason']}]")
    if len(stale) > 20:
        print(f"    … and {len(stale) - 20} more")

    if not dry and yes:
        with S.write():
            for item in stale:
                S.q(
                    "DELETE FROM edges WHERE rtype='VERIFIES' "
                    "AND from_label='SysTest' AND from_id=? "
                    "AND to_label='SysFeature' AND to_id=?",
                    item["tid"], item["fid"])
        print(f"\n  ✓ Removed {len(stale)} stale edge(s).")
    elif not dry:
        print(f"\n  Rerun with --yes to delete (or --dry-run to keep dry-run mode explicit).")


def cmd_link_endpoint(args, root, S):
    eid = f"EP-{args.method}-{args.path.replace('/', '-')}"[:80]
    features = [f.strip() for f in (args.feature or "").split(",") if f.strip()]
    if not features:
        raise CommandError("ERROR: --feature is required")
    for fid in features:
        if not S.node("SysFeature", fid):
            raise CommandError(f"ERROR: feature '{fid}' not found")
    with S.write():
        S.upsert("SysEndpoint", eid, {
            "method": args.method, "path": args.path,
            "binary": getattr(args, "binary", "") or "",
            "permission": getattr(args, "permission", "") or "",
        })
        for fid in features:
            S.link("SysEndpoint", eid, "IMPLEMENTS", "SysFeature", fid)
            print(f"✓ {args.method} {args.path}  →  IMPLEMENTS  →  {fid}")


def cmd_reset(args, root, S):
    rows = S.q("SELECT label, COUNT(*) as c FROM nodes GROUP BY label ORDER BY label")
    total = sum(r[1] for r in rows)
    if not total:
        print("  SQLite database has no nodes — nothing to reset.")
        return

    print("\n  WIPE ALL NODES — every session loses its context until re-seeded.\n")
    for r in rows:
        print(f"    {r[0]:<28} {r[1]}")
    print(f"    {'total':<28} {total}\n")

    if not sys.stdin.isatty():
        raise CommandError(
            "reset needs an interactive terminal (stdin is not a TTY). Run it yourself in a shell.")

    confirm = input("  Type 'wipe' to confirm: ").strip()
    if confirm != "wipe":
        print("  Cancelled."); return

    with S.write():
        S.q("DELETE FROM edges")
        S.q("DELETE FROM nodes")
    print(f"\n  ✓ Deleted {total} nodes and all edges.")


def cmd_mcp(args, root, S):
    import importlib.util as _ilu
    script = Path(__file__).resolve().parent / "sys_edge_mcp.py"
    if not script.exists():
        raise CommandError("sys_edge_mcp.py not found next to sys_edge_sqlite.py")
    spec = _ilu.spec_from_file_location("sys_edge_mcp", script)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.serve()


def cmd_register_mcp(args, root, S):
    target = root / ".mcp.json"
    try:
        cfg = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except ValueError:
        raise CommandError(f"{target} is not valid JSON — fix or remove it first")
    script = Path(__file__).resolve().parent / "sys_edge_mcp.py"
    try:
        script_arg = str(script.relative_to(root.resolve()))
    except ValueError:
        script_arg = str(script)
    entry = {"command": "python3", "args": [script_arg.replace("\\", "/")]}
    cfg.setdefault("mcpServers", {})["sysedge"] = entry
    target.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"✓ Registered MCP server 'sysedge' in {target}")
    print("  Other clients: add the same mcpServers entry to their MCP configuration.")


def cmd_open_viz(args, root, S):
    candidates = [
        Path(__file__).resolve().parent / "sys_edge_viz.py",
        root / "scripts" / "sys_edge_viz.py",
    ]
    viz = next((p for p in candidates if p.exists()), None)
    port = getattr(args, "port", 8888) or 8888
    if not viz:
        print("⚠  sys_edge_viz.py not found (visualiser not included in this build)", file=sys.stderr)
        for p in candidates:
            print(f"   Expected: {p}", file=sys.stderr)
        sys.exit(1)
    print(f"\n  SysEdge Visualiser")
    print(f"  ──────────────────────────────────────────────")
    print(f"  Script:  {viz}")
    print(f"  Port:    {port}")
    print(f"\n  Launch command:")
    print(f"    python3 {viz} --port {port}\n")
    if getattr(args, "launch", False):
        import subprocess as _sp
        _sp.Popen(["python3", str(viz), "--port", str(port)],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        print(f"  ✓ Started in background on port {port}")


# ── Phase 7 — scenario model ─────────────────────────────────────────────────

def cmd_expire_note(args, root, S):
    """Mark a SysNote as expired (removes it from worklog without deleting)."""
    note = S.node("SysNote", args.id)
    if not note:
        raise CommandError(f"⚠ {args.id} not found")
    with S.write():
        S.update("SysNote", args.id, {"status": "expired", "expiredAt": _now_iso()})
    print(f"✓ {args.id} expired  [{note.get('instance', '')}]")


def cmd_scenarios(args, root, S):
    """Show all scenarios with chapter decomposition status."""
    scenarios = S.nodes("SysScenario")
    if not scenarios:
        print("  No scenarios found.")
        return
    for sc in scenarios:
        sid = sc.get("id", "")
        dur = sc.get("durationS") or 0
        dur_min = f"{int(dur) // 60}m" if dur else "?"
        ai_tag = (f"  {sc['aiCallCount']} AI call{'s' if sc['aiCallCount'] != 1 else ''}"
                  if sc.get("aiCallCount") else "")
        print(f"\n  {sid}  {sc.get('name', '')}  [{sc.get('status', '?')} · {dur_min}{ai_tag}]")
        if sc.get("audience"):
            aud = sc["audience"]
            print(f"    audience: {', '.join(aud) if isinstance(aud, list) else aud}")
        for uid in S.out_ids("SysScenario", sid, "DEMONSTRATES", "SysUserStory"):
            us = S.node("SysUserStory", uid) or {}
            print(f"    demonstrates: {uid}  {us.get('title', '')}")
        for chid in S.out_ids("SysScenario", sid, "HAS_CHAPTER", "SysScenarioChapter"):
            ch = S.node("SysScenarioChapter", chid) or {}
            feat_ids = S.out_ids("SysScenarioChapter", chid, "EXERCISES", "SysFeature")
            linked = f"→ {', '.join(feat_ids)}" if feat_ids else "  (not yet linked)"
            print(f"    Ch {ch.get('number', '?')}  {ch.get('title', ''):<45} {linked}")
    print()
    for tp in S.nodes("SysTrainingProgram"):
        pid = tp.get("id", "")
        print(f"\n  {pid}  {tp.get('name', '')}  [audience: {tp.get('audience', '?')}]")
        for mid in S.out_ids("SysTrainingProgram", pid, "HAS_MODULE", "SysTrainingModule"):
            m = S.node("SysTrainingModule", mid) or {}
            feat_ids = S.out_ids("SysTrainingModule", mid, "TEACHES", "SysFeature")
            linked = f"→ {', '.join(feat_ids)}" if feat_ids else "  (not yet linked)"
            print(f"    {mid:<25} {m.get('title', ''):<45} {linked}")


def cmd_link_chapter(args, root, S):
    """Link a scenario chapter to a feature it exercises."""
    if not S.node("SysScenarioChapter", args.chapter):
        raise CommandError(f"chapter '{args.chapter}' not found. Run: sys_edge.py scenarios")
    with S.write():
        ok = S.link("SysScenarioChapter", args.chapter, "EXERCISES", "SysFeature", args.feature)
    if not ok:
        raise CommandError(f"feature '{args.feature}' not found")
    print(f"✓ {args.chapter}  →  EXERCISES  →  {args.feature}")


def cmd_link_scenario_story(args, root, S):
    """Create a DEMONSTRATES edge from a SysScenario to a SysUserStory."""
    if not S.node("SysScenario", args.scenario):
        raise CommandError(f"scenario '{args.scenario}' not found. Run: sys_edge.py scenarios")
    if not S.node("SysUserStory", args.story):
        raise CommandError(f"user story '{args.story}' not found. Run: sys_edge.py stories")
    with S.write():
        S.link("SysScenario", args.scenario, "DEMONSTRATES", "SysUserStory", args.story)
    print(f"✓ {args.scenario}  →  DEMONSTRATES  →  {args.story}")


def cmd_link_training(args, root, S):
    """Link a training module to a feature it teaches."""
    if not S.node("SysTrainingModule", args.module):
        raise CommandError(f"training module '{args.module}' not found. Run: sys_edge.py scenarios")
    with S.write():
        ok = S.link("SysTrainingModule", args.module, "TEACHES", "SysFeature", args.feature)
    if not ok:
        raise CommandError(f"feature '{args.feature}' not found")
    print(f"✓ {args.module}  →  TEACHES  →  {args.feature}")


def cmd_actor_coverage(args, root, S):
    """Show which actors have stories per feature, and which are missing."""
    instance = getattr(args, "instance", "") or ""
    feature  = getattr(args, "feature",  "") or ""
    if not instance and not feature:
        raise CommandError("ERROR: provide --instance or --feature")

    if feature:
        if not S.node("SysFeature", feature):
            raise CommandError(f"feature '{feature}' not found")
        feat_data = S.node("SysFeature", feature) or {}
        story_ids = S.in_ids("SysFeature", feature, "REQUIRES", "SysUserStory")
        actors_present = set()
        for sid in story_ids:
            us = S.node("SysUserStory", sid) or {}
            if us.get("actor"):
                actors_present.add(us["actor"])
        all_actors: set = set()
        for mid in S.in_ids("SysFeature", feature, "PROVIDES", "SysModule"):
            for fid2 in S.out_ids("SysModule", mid, "PROVIDES", "SysFeature"):
                for sid2 in S.in_ids("SysFeature", fid2, "REQUIRES", "SysUserStory"):
                    us2 = S.node("SysUserStory", sid2) or {}
                    if us2.get("actor"):
                        all_actors.add(us2["actor"])
        feature_rows = [{"fid": feature, "fname": feat_data.get("name", ""),
                         "actors": list(actors_present), "story_count": len(story_ids)}]
        label = feature
    else:
        all_actors: set = set()
        feature_rows = []
        for r in S.q(
            "SELECT id FROM nodes WHERE label='SysModule' AND json_extract(props,'$.instance')=?",
                (instance,)):
            mid = r[0]
            for fid in S.out_ids("SysModule", mid, "PROVIDES", "SysFeature"):
                feat = S.node("SysFeature", fid) or {}
                story_ids = S.in_ids("SysFeature", fid, "REQUIRES", "SysUserStory")
                actors = []
                for sid in story_ids:
                    us = S.node("SysUserStory", sid) or {}
                    if us.get("actor"):
                        actors.append(us["actor"])
                        all_actors.add(us["actor"])
                feature_rows.append({"fid": fid, "fname": feat.get("name", ""),
                                     "actors": list(set(actors)), "story_count": len(story_ids)})
        feature_rows.sort(key=lambda x: x["fid"])
        label = instance

    all_actors_list = sorted(all_actors)
    W = 70
    print(f"\n{'═' * W}")
    print(f"  ACTOR COVERAGE — {label}")
    if all_actors_list:
        print(f"  Reference actors: {', '.join(all_actors_list)}")
    else:
        print(f"  (No actor data — set --actor on stories to enable matrix)")
    print(f"{'═' * W}")

    if not feature_rows:
        print("  No features found.")
        print(f"{'═' * W}")
        return

    if all_actors_list:
        col_w = max(12, max(len(a) for a in all_actors_list) + 2)
        header = f"  {'Feature':<18}" + "".join(f"  {a[:col_w-2]:<{col_w-2}}" for a in all_actors_list) + "  Missing"
        print(header)
        print(f"  {'─'*18}" + f"  {'─'*(col_w-2)}" * len(all_actors_list) + "  ─────────────")
        for r in feature_rows:
            present = set(a for a in r["actors"] if a)
            missing = [a for a in all_actors_list if a not in present]
            row_str = f"  {r['fid']:<18}" + "".join(
                f"  {'✓' if a in present else '·':<{col_w-2}}" for a in all_actors_list)
            row_str += f"  {', '.join(missing) if missing else '—'}"
            print(row_str)
    else:
        for r in feature_rows:
            mark = f"{r['story_count']} stories" if r["story_count"] > 0 else "(no stories)"
            print(f"  {r['fid']:<20}  {mark}")

    no_stories  = [r for r in feature_rows if r["story_count"] == 0]
    no_actor    = [r for r in feature_rows if r["story_count"] > 0 and not any(r["actors"])]
    has_gaps    = [r for r in feature_rows if all_actors_list and r["story_count"] > 0
                   and set(all_actors_list) - set(r["actors"])]
    full_cover  = [r for r in feature_rows if all_actors_list and r["story_count"] > 0
                   and not (set(all_actors_list) - set(r["actors"]))]
    print(f"\n{'─' * W}")
    if no_stories:
        ids = ', '.join(r['fid'] for r in no_stories[:6])
        print(f"  ⚠  {len(no_stories)} feature(s) with no stories: {ids}"
              + ("…" if len(no_stories) > 6 else ""))
    if no_actor:
        print(f"  ⚠  {len(no_actor)} feature(s) have stories but no actor set — run:")
        print(f"     update-story --id US-xxx --actor \"Role\"")
    if all_actors_list and has_gaps:
        print(f"  ⚠  {len(has_gaps)} feature(s) missing actor coverage")
    if all_actors_list and full_cover:
        print(f"  ✓  {len(full_cover)} feature(s) have full actor coverage")
    print(f"{'═' * W}")


# ── graph utilities ───────────────────────────────────────────────────────────

def cmd_merge_nodes(args, root, S):
    """Transfer all edges from --absorb to --keep, then delete --absorb."""
    node_type = args.type
    if node_type not in VALID_LABELS:
        raise CommandError(f"unknown node type '{node_type}'")
    if not S.node(node_type, args.keep):
        raise CommandError(f"keep node '{args.keep}' not found")
    if not S.node(node_type, args.absorb):
        raise CommandError(f"absorb node '{args.absorb}' not found")
    if args.keep == args.absorb:
        raise CommandError("--keep and --absorb must be different")

    strategy = getattr(args, "strategy", "union") or "union"
    if node_type == "SysArchDecision":
        strategy = "prefer-keep"

    with S.write():
        in_rows = S.q(
            "SELECT from_label, from_id, type, props FROM edges "
            "WHERE to_label=? AND to_id=?", (node_type, args.absorb))
        for r in in_rows:
            fl, fid, rtype, ep = r[0], r[1], r[2], json.loads(r[3] or "{}")
            exists = S.q(
                "SELECT 1 FROM edges WHERE from_label=? AND from_id=? AND type=? "
                "AND to_label=? AND to_id=?", (fl, fid, rtype, node_type, args.keep))
            if not exists:
                S.conn.execute(
                    "INSERT OR IGNORE INTO edges(from_label,from_id,type,to_label,to_id,props) "
                    "VALUES(?,?,?,?,?,?)",
                    (fl, fid, rtype, node_type, args.keep, json.dumps(ep)))
            S.conn.execute(
                "DELETE FROM edges WHERE from_label=? AND from_id=? AND type=? "
                "AND to_label=? AND to_id=?", (fl, fid, rtype, node_type, args.absorb))

        out_rows = S.q(
            "SELECT to_label, to_id, type, props FROM edges "
            "WHERE from_label=? AND from_id=?", (node_type, args.absorb))
        for r in out_rows:
            tl, tid, rtype, ep = r[0], r[1], r[2], json.loads(r[3] or "{}")
            exists = S.q(
                "SELECT 1 FROM edges WHERE from_label=? AND from_id=? AND type=? "
                "AND to_label=? AND to_id=?", (node_type, args.keep, rtype, tl, tid))
            if not exists:
                S.conn.execute(
                    "INSERT OR IGNORE INTO edges(from_label,from_id,type,to_label,to_id,props) "
                    "VALUES(?,?,?,?,?,?)",
                    (node_type, args.keep, rtype, tl, tid, json.dumps(ep)))
            S.conn.execute(
                "DELETE FROM edges WHERE from_label=? AND from_id=? AND type=? "
                "AND to_label=? AND to_id=?", (node_type, args.absorb, rtype, tl, tid))

        absorb_props = S.node(node_type, args.absorb) or {}
        if strategy == "union":
            keep_props = S.node(node_type, args.keep) or {}
            merged = {k: v for k, v in absorb_props.items()
                      if k != "id" and k not in keep_props and v is not None}
            if merged:
                S.update(node_type, args.keep, merged)
        elif strategy == "prefer-absorb":
            merged = {k: v for k, v in absorb_props.items() if k != "id" and v is not None}
            if merged:
                S.update(node_type, args.keep, merged)

        S.delete_node(node_type, args.absorb)

    print(f"✓ {args.absorb} merged into {args.keep}  [{node_type}]  strategy={strategy}")


def cmd_migrate_binary(args, root, S):
    """Update the binary property on SysEndpoint and SysSymbol nodes."""
    from_b = args.from_binary
    to_b   = args.to_binary
    fids   = [f.strip() for f in (getattr(args, "features", "") or "").split(",") if f.strip()]

    ep_count = sym_count = 0
    with S.write():
        for lbl in ("SysEndpoint", "SysSymbol"):
            for r in S.q(
                f"SELECT id FROM nodes WHERE label=? AND json_extract(props,'$.binary')=?",
                    (lbl, from_b)):
                nid = r[0]
                if fids:
                    linked = S.out_ids(lbl, nid, "IMPLEMENTS", "SysFeature")
                    if not any(f in fids for f in linked):
                        continue
                S.update(lbl, nid, {"binary": to_b})
                if lbl == "SysEndpoint":
                    ep_count += 1
                else:
                    sym_count += 1

    scope = f"  scope: {len(fids)} feature(s)" if fids else "  scope: all"
    print(f"✓ migrate-binary  {from_b} → {to_b}")
    print(f"  endpoints updated : {ep_count}")
    print(f"  symbols updated   : {sym_count}")
    print(scope)


def cmd_analyse(args, root, S):
    """Analyse the graph for orphans and merge candidates."""
    as_json  = getattr(args, "json", False)
    do_merge = getattr(args, "merge", False)
    do_orphans = getattr(args, "orphans", False) or not do_merge
    W = 62
    result = {"orphans": {}, "merge": []} if as_json else None

    if do_orphans:
        print(f"\n{'─'*W}\n  ORPHANS\n{'─'*W}")
        checks = [
            ("Stories without UC",
             "SELECT id, json_extract(props,'$.title') FROM nodes WHERE label='SysUserStory' "
             "AND id NOT IN (SELECT from_id FROM edges WHERE type='REALIZED_BY' AND from_label='SysUserStory')"),
            ("Use cases without features",
             "SELECT id, json_extract(props,'$.instance') FROM nodes WHERE label='SysUseCase' "
             "AND id NOT IN (SELECT from_id FROM edges WHERE type='REQUIRES' AND from_label='SysUseCase')"),
            ("Features without module",
             "SELECT id, json_extract(props,'$.name') FROM nodes WHERE label='SysFeature' "
             "AND coalesce(json_extract(props,'$.status'),'') != 'Superseded' "
             "AND id NOT IN (SELECT to_id FROM edges WHERE type='PROVIDES' AND to_label='SysFeature')"),
            ("Modules without features",
             "SELECT id, json_extract(props,'$.name') FROM nodes WHERE label='SysModule' "
             "AND id NOT IN (SELECT from_id FROM edges WHERE type='PROVIDES' AND from_label='SysModule')"),
            ("ADRs without addressed standards",
             "SELECT id, json_extract(props,'$.title') FROM nodes WHERE label='SysArchDecision' "
             "AND id NOT IN (SELECT from_id FROM edges WHERE type='ADDRESSES' AND from_label='SysArchDecision')"),
        ]
        total = 0
        for check_label, query in checks:
            rows = S.q(query)
            if result is not None:
                result["orphans"][check_label] = [{"id": r[0], "title": r[1]} for r in rows]
            if rows:
                print(f"\n  {check_label} ({len(rows)}):")
                for r in rows[:8]:
                    print(f"    {r[0]:<20} {(r[1] or '')[:45]}")
                if len(rows) > 8:
                    print(f"    … and {len(rows)-8} more")
                total += len(rows)
            else:
                print(f"  ✓ {check_label}: none")
        print(f"\n  Total orphans: {total}")

    if do_merge:
        threshold = getattr(args, "threshold", 0.80) or 0.80
        print(f"\n{'─'*W}\n  MERGE CANDIDATES  (threshold {threshold})\n{'─'*W}")
        for node_type, name_col in [
            ("SysFeature",   "json_extract(props,'$.name')"),
            ("SysUseCase",   "coalesce(json_extract(props,'$.title'),json_extract(props,'$.name'),'')"),
            ("SysUserStory", "json_extract(props,'$.title')"),
        ]:
            rows = S.q(f"SELECT id, {name_col} FROM nodes WHERE label=? ORDER BY id", (node_type,))
            if len(rows) < 2:
                continue
            print(f"\n  {node_type} ({len(rows)} nodes):")
            for i, a in enumerate(rows):
                for b in rows[i+1:]:
                    at = set((a[1] or "").lower().split())
                    bt = set((b[1] or "").lower().split())
                    if not at or not bt:
                        continue
                    score = len(at & bt) / len(at | bt)
                    if score >= threshold:
                        if result is not None:
                            result["merge"].append({"a": a[0], "b": b[0], "score": score})
                        print(f"  {a[0]:<20}  ←→  {b[0]:<20}  {score:.0%}  {(a[1] or '')[:30]}")

    if as_json:
        print(json.dumps(result, indent=2, default=str))


def cmd_changes(args, root, S):
    """List change-log entries (newest first); --verify checks the hash chain."""
    import hashlib

    if getattr(args, "verify", False):
        rows = S.q(
            "SELECT props FROM nodes WHERE label='SysChange' "
            "ORDER BY json_extract(props,'$.seq')")
        prev, bad = "", []
        for r in rows:
            c = json.loads(r[0])
            stored = c.pop("hash", "")
            seq    = c.get("seq")
            c.pop("id", None)
            calc = hashlib.sha256(
                (prev + json.dumps(c, sort_keys=True, default=str)).encode()).hexdigest()
            if c.get("prevHash", "") != prev or calc != stored:
                bad.append(seq)
            prev = stored
        n = len(rows)
        if bad:
            seqs = ', '.join(map(str, bad[:10])) + (' …' if len(bad) > 10 else '')
            print(f"✗ change log chain broken at seq {seqs} ({n} entries)")
            sys.exit(1)
        print(f"✓ change log intact ({n} entries)")
        return

    limit = getattr(args, "limit", 50) or 50
    eid   = getattr(args, "id", "") or ""
    if eid:
        rows = S.q(
            "SELECT props FROM nodes WHERE label='SysChange' "
            "AND json_extract(props,'$.entityId')=? "
            "ORDER BY json_extract(props,'$.seq') DESC LIMIT ?", (eid, limit))
    else:
        rows = S.q(
            "SELECT props FROM nodes WHERE label='SysChange' "
            "ORDER BY json_extract(props,'$.seq') DESC LIMIT ?", (limit,))

    if getattr(args, "json", False):
        print(json.dumps([json.loads(r[0]) for r in rows], indent=2, default=str))
        return
    if not rows:
        print("  No change-log entries.")
        return
    for r in rows:
        c    = json.loads(r[0])
        diff = json.loads(c.get("diff") or "{}")
        parts = ([f"{k}" for k in diff.get("fields", {})] +
                 [f"+{e}" for e in diff.get("edges_added", [])] +
                 [f"-{e}" for e in diff.get("edges_removed", [])])
        print(f"  {c.get('id','')}  {str(c.get('at',''))[:19]}  {c.get('actor',''):<24} "
              f"{c.get('action',''):<6} {c.get('entityId',''):<16} "
              f"{c.get('command',''):<18} {', '.join(parts)[:60]}")


def cmd_reconcile_done(args, root, S):
    """Post-completion reconciliation after closing an enhancement or defect."""
    enh_id  = getattr(args, "id",      "") or ""
    def_id  = getattr(args, "defect",  "") or ""
    caller  = getattr(args, "instance", "") or ""
    work_id = enh_id or def_id
    if not work_id:
        raise CommandError("ERROR: provide --id (enhancement) or --defect")
    is_defect = bool(def_id)
    lbl       = "SysDefect" if is_defect else "SysEnhancement"
    rel_type  = "AFFECTS"   if is_defect else "EXTENDS"
    node = S.node(lbl, work_id)
    if not node:
        raise CommandError(f"⚠ {work_id} not found")

    inst  = node.get("instance", caller or "unknown")
    title = node.get("title", "")
    desc  = node.get("description", "")
    feat_ids = S.out_ids(lbl, work_id, rel_type, "SysFeature")
    W = 70
    label_str = "DEFECT" if is_defect else "ENHANCEMENT"
    print("═" * W)
    print(f"  RECONCILIATION — {work_id} [{inst}]  {label_str}")
    print(f"  {title[:W-4]}")
    _INFRA = {"deploy", "infra", "sys-edge", "sysops"}
    if not feat_ids and inst in _INFRA:
        print("═" * W)
        print(f"  ℹ  Infra-only {label_str.lower()} — no feature/test traceability required")
        print("═" * W)
        return
    print("═" * W)

    print(f"\n{'─'*W}\n  FEATURES ({len(feat_ids)})\n{'─'*W}")
    feature_analysis = []
    for fid in feat_ids:
        feat  = S.node("SysFeature", fid) or {}
        fname = feat.get("name", fid)
        mod_ids = S.in_ids("SysFeature", fid, "PROVIDES", "SysModule")
        minst = (S.node("SysModule", mod_ids[0]) or {}).get("instance", "") if mod_ids else ""
        ep_count  = len(S.in_ids("SysFeature", fid, "IMPLEMENTS", "SysEndpoint"))
        sym_count = len(S.in_ids("SysFeature", fid, "IMPLEMENTS", "SysSymbol"))
        test_ids  = S.in_ids("SysFeature", fid, "VERIFIES", "SysTest")
        def _tier(tid, kind):
            t = S.node("SysTest", tid) or {}
            return t.get("testType", "") in kind
        cmp = sum(1 for t in test_ids if _tier(t, ("component", "go-unit")))
        int_ = sum(1 for t in test_ids if _tier(t, ("integration",)))
        uc  = sum(1 for t in test_ids if _tier(t, ("usecase",)))
        e2e = sum(1 for t in test_ids if _tier(t, ("e2e",)))
        uc_ids = S.in_ids("SysFeature", fid, "REQUIRES", "SysUseCase")
        ucs = []
        for ucid in uc_ids:
            ucn = S.node("SysUseCase", ucid) or {}
            story_ids = S.in_ids("SysUseCase", ucid, "REALIZED_BY", "SysUserStory")
            ucs.append({"ucid": ucid, "uctitle": ucn.get("title", ""),
                        "ucdesc": ucn.get("description", ""), "ucinst": ucn.get("instance", ""),
                        "stories": [{"id": s, "title": (S.node("SysUserStory", s) or {}).get("title", "")}
                                    for s in story_ids]})
        feature_analysis.append({
            "fid": fid, "fname": fname, "minst": minst,
            "ep_count": ep_count, "sym_count": sym_count,
            "tiers": {"cmp": cmp, "int_": int_, "uc": uc, "e2e": e2e}, "ucs": ucs,
        })
        print(f"  {fid}  {fname}  [{minst}]")
        print(f"    endpoints : {ep_count:3d}  {'✓' if ep_count > 0 else '⚠'}"
              + ("  ← run link-endpoint" if ep_count == 0 else ""))
        print(f"    symbols   : {sym_count:3d}  {'✓' if sym_count > 0 else '⚠'}")

    all_ucs_raw = [uc for fa in feature_analysis for uc in fa["ucs"]]
    all_ucs = [u for u in all_ucs_raw if not caller or not u.get("ucinst") or u["ucinst"] == caller]
    filtered = len(all_ucs_raw) - len(all_ucs)
    if all_ucs and not getattr(args, "skip_uc_check", False):
        print(f"\n{'─'*W}\n  USE CASES & USER STORIES — review prose for accuracy\n{'─'*W}")
        print(f"  Context: {desc[:120].strip()}")
        seen: set = set()
        for u in all_ucs:
            ucid = u["ucid"]
            if ucid in seen:
                continue
            seen.add(ucid)
            print(f"\n  [KEEP or UPDATE?]  {ucid}  {u['uctitle']}")
            for us in (u.get("stories") or []):
                if us.get("id"):
                    print(f"    → US: {us['id']}  {us.get('title', '')}")
        if filtered:
            print(f"\n  ℹ  {filtered} UC(s) from other instances not shown")

    print(f"\n{'─'*W}\n  TEST GAPS & REQUIRED ACTIONS\n{'─'*W}")
    for fa in feature_analysis:
        t = fa["tiers"]
        print(f"\n  {fa['fid']}  {fa['fname']}")
        for tier, cnt in [("cmp", t["cmp"]), ("int", t["int_"]), ("uc", t["uc"]), ("e2e", t["e2e"])]:
            mark = "✓" if cnt > 0 else "✗"
            print(f"  {mark} {tier:<4} {cnt:3d} tests"
                  + (f"  → sys_edge.py link-feature --feature {fa['fid']} --tests <id>"
                     if cnt == 0 and tier in ("cmp", "int") else ""))
    print(f"\n{'═'*W}")


# ── session-driven AI: enh-align and uc-state-check ──────────────────────────

def _emit_ai_request(command: str, prompt_lines: list, response_format: dict,
                     subjects, output_path: str) -> None:
    request = {
        "command": command,
        "instruction": f"Review the subjects and return JSON matching responseFormat.",
        "responseFormat": response_format,
        "subjects": subjects if isinstance(subjects, list) else [],
        "prompt": "\n".join(prompt_lines),
    }
    if output_path:
        Path(output_path).write_text(json.dumps(request, indent=2), encoding="utf-8")
        print(f"  {command} request written to: {output_path}")
    else:
        print("\n" + "=" * 62)
        print(f"  {command.upper()} REQUEST — paste into Claude Code session")
        print("=" * 62)
        print(request["prompt"])
        print("\n" + "=" * 62 + "\n  JSON (save and --apply-results):\n" + "=" * 62)
        print(json.dumps(request, indent=2))
    print(f"\n  After session responds, save result JSON and run:")
    print(f"    python3 scripts/sys_edge.py {command} --apply-results <result.json>")


def cmd_enh_align(args, root, S):
    """AI check that enhancement covers its linked UC behaviours (session-driven)."""
    apply_results = getattr(args, "apply_results", "") or ""
    if apply_results:
        data = json.loads(Path(apply_results).read_text(encoding="utf-8"))
        alignments = data.get("alignments", []) if isinstance(data, dict) else data
        ok = gaps = 0
        for a in alignments:
            eid  = a.get("enhId", "?")
            v    = a.get("verdict", "ALIGNED").upper()
            det  = a.get("detail", "")
            print(f"  {'✓' if v.startswith('ALIGNED') else '⚠'}  {eid}  {v}: {det}")
            if v.startswith("ALIGNED"):
                ok += 1
            else:
                gaps += 1
        print(f"\n  {ok} aligned  |  {gaps} gap(s) detected")
        return

    enh_id   = getattr(args, "id",       "") or ""
    instance = getattr(args, "instance", "") or ""
    if not enh_id and not instance:
        raise CommandError("ERROR: provide --id or --instance")

    enh_rows = ([S.node("SysEnhancement", enh_id)] if enh_id
                else S.nodes("SysEnhancement", instance=instance))
    enh_rows = [e for e in enh_rows if e and e.get("status") in ("open", "in-progress", None)]
    if not enh_rows:
        print("  No open enhancements found.")
        return

    items = []
    for e in enh_rows:
        eid      = e.get("id", "")
        feat_ids = S.out_ids("SysEnhancement", eid, "EXTENDS", "SysFeature")
        ucs = []
        for fid in feat_ids:
            feat = S.node("SysFeature", fid) or {}
            for ucid in S.in_ids("SysFeature", fid, "REQUIRES", "SysUseCase"):
                uc = S.node("SysUseCase", ucid) or {}
                ucs.append({"uid": ucid, "utitle": uc.get("title", ""),
                            "main": uc.get("mainFlow", ""), "udesc": uc.get("description", ""),
                            "fid": fid, "fname": feat.get("name", "")})
        items.append({"eid": eid, "etitle": e.get("title", ""), "edesc": e.get("description", ""),
                      "estatus": e.get("status", ""), "ucs": ucs})

    resp_fmt = {"alignments": [{"enhId": "<ENH-xxx>", "verdict": "ALIGNED|GAP",
                                "detail": "<one sentence>",
                                "missingBehaviours": ["<UC-xxx: what is missing>"]}]}
    lines = [
        "Check whether each enhancement description covers its linked use case behaviours.",
        "", "Return STRICT JSON:", json.dumps(resp_fmt, indent=2), "",
        "ALIGNED = all UC main flows accounted for; GAP = UC behaviour not mentioned.",
        "Return raw JSON only, no markdown fences.", "", "## Enhancements",
    ]
    for item in items:
        lines.append(f"\n### {item['eid']}  [{item['estatus']}]  {item['etitle']}")
        if item["edesc"]:
            lines.append(f"Description: {item['edesc'][:500]}")
        for u in item["ucs"]:
            lines.append(f"  {u['uid']} ({u['fname']}): {u['utitle']}")
            if u["main"]:
                lines.append(f"    Main flow: {u['main'][:300]}")
        if not item["ucs"]:
            lines.append("  (no linked UCs)")

    _emit_ai_request("enh-align", lines, resp_fmt, items,
                     getattr(args, "output_request", "") or "")


def cmd_uc_state_check(args, root, S):
    """AI check that UC postconditions align with preconditions of related UCs (session-driven)."""
    apply_results = getattr(args, "apply_results", "") or ""
    if apply_results:
        data = json.loads(Path(apply_results).read_text(encoding="utf-8"))
        checks = data.get("checks", []) if isinstance(data, dict) else data
        ok = conflicts = 0
        for c in checks:
            uc1, uc2 = c.get("uc1", "?"), c.get("uc2", "?")
            v    = c.get("verdict", "OK").upper()
            det  = c.get("detail", "")
            print(f"  {'✓' if v == 'OK' else '⚠'}  {uc1} → {uc2}  {v}: {det}")
            if v == "OK":
                ok += 1
            else:
                conflicts += 1
        print(f"\n  {ok} pair(s) consistent  |  {conflicts} conflict(s)")
        if conflicts:
            print("  Action: align pre/postconditions via update-usecase")
        return

    instance = getattr(args, "instance", "") or ""
    uc_id    = getattr(args, "uc",       "") or ""
    if not instance and not uc_id:
        raise CommandError("ERROR: provide --instance or --uc")

    if uc_id:
        story_ids = S.in_ids("SysUseCase", uc_id, "REALIZED_BY", "SysUserStory")
        peers: set = set()
        for sid in story_ids:
            for uid in S.out_ids("SysUserStory", sid, "REALIZED_BY", "SysUseCase"):
                if uid != uc_id:
                    peers.add(uid)
        candidate_ids = [uc_id] + list(peers)
    else:
        candidate_ids = [r[0] for r in S.q(
            "SELECT id FROM nodes WHERE label='SysUseCase' "
            "AND json_extract(props,'$.instance')=? ORDER BY id", (instance,))]

    uc_cache = {uid: (S.node("SysUseCase", uid) or {}) for uid in candidate_ids}
    pairs = []
    seen: set = set()
    for id1 in candidate_ids:
        n1 = uc_cache.get(id1, {})
        if not n1.get("postconditions") and not n1.get("preconditions"):
            continue
        for sid in S.in_ids("SysUseCase", id1, "REALIZED_BY", "SysUserStory"):
            for id2 in S.out_ids("SysUserStory", sid, "REALIZED_BY", "SysUseCase"):
                if id2 == id1:
                    continue
                key = tuple(sorted([id1, id2]))
                if key in seen:
                    continue
                seen.add(key)
                n2 = uc_cache.get(id2) or S.node("SysUseCase", id2) or {}
                if (n1.get("postconditions") and n2.get("preconditions")) or \
                   (n2.get("postconditions") and n1.get("preconditions")):
                    pairs.append((id1, n1, id2, n2))

    if not pairs:
        scope = f"UC {uc_id}" if uc_id else f"instance {instance}"
        print(f"  No UC pairs with pre/postconditions found for {scope}.")
        print("  Tip: set preconditions and postconditions via update-usecase.")
        return

    resp_fmt = {"checks": [{"uc1": "<id>", "uc2": "<id>", "verdict": "OK|CONFLICT",
                            "detail": "<one sentence>"}]}
    lines = [
        "Check state consistency between UC pairs sharing a user story.",
        "", "Return STRICT JSON:", json.dumps(resp_fmt, indent=2), "",
        "CONFLICT if UC-A's postcondition leaves a state UC-B cannot start from.",
        "Return raw JSON only, no markdown fences.", "", "## Pairs to check",
    ]
    for id1, n1, id2, n2 in pairs[:40]:
        lines.append(f"\n### {id1} → {id2}")
        if n1.get("postconditions"):
            lines.append(f"  {id1} postconditions: {str(n1['postconditions'])[:300]}")
        if n2.get("preconditions"):
            lines.append(f"  {id2} preconditions:  {str(n2['preconditions'])[:300]}")
        if n2.get("postconditions"):
            lines.append(f"  {id2} postconditions: {str(n2['postconditions'])[:300]}")
        if n1.get("preconditions"):
            lines.append(f"  {id1} preconditions:  {str(n1['preconditions'])[:300]}")

    _emit_ai_request("uc-state-check", lines, resp_fmt,
                     [{"uc1": id1, "uc2": id2} for id1, n1, id2, n2 in pairs],
                     getattr(args, "output_request", "") or "")


# ── reqif ─────────────────────────────────────────────────────────────────────

_REQIF_TMPL = """\
<?xml version="1.0" encoding="UTF-8"?>
<REQ-IF xmlns="http://www.omg.org/spec/ReqIF/20110401/reqif.xsd">
  <THE-HEADER>
    <REQ-IF-HEADER IDENTIFIER="{header_id}">
      <COMMENT>{title}</COMMENT>
      <CREATION-TIME>{created}</CREATION-TIME>
      <REQ-IF-TOOL-ID>SysEdge</REQ-IF-TOOL-ID>
      <TITLE>{title}</TITLE>
    </REQ-IF-HEADER>
  </THE-HEADER>
  <CORE-CONTENT>
    <REQ-IF-CONTENT>
      <SPEC-OBJECTS>
{spec_objects}
      </SPEC-OBJECTS>
    </REQ-IF-CONTENT>
  </CORE-CONTENT>
</REQ-IF>
"""

_REQIF_OBJ = (
    '        <SPEC-OBJECT IDENTIFIER="{sid}" LAST-CHANGE="{changed}">\n'
    '          <VALUES>\n'
    '            <ATTRIBUTE-VALUE-STRING THE-VALUE="{title}">'
    '<DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>Title</ATTRIBUTE-DEFINITION-STRING-REF>'
    '</DEFINITION></ATTRIBUTE-VALUE-STRING>\n'
    '            <ATTRIBUTE-VALUE-STRING THE-VALUE="{desc}">'
    '<DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>Description</ATTRIBUTE-DEFINITION-STRING-REF>'
    '</DEFINITION></ATTRIBUTE-VALUE-STRING>\n'
    '          </VALUES>\n'
    '        </SPEC-OBJECT>'
)


def _xml_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _sqcfg(root: Path) -> dict:
    try:
        text = (Path(root) / "sysedge.yaml").read_text(encoding="utf-8")
        return {m.group(1): m.group(2).strip().strip("'\"")
                for m in re.finditer(r"^(\w+)\s*:\s*(.+)$", text, re.MULTILINE)}
    except OSError:
        return {}


def cmd_reqif(args, root, S):
    """Export requirements as ReqIF 1.2, or import SPEC-OBJECTs as user stories."""
    action   = getattr(args, "action", "export") or "export"
    instance = getattr(args, "instance", "") or ""
    module   = getattr(args, "module",   "") or ""
    title    = getattr(args, "title",    "") or _sqcfg(root).get("title", "SysEdge Export")
    prefix   = getattr(args, "prefix",   "US-REQIF") or "US-REQIF"

    if action == "export":
        items = [(s, s.get("title") or s.get("name") or "", s.get("description") or s.get("goal") or "")
                 for s in S.nodes("SysUserStory")
                 if not instance or s.get("instance") == instance]
        items += [(u, u.get("title") or u.get("name") or "", u.get("description", ""))
                  for u in S.nodes("SysUseCase")
                  if not instance or u.get("instance") == instance]
        if module:
            feat_ids = set(S.out_ids("SysModule", module, "PROVIDES", "SysFeature"))
            items += [(f, f.get("name", ""), f.get("description", ""))
                      for f in S.nodes("SysFeature") if f.get("id") in feat_ids]

        objs = "\n".join(
            _REQIF_OBJ.format(
                sid=item[0].get("id", ""),
                changed=(item[0].get("updatedAt") or item[0].get("createdAt") or _now_iso())[:19],
                title=_xml_esc(item[1]), desc=_xml_esc(item[2]))
            for item in items)
        out_arg  = getattr(args, "file", "") or f"sysedge-{instance or module or 'all'}.reqif"
        out_path = _resolve(root, out_arg)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_REQIF_TMPL.format(
            header_id=f"reqif-{_now_iso()[:10]}", title=_xml_esc(title),
            created=_now_iso()[:19], spec_objects=objs), encoding="utf-8")
        print(f"✓ {len(items)} requirements → {out_path}")

    elif action == "import":
        file_arg = getattr(args, "file", "") or ""
        if not file_arg:
            raise CommandError("reqif import needs --file")
        import xml.etree.ElementTree as _ET
        ns = "http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"
        try:
            root_el = _ET.fromstring(_resolve(root, file_arg).read_text(encoding="utf-8"))
        except _ET.ParseError as e:
            raise CommandError(f"ReqIF parse error: {e}")
        recs = []
        for obj in root_el.iter(f"{{{ns}}}SPEC-OBJECT"):
            identifier = obj.get("IDENTIFIER", "")
            vals: dict = {}
            for av in obj.iter(f"{{{ns}}}ATTRIBUTE-VALUE-STRING"):
                defn = av.find(f"{{{ns}}}DEFINITION/{{{ns}}}ATTRIBUTE-DEFINITION-STRING-REF")
                key  = defn.text if defn is not None else "unknown"
                vals[key] = av.get("THE-VALUE", "")
            recs.append({"identifier": identifier, "title": vals.get("Title", identifier),
                         "description": vals.get("Description", "")})
        existing: dict = {}
        for r in S.q("SELECT id, json_extract(props,'$.externalRefs') FROM nodes WHERE label='SysUserStory'"):
            try:
                refs = json.loads(r[1]) if r[1] and isinstance(r[1], str) else (r[1] or [])
                for ref in refs:
                    if str(ref).startswith("reqif:"):
                        existing[ref] = r[0]
            except Exception:
                pass
        apply_flag = getattr(args, "apply", False)
        seq = max(
            (int(r[0][len(prefix)+1:]) for r in S.q(
                "SELECT id FROM nodes WHERE label='SysUserStory' AND substr(id,1,?)=?",
                (len(prefix), prefix)) if r[0][len(prefix)+1:].isdigit()),
            default=0)
        print(f"  {'action':<8} {'id':<16} {'identifier':<42} title")
        for rec in recs:
            ref = f"reqif:{rec['identifier']}"
            ex  = existing.get(ref)
            action_str = "update" if ex else "create"
            nid = ex or f"{prefix}-{seq + 1:03d}"
            if not ex:
                seq += 1
            print(f"  {action_str:<8} {nid:<16} {ref:<42} {rec['title'][:50]}")
            if apply_flag:
                n = S.node("SysUserStory", nid)
                ext = list((n or {}).get("externalRefs") or [])
                if ref not in ext:
                    ext.append(ref)
                with S.write():
                    S.upsert("SysUserStory", nid, {
                        "title": rec["title"], "description": rec["description"],
                        "source": "reqif", "instance": instance, "externalRefs": ext,
                    })
        if not apply_flag:
            print(f"\n  (dry run — {len(recs)} SPEC-OBJECTs; --apply to write)")
        else:
            print(f"\n✓ {len(recs)} user stories written from {file_arg}")


# ── preview-import / commit-import ───────────────────────────────────────────

def cmd_preview_import(args, root, S):
    """Classify entities in an import JSON file as new/duplicate/near-duplicate."""
    path = _resolve(root, args.file)
    if not path.exists():
        raise CommandError(f"ERROR: {path} not found")
    data     = json.loads(path.read_text(encoding="utf-8"))
    kind     = data.get("kind", "unknown")
    entities = data.get("entities", [])
    if not entities:
        print(f"  No entities found in {path} (kind={kind})")
        return

    W = 70
    print(f"\n{'═'*W}")
    print(f"  IMPORT PREVIEW  kind={kind}  {len(entities)} entities")
    print(f"  {'Ref':<22} {'Title/Name':<35} {'Status':<12} Action")
    print(f"{'─'*W}")
    decisions = []

    for ent in entities:
        ref    = ent.get("id_hint") or ent.get("name") or "?"
        title  = (ent.get("title") or ent.get("name") or "")[:34]
        conf   = ent.get("_confidence", 1.0)
        action = "create"
        status = "new"
        match_id = None

        if conf < 0.5:
            action, status = "skip", "low-conf"
        elif kind == "stories":
            exact = S.q(
                "SELECT id FROM nodes WHERE label='SysUserStory' "
                "AND lower(json_extract(props,'$.title'))=lower(?) LIMIT 1",
                (ent.get("title", ""),))
            if exact:
                action, status, match_id = "skip", "duplicate", exact[0][0]
            else:
                actor = (ent.get("actor") or "").lower()
                words = set((ent.get("title") or "").lower().split())
                near = S.q(
                    "SELECT id, json_extract(props,'$.title') FROM nodes "
                    "WHERE label='SysUserStory' "
                    "AND lower(json_extract(props,'$.actor'))=? LIMIT 20", (actor,))
                for nr in near:
                    rw = set((nr[1] or "").lower().split())
                    if rw and words and len(words & rw) / max(len(words | rw), 1) >= 0.70:
                        action, status, match_id = "review", "near-dup", nr[0]
                        break
        elif kind == "use-cases":
            inst = ent.get("instance") or data.get("instance", "")
            ex = S.q(
                "SELECT id FROM nodes WHERE label='SysUseCase' "
                "AND json_extract(props,'$.name')=? AND json_extract(props,'$.instance')=? LIMIT 1",
                (ent.get("name", ""), inst))
            if ex:
                action, status, match_id = "skip", "duplicate", ex[0][0]
        elif kind == "requirements":
            mod = ent.get("moduleId") or ent.get("_moduleHint", "")
            ex = S.q(
                "SELECT f.id FROM nodes f JOIN edges e ON e.to_id=f.id AND e.type='PROVIDES' "
                "AND e.to_label='SysFeature' AND e.from_label='SysModule' "
                "WHERE f.label='SysFeature' AND lower(json_extract(f.props,'$.name'))=lower(?) "
                "AND (e.from_id=? OR e.from_id IN "
                "(SELECT id FROM nodes WHERE label='SysModule' "
                " AND lower(json_extract(props,'$.name'))=lower(?))) LIMIT 1",
                (ent.get("name", ""), mod, mod))
            if ex:
                action, status, match_id = "skip", "duplicate", ex[0][0]

        warn = (" [ambiguous]" if ent.get("_ambiguous") else "") + \
               (" [no actor]" if not ent.get("actor") and kind == "stories" and action == "create" else "")
        match_str = f"← {match_id}" if match_id else ""
        print(f"  {ref:<22} {title:<35} {status:<12} {action}{warn} {match_str}")
        decisions.append({"ref": ref, "action": action, "status": status,
                          "match_id": match_id, "entity": ent})

    print(f"{'═'*W}")
    dec_path = path.with_name(path.stem + "-decisions.json")
    dec_path.write_text(json.dumps(decisions, indent=2, default=str), encoding="utf-8")
    print(f"\n  Edit actions in: {dec_path}")
    print(f"  Then run: sys_edge.py commit-import {path} --decisions {dec_path}")


def cmd_commit_import(args, root, S):
    """Apply accepted import decisions to the graph."""
    data_path = _resolve(root, args.file)
    dec_path  = _resolve(root, args.decisions)
    if not data_path.exists():
        raise CommandError(f"ERROR: {data_path} not found")
    if not dec_path.exists():
        raise CommandError(f"ERROR: {dec_path} — run preview-import first")
    data      = json.loads(data_path.read_text(encoding="utf-8"))
    decisions = json.loads(dec_path.read_text(encoding="utf-8"))
    kind      = data.get("kind", "unknown")
    instance  = data.get("instance", "")

    def _text(v):
        if isinstance(v, (list, tuple)):
            return "\n".join(str(x) for x in v if str(x).strip())
        return v or ""

    def _fields(k_, ent):
        if k_ == "stories":
            p = {"title": ent.get("title"), "actor": ent.get("actor"), "goal": ent.get("goal"),
                 "benefit": ent.get("benefit"), "narrative": ent.get("narrative"),
                 "priority": ent.get("priority"),
                 "acceptanceCriteria": ent.get("acceptance_criteria") or ent.get("acceptanceCriteria")}
        elif k_ == "use-cases":
            p = {"title": ent.get("title"), "name": ent.get("name"), "actor": ent.get("actor"),
                 "description": ent.get("description"), "priority": ent.get("priority"),
                 "preconditions": _text(ent.get("preconditions")),
                 "mainFlow": _text(ent.get("main_flow") or ent.get("mainFlow")),
                 "postconditions": _text(ent.get("postconditions")),
                 "authorizedRoles": _text(ent.get("authorized_roles") or ent.get("authorizedRoles")),
                 "failureScenarios": _text(ent.get("failure_scenarios") or ent.get("failureScenarios"))}
        else:
            p = {"name": ent.get("name"), "description": ent.get("description")}
        return {k: v for k, v in p.items() if v not in (None, "", [])}

    labels = {"stories": ("SysUserStory", "US-IMPORT"),
              "use-cases": ("SysUseCase", "UC-IMPORT"),
              "requirements": ("SysFeature", "F-IMPORT")}
    if kind not in labels:
        raise CommandError(f"unknown import kind '{kind}'")
    lbl, id_prefix = labels[kind]

    committed = skipped = failed = 0
    seq = max(
        (int(r[0][len(id_prefix)+1:]) for r in S.q(
            "SELECT id FROM nodes WHERE label=? AND substr(id,1,?)=?",
            (lbl, len(id_prefix), id_prefix)) if r[0][len(id_prefix)+1:].isdigit()),
        default=0)

    for dec in decisions:
        action = dec.get("action", "skip")
        ent    = dec.get("entity", {}) or {}
        ref    = dec.get("ref", "?")
        if action not in ("create", "update", "merge-into"):
            skipped += 1
            continue
        try:
            props = _fields(kind, ent)
            if action == "create":
                node_id = ent.get("id_hint")
                if not node_id:
                    seq += 1
                    node_id = f"{id_prefix}-{seq:03d}"
                if S.node(lbl, node_id):
                    print(f"  ⚠ {ref}: {node_id} exists — use action 'update'", file=sys.stderr)
                    skipped += 1
                    continue
                if kind == "use-cases":
                    props.setdefault("name", node_id.lower())
                    props["instance"] = instance or ent.get("instance", "")
                elif kind == "stories":
                    props["instance"] = instance
                    props.setdefault("priority", "P1")
                else:
                    props.setdefault("status", "Proposed")
                with S.write():
                    S.upsert(lbl, node_id, props)
            else:
                node_id = dec.get("match_id") or ent.get("id_hint")
                if not node_id or not S.node(lbl, node_id):
                    print(f"  ✗ {ref}: needs match_id of an existing {lbl}", file=sys.stderr)
                    failed += 1
                    continue
                with S.write():
                    S.update(lbl, node_id, _fields(kind, ent))

            with S.write():
                if kind == "stories":
                    for fid in (ent.get("feature_refs") or []):
                        S.link("SysUserStory", node_id, "REQUIRES", "SysFeature", fid)
                elif kind == "use-cases":
                    for sid in (ent.get("story_refs") or []):
                        S.link("SysUserStory", sid, "REALIZED_BY", "SysUseCase", node_id)
                    for fid in (ent.get("feature_refs") or []):
                        S.link("SysUseCase", node_id, "REQUIRES", "SysFeature", fid)
                elif ent.get("moduleId"):
                    S.link("SysModule", ent["moduleId"], "PROVIDES", "SysFeature", node_id)

            t = ent.get("title") or ent.get("name") or ""
            print(f"  ✓ {action:<10} {node_id}  {t[:50]}")
            committed += 1
        except Exception as e:
            print(f"  ✗ {ref}: {e}", file=sys.stderr)
            failed += 1

    print(f"\n{'═'*60}")
    print(f"  committed={committed}  skipped={skipped}  failed={failed}")


# ── Go scanning ───────────────────────────────────────────────────────────────

_GO_ROUTE_RE_SQ    = re.compile(r'\br\.(Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*"([^"]+)"')
_GO_PERM_RE_SQ     = re.compile(r'withPerm\s*\(\s*"([^"]+)"')
_GO_AUTH_RE_SQ     = re.compile(r'withAuth\s*\(')
_GO_REQPERM_RE_SQ  = re.compile(r'RequirePermission\s*\(\s*"([^"]+)"')
_GO_TEST_RE_SQ     = re.compile(r'^func (Test[A-Za-z0-9_]+)\s*\(t \*testing\.')
_GO_BENCH_RE_SQ    = re.compile(r'^func (Benchmark[A-Za-z0-9_]+)\s*\(')
_SKIP_DIRS_SQ      = {"node_modules", ".git", "dist", "build", "vendor", "__pycache__"}


def _go_root_sqlite(root: Path) -> Path:
    p = _sqcfg(root).get("go_root", ".")
    r = Path(p)
    return r if r.is_absolute() else root / r


def _scan_go_routes_sqlite(scan_root: Path) -> list:
    routes = []
    for f in sorted(scan_root.rglob("*.go")):
        if "_test.go" in f.name or any(p in _SKIP_DIRS_SQ for p in f.parts):
            continue
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _GO_ROUTE_RE_SQ.search(line)
                if not m:
                    continue
                pm   = _GO_PERM_RE_SQ.search(line) or _GO_REQPERM_RE_SQ.search(line)
                perm = pm.group(1) if pm else ("authenticated" if _GO_AUTH_RE_SQ.search(line) else "")
                routes.append({"method": m.group(1).upper(), "path": m.group(2), "permission": perm})
        except Exception:
            pass
    return routes


def cmd_scan_endpoints(args, root, S):
    """Scan Go source for route registrations and diff against SysEndpoint graph nodes."""
    path_arg   = getattr(args, "path", "") or ""
    instance   = getattr(args, "instance", "") or ""
    apply_mode = getattr(args, "apply", False)
    scan_root  = Path(path_arg) if path_arg else _go_root_sqlite(root)
    if not Path(scan_root).is_absolute():
        scan_root = root / scan_root
    if not scan_root.exists():
        raise CommandError(f"ERROR: {scan_root} does not exist — pass --path <go-root>")

    code_routes = _scan_go_routes_sqlite(scan_root)
    code_index  = {(r["method"], r["path"]): r for r in code_routes}

    if instance:
        graph_rows = S.q("""
            SELECT json_extract(e.props,'$.method'), json_extract(e.props,'$.path'),
                   json_extract(e.props,'$.permission'), e.id
            FROM nodes e
            WHERE e.label='SysEndpoint'
              AND EXISTS (
                SELECT 1 FROM edges eg
                JOIN edges ep ON ep.to_id=eg.to_id AND ep.type='PROVIDES' AND ep.to_label='SysFeature'
                JOIN nodes m ON m.label='SysModule' AND m.id=ep.from_id
                WHERE eg.from_id=e.id AND eg.type='IMPLEMENTS' AND eg.from_label='SysEndpoint'
                  AND json_extract(m.props,'$.instance')=?
              )
        """, (instance,))
    else:
        graph_rows = S.q(
            "SELECT json_extract(props,'$.method'), json_extract(props,'$.path'), "
            "json_extract(props,'$.permission'), id FROM nodes WHERE label='SysEndpoint'")

    graph_index = {(r[0], r[1]): {"method": r[0], "path": r[1], "perm": r[2], "eid": r[3]}
                   for r in graph_rows if r[0] and r[1]}

    missing = [r for k, r in code_index.items() if k not in graph_index]
    stale   = [ep for k, ep in graph_index.items() if k not in code_index]
    perm_mm = [{**r, "graph_perm": graph_index[(r["method"], r["path"])].get("perm", "")}
               for r in code_routes
               if (r["method"], r["path"]) in graph_index
               and r["permission"]
               and graph_index[(r["method"], r["path"])].get("perm", "") != r["permission"]]

    print(f"\nENDPOINT DRIFT REPORT  ({scan_root})")
    print(f"  Code: {len(code_index)} routes  |  Graph: {len(graph_index)} nodes\n")
    if missing:
        print(f"  MISSING IN GRAPH ({len(missing)}):")
        for r in missing:
            print(f"    {r['method']:<7} {r['path']:<60}" +
                  (f"  [{r['permission']}]" if r["permission"] else ""))
    else:
        print("  MISSING IN GRAPH: none ✓")
    if stale:
        print(f"\n  STALE IN GRAPH ({len(stale)}):")
        for ep in stale:
            print(f"    {ep.get('method',''):<7} {ep.get('path','')}")
    else:
        print("\n  STALE IN GRAPH: none ✓")
    if perm_mm:
        print(f"\n  PERMISSION MISMATCH ({len(perm_mm)}):")
        for r in perm_mm:
            print(f"    {r['method']:<7} {r['path']:<55}  code={r['permission']}  graph={r['graph_perm']}")
    else:
        print("\n  PERMISSION MISMATCH: none ✓")

    if apply_mode and (missing or perm_mm):
        with S.write():
            for r in missing:
                eid = f"EP-{r['method']}-{r['path'].replace('/', '-')}"[:80]
                S.upsert("SysEndpoint", eid, {"method": r["method"], "path": r["path"],
                                              "permission": r["permission"]})
                print(f"  ✓ upserted  {r['method']} {r['path']}")
            for r in perm_mm:
                eid = f"EP-{r['method']}-{r['path'].replace('/', '-')}"[:80]
                S.update("SysEndpoint", eid, {"permission": r["permission"]})
                print(f"  ✓ perm fix  {r['method']} {r['path']}")
        print(f"\n  Applied {len(missing)+len(perm_mm)} changes.")

    if missing or stale or perm_mm:
        sys.exit(1)


def cmd_scan_go_tests(args, root, S):
    """Scan Go *_test.go files and register test functions as SysTest nodes."""
    path_arg  = getattr(args, "path", "") or ""
    scan_root = Path(path_arg) if path_arg else _go_root_sqlite(root)
    if not Path(scan_root).is_absolute():
        scan_root = root / scan_root
    if not scan_root.exists():
        raise CommandError(f"ERROR: {scan_root} does not exist — pass --path <go-root>")

    valid_ids: set = set()
    for lbl in ("SysFeature", "SysUserStory", "SysUseCase"):
        for r in S.q("SELECT id FROM nodes WHERE label=?", (lbl,)):
            valid_ids.add(r[0])

    total = files = total_links = 0
    scan_path = Path(scan_root)
    go_files  = [scan_path] if scan_path.is_file() else sorted(scan_path.rglob("*_test.go"))

    with S.write():
        for f in go_files:
            if any(p in _SKIP_DIRS_SQ for p in f.parts):
                continue
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)
            pkg_id = rel
            fp_lower = rel.lower()
            category = ("e2e"         if "e2e" in fp_lower or "end_to_end" in fp_lower
                        else "usecase" if "usecase" in fp_lower
                        else "integration" if "integration" in fp_lower
                        else "component")
            S.upsert("SysTestPackage", pkg_id, {
                "path": pkg_id, "area": f.stem, "testType": category,
                "testCategory": category, "canonicalRoot": "go",
            })
            tests = []
            source = ""
            try:
                source = f.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(source.splitlines(), 1):
                    for pat in (_GO_TEST_RE_SQ, _GO_BENCH_RE_SQ):
                        m = pat.match(line)
                        if m:
                            fn  = m.group(1)
                            tid = f"{pkg_id}::{fn}"
                            tests.append({"id": tid, "file": pkg_id, "fn": fn,
                                          "testType": category, "packageId": pkg_id,
                                          "line": lineno})
            except Exception:
                pass
            if tests:
                refs = [r for r in _ENTITY_REF_RE.findall(source) if r in valid_ids]
                for t in tests:
                    S.upsert("SysTest", t["id"], {
                        "file": t["file"], "fn": t["fn"], "testType": t["testType"],
                        "packageId": t["packageId"], "line": t["line"],
                    })
                    S.link("SysTestPackage", pkg_id, "CONTAINS_TEST", "SysTest", t["id"])
                    for ref_id in refs:
                        if ref_id.startswith("F-"):
                            S.link("SysTest", t["id"], "VERIFIES", "SysFeature", ref_id)
                total_links += len(refs) * len(tests)
                total += len(tests)
                files += 1

    link_note = f", {total_links} VERIFIES links" if total_links else ""
    print(f"  Go tests: {files} files, {total} test functions registered{link_note}")


# ── export-neo4j ──────────────────────────────────────────────────────────────

def _cypher_val(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        s = json.dumps(v, default=str)
        return s
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def cmd_export_neo4j(args, root, S):
    """Export SQLite graph to a Cypher script loadable into Neo4j (migration bridge)."""
    out_file = getattr(args, "output", "") or "sysedge-export.cypher"
    out_path = _resolve(root, out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "// SysEdge graph export — generated by sys_edge_sqlite.py export-neo4j",
        f"// Generated: {_now_iso()}",
        "// Run with: cypher-shell -u neo4j -p <password> -f sysedge-export.cypher",
        "//   or paste into Neo4j Browser",
        "",
        "// ── NODES ──────────────────────────────────────────────────────────────",
    ]
    node_rows = S.q("SELECT label, id, props FROM nodes ORDER BY label, id")
    for r in node_rows:
        lbl, nid, props_raw = r[0], r[1], r[2]
        props = json.loads(props_raw) if props_raw else {}
        props["id"] = nid
        lines.append(f"MERGE (n:{lbl} {{id: {_cypher_val(nid)}}})")
        set_parts = [f"n.{k} = {_cypher_val(v)}"
                     for k, v in sorted(props.items()) if k != "id" and v is not None]
        if set_parts:
            lines.append("SET " + ", ".join(set_parts) + ";")
        else:
            lines[-1] += ";"
        lines.append("")

    lines.append("// ── RELATIONSHIPS ──────────────────────────────────────────────────────")
    edge_rows = S.q(
        "SELECT from_label, from_id, type, to_label, to_id, props FROM edges "
        "ORDER BY type, from_label, from_id, to_label, to_id")
    for r in edge_rows:
        fl, fid, rtype, tl, tid, ep_raw = r[0], r[1], r[2], r[3], r[4], r[5]
        ep = json.loads(ep_raw) if ep_raw else {}
        set_clause = ""
        if ep:
            set_clause = " SET " + ", ".join(
                f"r.{k} = {_cypher_val(v)}" for k, v in sorted(ep.items()) if v is not None)
        lines.append(f"MATCH (a:{fl} {{id: {_cypher_val(fid)}}}), (b:{tl} {{id: {_cypher_val(tid)}}})")
        lines.append(f"MERGE (a)-[r:{rtype}]->(b){set_clause};")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ {len(node_rows)} nodes + {len(edge_rows)} relationships → {out_path}")
    print(f"  Run: cypher-shell -u neo4j -p <password> -f {out_path}")


# ── Module context ────────────────────────────────────────────────────────────

def cmd_add_module_context(args, root, S):
    """Store a context note (constraint/concern/decision/outcome) on a module."""
    mid = args.module
    if not S.node("SysModule", mid):
        raise CommandError(f"Module '{mid}' not found.")
    with S.write():
        mcid = S.alloc_id("SysModuleContext", "MC-")
        S.update("SysModuleContext", mcid, {
            "module": mid,
            "kind": args.kind,
            "text": args.text,
            "created": _now_iso(),
            "author": getattr(args, "author", "") or "session",
        })
        S.link("SysModuleContext", mcid, "CONTEXT_FOR", "SysModule", mid)
    snippet = args.text[:60] + "…" if len(args.text) > 60 else args.text
    print(f"✓ {mcid} added to {mid} [{args.kind}]: \"{snippet}\"")


def cmd_show_module_context(args, root, S):
    """List context notes for a module, newest first, grouped by kind."""
    mid = args.module
    kind_filter = getattr(args, "kind", "") or ""
    mc_ids = S.in_ids("SysModule", mid, "CONTEXT_FOR", "SysModuleContext")
    if not mc_ids:
        print(f"No context notes for {mid}.")
        return
    notes = []
    for mcid in mc_ids:
        n = S.node("SysModuleContext", mcid)
        if n and (not kind_filter or n.get("kind") == kind_filter):
            notes.append(n)
    if not notes:
        suffix = f" [{kind_filter}]" if kind_filter else ""
        print(f"No context notes for {mid}{suffix}.")
        return
    print(f"\n{'═' * W}")
    print(f"  MODULE CONTEXT — {mid}  ({len(notes)} notes)")
    print(f"{'═' * W}")
    for k in ("constraint", "concern", "decision", "outcome"):
        kn = sorted([n for n in notes if n.get("kind") == k],
                    key=lambda n: n.get("created") or "", reverse=True)
        if not kn:
            continue
        print(f"\n  [{k.upper()}]")
        for n in kn:
            date = str(n.get("created", ""))[:10]
            text = n.get("text", "")
            snippet = text[:80] + "…" if len(text) > 80 else text
            print(f"  {n['id']}  {date}  {snippet}")
    print(f"\n{'═' * W}")


# ── deliver-brief ─────────────────────────────────────────────────────────────

def cmd_deliver_brief(args, root, S):
    """Assemble a full delivery brief for an ENH or DEF and emit JSON + agent instructions."""
    enh_id = getattr(args, "enh", "") or ""
    def_id = getattr(args, "defect_id", "") or ""

    if enh_id:
        node = S.node("SysEnhancement", enh_id)
        if not node:
            raise CommandError(f"Enhancement '{enh_id}' not found.")
        subject_kind = "enh"
        subject_id = enh_id
        feature_ids = S.out_ids("SysEnhancement", enh_id, "EXTENDS", "SysFeature")
    elif def_id:
        node = S.node("SysDefect", def_id)
        if not node:
            raise CommandError(f"Defect '{def_id}' not found.")
        subject_kind = "def"
        subject_id = def_id
        feature_ids = S.out_ids("SysDefect", def_id, "AFFECTS", "SysFeature")
    else:
        raise CommandError("ERROR: provide --enh ENH-xxx or --def DEF-xxx")

    subject = {
        "kind": subject_kind,
        "id": subject_id,
        "title": node.get("title", ""),
        "priority": node.get("priority", ""),
        "description": node.get("description") or node.get("body") or "",
    }

    mof = _module_of_feature(S)
    features = []
    module_ids: set = set()
    for fid in feature_ids:
        f = S.node("SysFeature", fid) or {}
        uc_ids = S.in_ids("SysFeature", fid, "REQUIRES", "SysUseCase")
        story_ids = S.in_ids("SysFeature", fid, "REQUIRES", "SysUserStory")
        test_count = len(S.in_ids("SysFeature", fid, "VERIFIES", "SysTest"))
        mid = mof.get(fid) or f.get("moduleId") or ""
        if mid:
            module_ids.add(mid)
        features.append({
            "id": fid,
            "title": f.get("name") or f.get("title") or "",
            "status": f.get("status") or "",
            "test_count": test_count,
            "use_cases": [{"id": uid,
                           "title": (S.node("SysUseCase", uid) or {}).get("title") or ""}
                          for uid in uc_ids],
            "stories": [{"id": sid,
                         "title": (S.node("SysUserStory", sid) or {}).get("title") or ""}
                        for sid in story_ids],
        })

    modules = []
    for mid in sorted(module_ids):
        m = S.node("SysModule", mid) or {}
        modules.append({"id": mid, "title": m.get("name") or m.get("title") or ""})

    mc_notes: list = []
    for mid in sorted(module_ids):
        for mcid in S.in_ids("SysModule", mid, "CONTEXT_FOR", "SysModuleContext"):
            n = S.node("SysModuleContext", mcid) or {}
            mc_notes.append({
                "id": mcid, "module": mid,
                "kind": n.get("kind") or "", "text": n.get("text") or "",
                "created": n.get("created") or "",
            })
    mc_notes.sort(key=lambda n: (n["kind"], n["created"]))

    open_defects: list = []
    if subject_kind == "enh":
        for fid in feature_ids:
            for did in S.in_ids("SysFeature", fid, "AFFECTS", "SysDefect"):
                d = S.node("SysDefect", did) or {}
                if d.get("status") in ("open", "in-progress"):
                    open_defects.append({
                        "id": did, "title": d.get("title") or "", "feature": fid})

    title = node.get("title", "")
    label = f"{subject_id}  —  {title}" if title else subject_id
    sep = "═" * W

    print(f"\n{sep}")
    print(f"  DELIVERY BRIEF  {label}")
    print(f"{sep}\n")

    brief = {
        "command": "deliver-brief",
        "schemaVersion": 1,
        "subject": subject,
        "features": features,
        "modules": modules,
        "module_context": mc_notes,
        "open_defects_on_features": open_defects,
    }
    print("---DELIVER-BRIEF-JSON-START---")
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    print("---DELIVER-BRIEF-JSON-END---\n")

    kind_word = "enhancement" if subject_kind == "enh" else "defect"
    print(f"## Instructions for the delivery agent\n")
    print(f"You are implementing **{subject_id}: {title}**.")
    if subject.get("description"):
        print(f"\n**Description:** {subject['description'][:300]}")

    print("\n### Module context")
    if mc_notes:
        for n in mc_notes:
            print(f"- [{n['kind']}] {n['text']}")
    else:
        print("No module context notes recorded — add them with `add-module-context`.")

    print("\n### Scope")
    if features:
        for f in features:
            print(f"\n**{f['id']}** — {f['title']} (status: {f['status']}, {f['test_count']} tests)")
            if f["use_cases"]:
                print("  Use cases: " + ", ".join(
                    f"{uc['id']} {uc['title']}" for uc in f["use_cases"]))
            if f["stories"]:
                print("  Stories: " + ", ".join(
                    f"{s['id']} {s['title']}" for s in f["stories"]))
    else:
        print("No linked features — link them first with `link-enhancement` or `link-defect`.")

    print("\n### Known issues on these features")
    if open_defects:
        for d in open_defects:
            print(f"- {d['id']} [{d['feature']}]: {d['title']}")
    else:
        print("None")

    print("\n### Acceptance")
    for f in features:
        print(f"- After implementation: `sys_edge.py check-test-links --feature {f['id']}`")
    if subject_kind == "enh":
        print(f"- Close with: `sys_edge.py close-enhancement {subject_id}`")
    else:
        print(f"- Close with: `sys_edge.py close-defect --id {subject_id}`")


# ── Entry point ──────────────────────────────────────────────────────────────

_HANDLERS = {
    "init": cmd_init, "seed": cmd_seed, "backup": cmd_backup, "status": cmd_status,
    "briefing": cmd_briefing, "coverage": cmd_briefing, "worklog": cmd_worklog,
    "test-gaps": cmd_test_gaps,
    "create-enhancement": cmd_create_enhancement, "start-enhancement": cmd_start_enhancement,
    "close-enhancement": cmd_close_enhancement, "abandon-enhancement": cmd_abandon_enhancement,
    "show-enhancement": cmd_show_enhancement,
    "link-defect": cmd_link_defect, "claim-defect": cmd_claim_defect,
    "unclaim-defect": cmd_unclaim_defect, "close-defect": cmd_close_defect,
    "show-defect": cmd_show_defect,
    "add-note": cmd_add_note, "show-notes": cmd_show_notes,
    "search": cmd_search, "show-feature": cmd_show_feature, "features": cmd_features,
    "check-test-links": cmd_check_test_links,
    # Phase 2 — stories
    "create-story": cmd_create_story, "update-story": cmd_update_story,
    "show-story": cmd_show_story, "stories": cmd_stories,
    # Phase 2 — use cases
    "create-usecase": cmd_create_usecase, "update-usecase": cmd_update_usecase,
    "show-usecase": cmd_show_usecase, "list-usecases": cmd_list_usecases,
    # Phase 2 — features
    "create-feature": cmd_create_feature, "update-feature": cmd_update_feature,
    "retire-feature": cmd_retire_feature, "show-feature-tests": cmd_show_feature_tests,
    # Phase 2 — linking
    "link-test": cmd_link_test, "link-story": cmd_link_story,
    "link-usecase": cmd_link_usecase, "link-feature": cmd_link_feature,
    "link-blocks": cmd_link_blocks, "unlink-blocks": cmd_unlink_blocks,
    "show-blockers": cmd_show_blockers,
    "link-enhancement": cmd_link_enhancement, "unlink-enhancement": cmd_unlink_enhancement,
    "unlink-usecase": cmd_unlink_usecase,
    # Phase 2 — enhancement workflow
    "wontfix-enhancement": cmd_wontfix_enhancement, "defer-enhancement": cmd_defer_enhancement,
    "reopen-enhancement": cmd_reopen_enhancement, "approve-enhancement": cmd_approve_enhancement,
    "list-enhancements": cmd_list_enhancements,
    # Phase 2 — defects
    "list-defects": cmd_list_defects, "update-defect": cmd_update_defect,
    # Phase 2 — ADRs
    "create-adr": cmd_create_adr, "seed-standards": cmd_seed_standards,
    # Phase 2 — analysis
    "coverage-report": cmd_coverage_report, "trace-matrix": cmd_trace_matrix,
    "export": cmd_export, "health-report": cmd_health_report,
    "full-inventory": cmd_full_inventory,
    # Phase 3 — test-run tracking
    "record-run": cmd_record_run, "run-history": cmd_run_history,
    "prune-runs": cmd_prune_runs, "test-status": cmd_test_status,
    # Phase 3 — utility
    "impact": cmd_impact, "link-commit": cmd_link_commit,
    "add-standard": cmd_add_standard, "scan-tests": cmd_scan_tests,
    # Phase 3 — session-driven AI review
    "coverage-review": cmd_coverage_review,
    "quality-review": cmd_quality_review,
    "audit-status": cmd_audit_status,
    # Phase 5 — audit-test, session-close, config, set-external-ref, baseline
    "audit-test": cmd_audit_test, "session-close": cmd_session_close,
    "config": cmd_config, "set-external-ref": cmd_set_external_ref,
    "baseline": cmd_baseline,
    # Phase 6 — defect alias, enhancement update, stale-test pruning, endpoint, reset, mcp, viz
    "create-defect": cmd_link_defect,
    "update-enhancement": cmd_update_enhancement,
    "prune-stale-tests": cmd_prune_stale_tests,
    "link-endpoint": cmd_link_endpoint,
    "reset": cmd_reset,
    # Phase 7 — scenario model
    "expire-note": cmd_expire_note, "scenarios": cmd_scenarios,
    "link-chapter": cmd_link_chapter, "link-scenario-story": cmd_link_scenario_story,
    "link-training": cmd_link_training, "actor-coverage": cmd_actor_coverage,
    # Phase 7 — graph utilities
    "merge-nodes": cmd_merge_nodes, "migrate-binary": cmd_migrate_binary,
    "analyse": cmd_analyse, "changes": cmd_changes, "reconcile-done": cmd_reconcile_done,
    # Phase 7 — session-driven AI
    "enh-align": cmd_enh_align, "uc-state-check": cmd_uc_state_check,
    # Phase 7 — import/export
    "reqif": cmd_reqif, "preview-import": cmd_preview_import,
    "commit-import": cmd_commit_import, "export-neo4j": cmd_export_neo4j,
    # Phase 7 — Go scanning
    "scan-endpoints": cmd_scan_endpoints, "scan-go-tests": cmd_scan_go_tests,
    # Phase 8 — module context + delivery briefing
    "add-module-context": cmd_add_module_context,
    "show-module-context": cmd_show_module_context,
    "deliver-brief": cmd_deliver_brief,
}
assert set(_HANDLERS) == SUPPORTED


def run(cmd: str, args, root: Path) -> None:
    """Run one CLI command against the SQLite store. May sys.exit(1|2)."""
    root = Path(root)
    handler = _HANDLERS.get(cmd)
    if handler is None:
        _err(f"✗ {cmd} needs the Neo4j backend (not available with SYSEDGE_BACKEND=sqlite)")
        sys.exit(1)
    S = None
    try:
        S = Store(db_path(root), create=cmd in _CREATES_DB)
        handler(args, root, S)
    except CommandError as e:
        if str(e):
            _err(str(e))
        sys.exit(e.code)
    except sqlite3.OperationalError as e:
        _err(f"⚠  SQLite database {db_path(root)} unavailable: {e}")
        sys.exit(2)
    except sqlite3.DatabaseError as e:
        _err(f"⚠  SQLite database {db_path(root)} is unreadable: {e}")
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover - unexpected bug
        _err(f"\n✗  {cmd} failed: {e}")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    finally:
        if S is not None:
            S.close()
