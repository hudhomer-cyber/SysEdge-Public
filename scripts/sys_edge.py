"""
sys_edge.py — SysEdge CLI: a requirements-traceability graph for AI coding agents.

Reads/writes Sys* nodes in Neo4j:
  SysUserStory -REALIZED_BY-> SysUseCase -REQUIRES-> SysFeature <-PROVIDES- SysModule
  SysSymbol -IMPLEMENTS-> SysFeature      SysModule -CONTAINS_SYMBOL-> SysSymbol
  SysTestPackage -CONTAINS_TEST-> SysTest -VERIFIES-> SysFeature
  SysArchDecision -ADDRESSES-> SysArchStd

Agents should change the graph only through this CLI (no raw Cypher), so
every write goes through the same validation and safety checks.

Key commands:
    init                            Create Neo4j constraints (run once)
    seed data/sys-backup-XXX.json              Load ALL data (sys-edge only)
    seed data/sys-backup-XXX.json --instance core   Restore only core instance nodes
    seed data/sys-backup-XXX.json --module MOD-p4   Restore only one module's nodes
    briefing --instance framework   Session-start coverage briefing
    record-run --package <path>     Record a pytest run result
    test-status [--instance core]   Show last-run timestamps across packages

    scan-tests --path <file.py>     Register test functions from one file
    scan-tests --all [--yes]        Discover and scan all test files
      [--root p6|backend] [--area p3] [--instance core]

    scan-code --path <dir>          Scan symbols from one directory (additive)
    scan-code --all [--yes]         Scan discovered code roots for symbols
    scan-code --module MOD-<id>     Scan a module's registered codePaths

    link-test  --feature F-FW-001 --file test.py --fn test_login
    link-endpoint --feature F-FW-001 --method GET --path /auth/login --binary core
    link-symbol --feature F-FW-001 --file handlers.go --symbol handleLogin
    link-chapter --chapter SCN-001-CH-02 --feature F-P3-005
    link-training --module TRN-MGR-03 --feature F-FW-004

    features --module MOD-auth      List features for a module
    stories                         List user stories and linked features
    scenarios                       Show scenarios/training decomposition status

    backup [--output path]          Export all Sys* nodes to seed-compatible JSON
"""
import argparse
import ast
import collections
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Project root: the parent of the directory holding this script (cli/.. in a
# package, scripts/.. in this repo), unless SYSEDGE_PROJECT_ROOT says otherwise.
_root = Path(os.environ.get("SYSEDGE_PROJECT_ROOT") or Path(__file__).resolve().parent.parent)
try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env", override=False)
except ImportError:
    pass


def _env_setting(name: str, default: str = "") -> str:
    """SYSEDGE_NEO4J_<name>, falling back to the legacy NEO4J_<name>.

    The legacy names are shared with application databases, so using them
    prints a one-line notice (silence with SYSEDGE_LEGACY_ENV_OK=1).
    """
    value = os.environ.get(f"SYSEDGE_NEO4J_{name}")
    if value:
        return value
    legacy = os.environ.get(f"NEO4J_{name}")
    if legacy:
        if not os.environ.get("SYSEDGE_LEGACY_ENV_OK") and name not in _LEGACY_WARNED:
            _LEGACY_WARNED.add(name)
            print(f"  ℹ  using NEO4J_{name}; set SYSEDGE_NEO4J_{name} so SysEdge never shares "
                  f"the application's database settings", file=sys.stderr)
        return legacy
    return default


_LEGACY_WARNED: set = set()


def _neo4j_config() -> tuple:
    """(uri, user, password), resolved when a connection is first needed."""
    pw = _env_setting("PASSWORD")
    if not pw:
        print("ERROR: Neo4j password not configured — set SYSEDGE_NEO4J_PASSWORD "
              f"(in {_root / '.env'} or the environment).", file=sys.stderr)
        sys.exit(2)
    return (_env_setting("URI", "bolt://localhost:7688"), _env_setting("USER", "neo4j"), pw)


def _neo4j_uri() -> str:
    return os.environ.get("SYSEDGE_NEO4J_URI") or os.environ.get("NEO4J_URI") or "bolt://localhost:7688"

# Patterns that indicate a destructive operation on the Sys graph
_DELETE_PATTERN = __import__("re").compile(
    r"\b(DETACH\s+DELETE|DELETE)\b", __import__("re").IGNORECASE
)


class _SafeSession:
    """Wraps a Neo4j session and refuses any query containing DELETE on Sys* nodes.

    Property REMOVE is allowed (it clears a field, it doesn't delete data).

    Community edition cannot deny DELETE at the database level, so we enforce
    it here. The reset command uses the raw admin driver explicitly.

    Note: __enter__/__exit__ intentionally not implemented — the context manager
    lifecycle is managed by _SafeDriver.session()'s _Ctx class. This avoids
    double-close issues that caused silent batch failures.
    """
    def __init__(self, session):
        self._s = session

    def run(self, query, **kwargs):
        if _DELETE_PATTERN.search(query):
            raise PermissionError(
                "⛔  DELETE operations are not permitted via the instance driver.\n"
                "    The graph is shared across all sessions — only the user can authorise deletes.\n"
                "    Use: python3 scripts/sys_edge.py close-defect (sets status, does not delete)\n"
                "    or ask the user to run the operation."
            )
        return self._s.run(query, **kwargs)

    def begin_transaction(self, **kwargs):
        return _SafeTransaction(self._s.begin_transaction(**kwargs))


class _SafeTransaction(_SafeSession):
    """Explicit transaction with the same DELETE guard as _SafeSession."""
    def commit(self):
        return self._s.commit()

    def rollback(self):
        return self._s.rollback()


class _SafeDriver:
    """Wraps a Neo4j driver and returns _SafeSessions via a clean context manager."""
    def __init__(self, driver):
        self._d = driver

    def session(self, **kwargs):
        # _Ctx owns the raw session lifecycle; _SafeSession only intercepts run()
        outer = self
        class _Ctx:
            def __enter__(_self):
                _self._raw = outer._d.session(**kwargs)
                _self._raw.__enter__()
                return _SafeSession(_self._raw)
            def __exit__(_self, *a):
                return _self._raw.__exit__(*a)
        return _Ctx()

    def close(self): self._d.close()


# ── Driver ──────────────────────────────────────────────────────────────────

def _driver(admin: bool = False):
    """Return a Neo4j driver.

    By default returns a safe driver that refuses DELETE statements.
    Pass admin=True only for commands that explicitly need delete access
    (reset, full restore, prune-*, merge-nodes, check-test-links --fix).
    """
    try:
        from neo4j import GraphDatabase, warnings as neo4j_warnings
        import warnings
        warnings.filterwarnings("ignore", category=neo4j_warnings.Neo4jWarning
                                if hasattr(neo4j_warnings, "Neo4jWarning") else Warning)
    except ImportError:
        print("ERROR: pip install neo4j", file=sys.stderr)
        sys.exit(1)
    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    # Community edition has no RBAC, so every connection uses the same
    # credentials. _SafeDriver is a guard against accidental deletes in CLI
    # code, not a security boundary.
    uri, user, pw = _neo4j_config()
    raw = GraphDatabase.driver(
        uri, auth=(user, pw),
        connection_timeout=5,
        max_transaction_retry_time=10,
    )
    return raw if admin else _SafeDriver(raw)


def _validate_instance(instance: str, session) -> None:
    """Raise SystemExit with a helpful list if `instance` is not a known SysModule instance.

    Skips the check for 'master' (always valid) and when instance is empty.
    """
    if not instance or instance == "master":
        return
    known = sorted({
        r["inst"] for r in session.run(
            "MATCH (m:SysModule) WHERE m.instance IS NOT NULL "
            "RETURN DISTINCT m.instance AS inst ORDER BY inst"
        ).data()
        if r["inst"]
    })
    if instance not in known:
        print(f"ERROR: unknown instance '{instance}'", file=sys.stderr)
        if known:
            cols = 4
            rows = [known[i:i+cols] for i in range(0, len(known), cols)]
            print(f"Known instances:", file=sys.stderr)
            for row in rows:
                print(f"  {', '.join(f'{v:<18}' for v in row)}", file=sys.stderr)
        else:
            print("  (no SysModule nodes found — run seed or init first)", file=sys.stderr)
        sys.exit(1)


# ── Release version ───────────────────────────────────────────────────────────
VERSION = "1.4.0"

# ── Schema version ────────────────────────────────────────────────────────────
# Increment when a new Sys* label or required property is added.
# Backups include this version; seed refuses forward-incompatible restores.
SCHEMA_VERSION = 3  # v3: backups carry generic `_relationships`


# ── ID generation: retry on constraint collision ──────────────────────────────

# Kit-only commands (one-time per repository licence). make_lite.py reads this
# set to build the free CLI, so it is the single source of truth for the split.
# quality-review is free; only its --ai mode is gated (inside the command).
PREMIUM_COMMANDS = {
    "export", "preview-import", "commit-import",
    "audit-test", "coverage-review",
    "analyse", "merge-nodes",
    "seed-standards", "create-adr",
    "baseline", "trace-matrix", "reqif",
}

EXIT_NOT_LICENSED = 3


def _load_licence_module():
    """licence.py from this script's directory (Kit only), or None."""
    import importlib.util as _ilu
    if os.environ.get("SYSEDGE_DEV") == "1":
        return None
    lic_path = Path(os.path.dirname(os.path.abspath(__file__))) / "licence.py"
    if not lic_path.exists():
        return None
    spec = _ilu.spec_from_file_location("licence", lic_path)
    lic = _ilu.module_from_spec(spec)
    spec.loader.exec_module(lic)
    return lic


def _require_licence(command: str = "") -> None:
    """No-op in 1.4.0 — all commands are freely available."""


# Model aliases → provider-specific model IDs
_AI_MODELS = {
    "claude": {
        "haiku":  "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-6",
    },
    "gemini": {
        "haiku":  "gemini-2.5-flash",   # fast/cheap equivalent
        "sonnet": "gemini-2.5-pro",     # capable equivalent
    },
    "qwen": {
        "haiku":  "qwen-plus",          # fast/cheap equivalent
        "sonnet": "qwen-max",           # capable equivalent
    },
    "kimi": {
        "haiku":  "moonshot-v1-8k",     # fast/cheap context
        "sonnet": "kimi-k2-0711-preview",  # capable reasoning model
    },
}


_AI_NOTICE_SHOWN: list = []


def _ai_debug(err: Exception) -> None:
    """Provider failures fall through to the next provider; show why with SYSEDGE_DEBUG=1."""
    if os.environ.get("SYSEDGE_DEBUG"):
        print(f"  [ai] provider failed: {type(err).__name__}: {err}", file=sys.stderr)


def _ai_model(provider: str, alias: str) -> str:
    """Model id for a provider and alias; override with SYSEDGE_AI_MODEL_<PROVIDER>_<ALIAS>."""
    override = os.environ.get(f"SYSEDGE_AI_MODEL_{provider.upper()}_{alias.upper()}")
    if override:
        return override
    models = _AI_MODELS.get(provider, {})
    return models.get(alias) or models.get("haiku", "")


def _ai_call(prompt: str, model: str = "haiku", max_tokens: int = 2048,
             provider: str = "") -> str:
    """Send a prompt to an available AI provider and return the text response.

    Provider priority (first allowed wins):
      1. claude CLI  — Claude Code session tokens (no API key needed)
      2. kimi CLI    — Kimi Code CLI session tokens (no API key needed)
      3. gemini CLI  — Gemini CLI session tokens (free tier: 1,000 req/day)
      4. qwen CLI    — Qwen Code session tokens (DashScope or local Ollama)
      5. codex exec  — OpenAI Codex CLI (ChatGPT subscription or OPENAI_API_KEY)
      6. ANTHROPIC_API_KEY  — direct Anthropic API
      7. GEMINI_API_KEY     — direct Google Generative AI API
      8. OPENAI_API_KEY     — direct OpenAI API
      9. DASHSCOPE_API_KEY  — direct Alibaba DashScope API (Qwen)

    Data disclosure: the prompt contains graph content (UC descriptions, test file
    paths, architecture standards). It is sent to whichever provider is used.
    Restrict providers via SYSEDGE_AI_PROVIDERS env var or --provider flag.

    Raises RuntimeError if no provider is available or allow-list is exhausted.
    """
    import subprocess as _sp
    import os as _os

    # Build allow-set: --provider flag overrides env var; env var overrides default (all)
    env_allow = _os.environ.get("SYSEDGE_AI_PROVIDERS", "").strip()
    if provider:
        allow = {provider.lower()}
    elif env_allow:
        allow = {p.strip().lower() for p in env_allow.split(",") if p.strip()}
    else:
        allow = None  # None = all providers allowed

    def _allowed(name: str) -> bool:
        return allow is None or name in allow

    if allow is None and not _AI_NOTICE_SHOWN:
        _AI_NOTICE_SHOWN.append(True)
        print("  ℹ  AI mode sends graph content to the first available provider "
              "(claude, kimi, gemini, qwen, codex CLIs, then API keys). "
              "Restrict with SYSEDGE_AI_PROVIDERS=claude (or --provider).", file=sys.stderr)

    claude_model  = _ai_model("claude", model)
    gemini_model  = _ai_model("gemini", model)
    qwen_model    = _ai_model("qwen", model)
    kimi_model    = _ai_model("kimi", model)

    # ── 1. claude CLI (Claude Code session tokens) ─────────────────────────
    if _allowed("claude"):
        try:
            r = _sp.run(
                ["claude", "-p", prompt, "--output-format", "json", "--model", claude_model],
                capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir(),
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                text = data.get("result", "")
                if text:
                    print("[provider: claude]", file=sys.stderr)
                    return text
        except Exception as e:
            _ai_debug(e)

    # ── 2. kimi CLI (Kimi Code CLI session tokens) ─────────────────────────
    # Uses the default_model configured in the user's ~/.kimi-code/config.toml.
    # No API key is required; tokens are drawn from the active Kimi Code session.
    if _allowed("kimi"):
        kimi_bin = _os.environ.get("KIMI_CODE_BIN", "kimi")
        try:
            r = _sp.run(
                [kimi_bin, "-p", prompt, "--output-format", "stream-json"],
                capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir(),
            )
            if r.returncode == 0 and r.stdout.strip():
                for line in r.stdout.strip().splitlines():
                    try:
                        ev = json.loads(line)
                        if ev.get("role") == "assistant":
                            text = ev.get("content", "")
                            if text:
                                print("[provider: kimi]", file=sys.stderr)
                                return text
                    except Exception:
                        continue
        except Exception as e:
            _ai_debug(e)

    # ── 3. gemini CLI (Gemini CLI session tokens — free 1,000 req/day) ──────
    if _allowed("gemini"):
        try:
            r = _sp.run(
                ["gemini", "-p", prompt, "--output-format", "json", "--model", gemini_model],
                capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir(),
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                text = data.get("response", "")  # Gemini uses "response" not "result"
                if text:
                    print("[provider: gemini]", file=sys.stderr)
                    return text
        except Exception as e:
            _ai_debug(e)

    # ── 3. qwen CLI (Qwen Code — identical interface to Gemini CLI) ──────────
    if _allowed("qwen"):
        try:
            r = _sp.run(
                ["qwen", "-p", prompt, "--output-format", "json", "--model", qwen_model],
                capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir(),
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                text = data.get("response", "")
                if text:
                    print("[provider: qwen]", file=sys.stderr)
                    return text
        except Exception as e:
            _ai_debug(e)

    # ── 4. codex exec (OpenAI Codex CLI — JSONL stream) ─────────────────────
    if _allowed("codex"):
        try:
            r = _sp.run(
                ["codex", "exec", prompt, "--json"],
                capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir(),
            )
            if r.returncode == 0 and r.stdout.strip():
                # JSONL stream — find the last agent_message item
                for line in reversed(r.stdout.strip().splitlines()):
                    try:
                        ev = json.loads(line)
                        if (ev.get("type") == "item.completed"
                                and ev.get("item", {}).get("type") == "agent_message"):
                            text = ev["item"].get("text", "")
                            if text:
                                print("[provider: codex]", file=sys.stderr)
                                return text
                    except Exception:
                        continue
        except Exception as e:
            _ai_debug(e)

    # ── 5. ANTHROPIC_API_KEY — direct Anthropic API ──────────────────────────
    if _allowed("anthropic") and _os.environ.get("ANTHROPIC_API_KEY", ""):
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=_os.environ["ANTHROPIC_API_KEY"])
            resp = client.messages.create(
                model=claude_model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            print("[provider: anthropic]", file=sys.stderr)
            return resp.content[0].text.strip()
        except Exception as e:
            _ai_debug(e)

    # ── 6. GEMINI_API_KEY — direct Google Generative AI API ──────────────────
    if _allowed("gemini-api") and _os.environ.get("GEMINI_API_KEY", ""):
        try:
            import google.generativeai as _genai
            _genai.configure(api_key=_os.environ["GEMINI_API_KEY"])
            m = _genai.GenerativeModel(gemini_model)
            resp = m.generate_content(prompt)
            print("[provider: gemini-api]", file=sys.stderr)
            return resp.text.strip()
        except Exception as e:
            _ai_debug(e)

    # ── 7. OPENAI_API_KEY — direct OpenAI API ────────────────────────────────
    if _allowed("openai") and _os.environ.get("OPENAI_API_KEY", ""):
        try:
            import openai as _openai
            client = _openai.OpenAI(api_key=_os.environ["OPENAI_API_KEY"])
            resp = client.chat.completions.create(
                model="gpt-4o-mini" if model == "haiku" else "gpt-4o",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            print("[provider: openai]", file=sys.stderr)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            _ai_debug(e)

    # ── 8. DASHSCOPE_API_KEY — Alibaba Qwen via OpenAI-compatible endpoint ───
    if _allowed("dashscope") and _os.environ.get("DASHSCOPE_API_KEY", ""):
        try:
            import openai as _openai
            client = _openai.OpenAI(
                api_key=_os.environ["DASHSCOPE_API_KEY"],
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            resp = client.chat.completions.create(
                model=qwen_model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            print("[provider: dashscope]", file=sys.stderr)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            _ai_debug(e)

    allowed_str = f" (allowed: {', '.join(sorted(allow))})" if allow else ""
    raise RuntimeError(
        f"No AI provider available{allowed_str}.\n"
        "  To restrict providers: SYSEDGE_AI_PROVIDERS=claude,kimi (or --provider kimi)\n"
        "  Data disclosure: graph content (UC descriptions, test paths, arch standards)\n"
        "    is sent to whichever provider responds first.\n"
        "  Session tokens (no key needed):\n"
        "    - Run inside Claude Code   (claude CLI)\n"
        "    - Run inside Kimi Code     (kimi CLI)\n"
        "    - Run inside Gemini CLI    (gemini CLI — free 1,000 req/day)\n"
        "    - Run inside Qwen Code     (qwen CLI)\n"
        "    - Run inside OpenAI Codex  (codex CLI)\n"
        "  API keys (.env file):\n"
        "    - ANTHROPIC_API_KEY        (Anthropic — pip install anthropic)\n"
        "    - GEMINI_API_KEY           (Google — pip install google-generativeai)\n"
        "    - OPENAI_API_KEY           (OpenAI — pip install openai)\n"
        "    - DASHSCOPE_API_KEY        (Qwen/Alibaba — pip install openai)"
    )


def _alloc_id(session, label: str, prefix: str, pad: int = 0,
              init_props: dict = None) -> str:
    """Allocate the next sequential id for a Sys* node, retrying on collision.

    Uses a read-then-create pattern. If two sessions race and both get the same
    max, the second CREATE will fail on the uniqueness constraint. We catch
    that and retry with max+1 until we succeed. This is safe because the
    constraint guarantees no duplicate ids.

    init_props: extra properties written atomically in the same CREATE so the
    node is never visible in a bare/schema-less state (prevents reconcile_done
    race where a bare node has no status and gets picked up as a gap).
    """
    from neo4j.exceptions import ClientError as _NeoClientError
    max_attempts = 10
    extra = {k: v for k, v in (init_props or {}).items()}
    for attempt in range(max_attempts):
        result = session.run(
            f"MATCH (n:{label}) WHERE n.id STARTS WITH $pfx "
            f"RETURN max(toInteger(substring(n.id, $skip))) AS mx",
            pfx=prefix, skip=len(prefix)
        ).single()
        nxt = (result["mx"] or 0) + 1
        candidate = f"{prefix}{nxt:0{pad}d}" if pad else f"{prefix}{nxt}"
        props = {"id": candidate, **extra}
        try:
            session.run(f"CREATE (n:{label} $props) RETURN n.id", props=props).single()
            return candidate
        except _NeoClientError as e:
            if "ConstraintValidation" in str(e) and attempt < max_attempts - 1:
                continue   # another session won this id — retry
            raise
    raise RuntimeError(f"Could not allocate id for {label} after {max_attempts} attempts")


# ── Init: constraints ────────────────────────────────────────────────────────

CONSTRAINTS = [
    ("sys_system_id",    "SysSystem",          "id"),
    ("sys_domain_id",    "SysDomain",          "id"),
    ("sys_module_id",    "SysModule",          "id"),
    ("sys_feature_id",   "SysFeature",         "id"),
    ("sys_story_id",     "SysUserStory",       "id"),
    ("sys_archstd_id",   "SysArchStd",         "id"),
    ("sys_test_id",      "SysTest",            "id"),
    ("sys_endpoint_id",  "SysEndpoint",        "id"),
    ("sys_symbol_id",    "SysSymbol",          "id"),
    ("sys_scenario_id",  "SysScenario",        "id"),
    ("sys_scn_ch_id",    "SysScenarioChapter", "id"),
    ("sys_trn_prog_id",  "SysTrainingProgram", "id"),
    ("sys_trn_mod_id",   "SysTrainingModule",  "id"),
    ("sys_test_pkg_id",  "SysTestPackage",     "id"),
    ("sys_test_run_id",  "SysTestRun",         "id"),
    ("sys_user_id",      "SysUser",            "id"),
    ("sys_app_id",       "SysApplication",     "id"),
    ("sys_defect_id",    "SysDefect",          "id"),
    ("sys_enhance_id",   "SysEnhancement",     "id"),
    ("sys_usecase_id",   "SysUseCase",         "id"),
    ("sys_dircat_id",    "SysDirCategory",     "id"),
    ("sys_feedback_id",  "SysFeedback",        "id"),
    ("sys_proposal_id",  "SysProposal",        "id"),
    ("sys_note_id",      "SysNote",            "id"),
    ("sys_adr_id",       "SysArchDecision",    "id"),
    ("sys_docart_id",    "SysDocArtifact",     "id"),
    ("sys_schema_id",    "SysSchema",          "id"),
    ("sys_change_id",    "SysChange",          "id"),
    ("sys_baseline_id",  "SysBaseline",        "id"),
]

# Property indexes for the hot filters (instance/status scoping, file lookups,
# run history ordering).
INDEXES = [
    ("sys_module_instance", "SysModule",      "instance"),
    ("sys_enh_instance",    "SysEnhancement", "instance"),
    ("sys_enh_status",      "SysEnhancement", "status"),
    ("sys_enh_priority",    "SysEnhancement", "priority"),
    ("sys_defect_instance", "SysDefect",      "instance"),
    ("sys_defect_status",   "SysDefect",      "status"),
    ("sys_defect_closed",   "SysDefect",      "closedAt"),    # FB-404: AND closedAt IS NULL filter
    ("sys_usecase_inst",    "SysUseCase",     "instance"),
    ("sys_feature_status",  "SysFeature",     "status"),
    ("sys_test_file",       "SysTest",        "file"),
    ("sys_test_repo",       "SysTest",        "repo"),
    ("sys_symbol_file",     "SysSymbol",      "file"),
    ("sys_testrun_ranat",   "SysTestRun",     "ranAt"),
    ("sys_change_entity",   "SysChange",      "entityId"),
    ("sys_change_seq",      "SysChange",      "seq"),
    ("sys_feedback_inst",   "SysFeedback",    "instance"),
]

# Composite RANGE indexes for hot filter properties (not uniqueness — just lookup speed).
# Every worklog/briefing/full-inventory scans these labels by instance+status.
# NOTE: names must not overlap with INDEXES entries above.
# Legacy names that exist in the graph under old names are listed in LEGACY_INDEXES
# so cmd_init can drop them on next run (avoids silent duplicates).
RANGE_INDEXES = [
    ("sys_enh_inst_stat",   "SysEnhancement", ["instance", "status"]),
    ("sys_def_inst_stat",   "SysDefect",      ["instance", "status"]),
    ("sys_def_stat_closed", "SysDefect",      ["status", "closedAt"]),  # open-defect compound filter
    ("sys_test_type_file",  "SysTest",        ["testType", "file"]),
]

# Old index names replaced by entries above — dropped silently on init.
LEGACY_INDEXES = [
    "sys_enh_instance_status",   # → sys_enh_inst_stat
    "sys_def_instance_status",   # → sys_def_inst_stat
    "sys_feedback_instance",     # moved to INDEXES as sys_feedback_inst
    "sys_test_type",             # → sys_test_type_file (composite)
]

def cmd_init(args):
    drv = _driver()
    with drv.session() as s:
        failed = []
        for name, label, prop in CONSTRAINTS:
            try:
                s.run(
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                )
            except Exception as e:  # existing duplicates block a new constraint
                if "ServiceUnavailable" in type(e).__name__:
                    raise
                failed.append(label)
        for name, label, prop in INDEXES:
            s.run(f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})")
        # Drop legacy names before creating replacements (avoids duplicate-property conflicts)
        for old_name in LEGACY_INDEXES:
            try:
                s.run(f"DROP INDEX {old_name} IF EXISTS")
            except Exception:
                pass  # index may already be absent; non-fatal
        for name, label, props in RANGE_INDEXES:
            cols = ", ".join(f"n.{p}" for p in props)
            s.run(f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON ({cols})")
        for label in failed:
            dups = s.run(f"MATCH (n:{label}) WITH n.id AS id, count(*) AS c WHERE c > 1 "
                         f"RETURN id, c ORDER BY id LIMIT 10").data()
            listed = ", ".join(f"{d['id']} ×{d['c']}" for d in dups) or "unknown"
            print(f"  ⚠  constraint on {label}.id not created — duplicate ids: {listed}"
                  f"  (merge them with merge-nodes, then re-run init)", file=sys.stderr)
        # Schema singleton — version marker for backup compatibility
        s.run("""
            MERGE (s:SysSchema {id:'schema'})
            ON CREATE SET s.version=$v, s.createdAt=$now
            ON MATCH SET s.version=$v
        """, v=SCHEMA_VERSION, now=_now_iso())
        print(f"✓ {len(CONSTRAINTS) - len(failed)} constraints · {len(INDEXES)} indexes · "
              f"{len(RANGE_INDEXES)} composite indexes · schema v{SCHEMA_VERSION}")
        # A brand-new project starts in lightweight mode (stories/use cases optional)
        if not (_root / "sysedge.yaml").exists() and _sys_node_count(s) == 0:
            _set_config_value("model", "lightweight")
            print("✓ sysedge.yaml created with model: lightweight "
                  "(stories and use cases optional; `config set model full` for the full V-model)")
    drv.close()


# ── Seed from JSON ────────────────────────────────────────────────────────────

def _filter_seed_data(data: dict, instance: str = "", module_id: str = "") -> dict:
    """
    Narrow a full backup dict to only nodes/edges belonging to the
    requested instance or module.  Global structural nodes (systems,
    domains, arch standards/decisions, scenarios, training, users,
    applications, dirCategories, feedback) are SKIPPED — they are
    managed by sys-edge/master/architect and must not be overwritten
    by an instance-scoped re-seed.

    Returns a new data dict containing only the filtered subset.
    """
    if not instance and not module_id:
        return data  # no filter — full seed

    # 1. Which modules are in scope?
    all_mods = data.get("modules", [])
    if module_id:
        scope_mods = [m for m in all_mods if m["id"] == module_id]
    else:
        scope_mods = [m for m in all_mods if m.get("instance") == instance]

    scope_mod_ids = {m["id"] for m in scope_mods}

    # 2. Which features belong to those modules?
    scope_feats = [f for f in data.get("features", [])
                   if f.get("moduleId") in scope_mod_ids or f.get("parentId") in
                   {f2["id"] for f2 in data.get("features",[]) if f2.get("moduleId") in scope_mod_ids}]
    scope_feat_ids = {f["id"] for f in scope_feats}

    # 3. Enhancements, defects, proposals, and notes for this instance/module scope
    if instance:
        scope_enhs  = [e for e in data.get("enhancements", []) if e.get("instance") == instance]
        scope_defs  = [d for d in data.get("defects",      []) if d.get("instance") == instance]
        scope_props = [p for p in data.get("proposals",    []) if p.get("instance") == instance]
        scope_notes = [n for n in data.get("notes",        []) if n.get("instance") == instance]
    elif module_id:
        # Module-scoped restore: include items that touch scope features
        scope_enhs  = [e for e in data.get("enhancements", [])
                       if any(fid in scope_feat_ids for fid in e.get("extendsFeatures", []))]
        scope_defs  = [d for d in data.get("defects",      [])
                       if any(fid in scope_feat_ids for fid in d.get("affectsFeatures", []))]
        scope_props = [p for p in data.get("proposals",    [])
                       if any(fid in scope_feat_ids for fid in p.get("extendsFeatures", []))]
        scope_notes = []
    else:
        scope_enhs = scope_defs = scope_props = scope_notes = []

    # 4. Tests, endpoints, symbols linked to scope features
    scope_tests = [t for t in data.get("tests", [])
                   if any(fid in scope_feat_ids for fid in t.get("verifiesFeatures", []))]
    scope_pkgs  = [p for p in data.get("testPackages", [])
                   if any(t.get("packageId") == p["id"] for t in scope_tests)]
    scope_eps   = [e for e in data.get("endpoints", [])
                   if e.get("implementsFeature") in scope_feat_ids]
    scope_syms  = [s for s in data.get("symbols", [])
                   if s.get("moduleId") in scope_mod_ids or s.get("implementsFeature") in scope_feat_ids]

    # 5. Use cases that REQUIRES scope features
    scope_ucs   = [uc for uc in data.get("useCases", [])
                   if any(fid in scope_feat_ids for fid in uc.get("requiresFeatures", []))]

    # Story→feature REQUIRES edges — include stories that link to scope features
    # (FB-111: these were being lost on every per-instance restore)
    scope_stories = [us for us in data.get("userStories", [])
                     if any(fid in scope_feat_ids for fid in us.get("requiresFeatures", []))]

    filtered = {
        "modules":      scope_mods,
        "features":     scope_feats,
        "enhancements": scope_enhs,
        "defects":      scope_defs,
        "proposals":    scope_props,
        "notes":        scope_notes,
        "tests":        scope_tests,
        "testPackages": scope_pkgs,
        "endpoints":    scope_eps,
        "symbols":      scope_syms,
        "useCases":     scope_ucs,
        "userStories":  scope_stories,  # FB-111: story→feature REQUIRES edges included
        # Global infrastructure — always included so briefing works after restore
        # (FB-112: SysSystem/SysDomain are required for module→domain→system links)
        "systems":  data.get("systems", []),
        "domains":  data.get("domains", []),
        # True globals excluded — managed by dedicated instances
        "archStandards": [], "archDecisions": [], "scenarios": [],
        "trainingPrograms": [], "users": [], "applications": [],
        "dirCategories": [], "feedback": [], "usesPattern": [], "prerequisiteFor": [],
    }

    scope_desc = f"instance={instance}" if instance else f"module={module_id}"
    print(f"  [filtered seed: {scope_desc}]")
    print(f"  modules={len(scope_mods)}  features={len(scope_feats)}  "
          f"enhancements={len(scope_enhs)}  defects={len(scope_defs)}")
    print(f"  tests={len(scope_tests)}  endpoints={len(scope_eps)}  symbols={len(scope_syms)}")
    return filtered


_SYS_LABEL_RE = re.compile(r"^Sys[A-Za-z]+$")
_REL_TYPE_RE  = re.compile(r"^[A-Z][A-Z_]*$")


def _sys_labels(session) -> list:
    """Every Sys* label present in the database (validated, safe to interpolate)."""
    rows = session.run("CALL db.labels() YIELD label RETURN label").data()
    return sorted(r["label"] for r in rows if _SYS_LABEL_RE.match(r["label"] or ""))


def _sys_node_count(session) -> int:
    """Number of SysEdge nodes, excluding the SysSchema singleton."""
    row = session.run("""
        MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'Sys') AND NOT n:SysSchema
        RETURN count(n) AS c
    """).single()
    return row["c"] if row else 0


def _wipe_sys_nodes(session) -> int:
    """Delete every Sys* node except SysSchema, label by label, in batches.

    Only SysEdge labels are touched, so a database shared with an application
    keeps the application's data.
    """
    wiped = 0
    for label in _sys_labels(session):
        if label == "SysSchema":
            continue
        row = session.run(f"MATCH (n:`{label}`) RETURN count(n) AS c").single()
        n = row["c"] if row else 0
        if n:
            session.run(f"MATCH (n:`{label}`) CALL {{ WITH n DETACH DELETE n }} "
                        f"IN TRANSACTIONS OF 10000 ROWS")
            wiped += n
    return wiped


def _restore_relationships(session, rels: list, keep_ids: set | None = None) -> int:
    """Re-create backed-up relationships between Sys* nodes (MERGE, idempotent).

    With keep_ids (a filtered seed), only relationships touching a seeded node
    are restored.
    """
    groups: dict = {}
    for r in rels or []:
        if keep_ids is not None and r.get("from") not in keep_ids and r.get("to") not in keep_ids:
            continue
        key = (r.get("fromLabel"), r.get("type"), r.get("toLabel"))
        if not (_SYS_LABEL_RE.match(key[0] or "") and _REL_TYPE_RE.match(key[1] or "")
                and _SYS_LABEL_RE.match(key[2] or "")):
            continue
        groups.setdefault(key, []).append(
            {"f": r["from"], "t": r["to"], "p": r.get("props") or {}})
    restored = 0
    for (fl, rt, tl), rows in groups.items():
        for i in range(0, len(rows), 5000):
            session.run(f"""
                UNWIND $rows AS row
                MATCH (a:`{fl}` {{id: row.f}}), (b:`{tl}` {{id: row.t}})
                MERGE (a)-[r:`{rt}`]->(b)
                SET r += row.p
            """, rows=rows[i:i + 5000])
        restored += len(rows)
    return restored


def cmd_seed(args):
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)

    # Unfiltered seed touches ALL instances — require --instance or --full-restore
    filter_instance = getattr(args, "instance", "") or ""
    filter_module   = getattr(args, "module",   "") or ""
    full_restore    = getattr(args, "full_restore", False)

    if full_restore and (filter_instance or filter_module):
        print("ERROR: --full-restore cannot be combined with --instance or --module",
              file=sys.stderr)
        sys.exit(1)

    first_run = False
    if not filter_instance and not filter_module and not full_restore:
        # An unfiltered seed into an empty graph is the normal first run.
        _probe = _driver()
        with _probe.session() as _s:
            first_run = _sys_node_count(_s) == 0
        _probe.close()
    if not filter_instance and not filter_module and not full_restore and not first_run:
        # Find the most recent backup to suggest (FB-128)
        try:
            backups = sorted(Path("data").glob("sys-backup-*.json"), reverse=True)
            latest  = backups[0].name if backups else "<backup>.json"
        except Exception:
            latest = "<backup>.json"
        print("⛔  STOP — unfiltered seed overwrites ALL instances' data.", file=sys.stderr)
        print("", file=sys.stderr)
        print("    To restore only YOUR session's nodes (safe):", file=sys.stderr)
        print(f"      sys_edge.py seed data/{latest} --instance <your-instance>", file=sys.stderr)
        print("", file=sys.stderr)
        print("    If you own the graph and need a full restore:", file=sys.stderr)
        print(f"      sys_edge.py seed data/{latest} --full-restore", file=sys.stderr)
        print("", file=sys.stderr)
        sys.exit(1)

    if full_restore:
        print("  [full-restore mode — all instances will be overwritten]", file=sys.stderr)
    elif first_run:
        print("  [first run — graph is empty, seeding everything]", file=sys.stderr)

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_relationships = data.get("_relationships") or []

    # Schema version compatibility check — refuse to restore a backup that is
    # newer than this CLI knows about (it may contain node types we'd silently drop).
    backup_version = data.get("_schema_version", 0)
    if backup_version > SCHEMA_VERSION:
        print(f"⛔  Backup schema version {backup_version} is newer than this CLI "
              f"(schema v{SCHEMA_VERSION}).", file=sys.stderr)
        print(f"    Upgrade sys_edge.py before restoring this backup.", file=sys.stderr)
        sys.exit(1)

    data = _filter_seed_data(data,
                             instance=getattr(args, "instance", "") or "",
                             module_id=getattr(args, "module", "") or "")

    # Dry-run: show counts of what would be seeded without writing
    if getattr(args, "dry_run", False):
        print(f"\n  [DRY RUN — nothing written]\n")
        total = 0
        for k, v in sorted(data.items()):
            if isinstance(v, list) and v:
                print(f"  {k:<25} {len(v):6d}  (would seed)")
                total += len(v)
        if raw_relationships:
            print(f"  {'_relationships':<25} {len(raw_relationships):6d}  (would restore)")
        print(f"\n  total: {total} items")
        print(f"\n  Remove --dry-run to apply.\n")
        return

    # Auto-backup before any seed so the pre-seed state is always recoverable
    # (after the dry-run check: a dry run writes nothing).
    if not getattr(args, "no_backup", False) and not first_run:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        pre_path = Path(f"data/sys-preseed-{ts}.json")
        print(f"  [pre-seed backup → {pre_path}]")
        if not _do_backup(pre_path, quiet=True):
            print("  [pre-seed backup skipped — graph has no SysEdge nodes]")

    # Full restore is an explicit destructive operation: use the admin driver
    # so the pre-seed wipe can DETACH DELETE all Sys* nodes.
    drv  = _driver(admin=True) if full_restore else _driver()
    counts = {}
    with drv.session() as s:
        # Full restore: remove every Sys* node (and their edges) before re-seeding.
        # Only Sys* labels are deleted, so an application sharing the database
        # keeps its data. The SysSchema singleton created by `init` survives.
        if full_restore:
            other = s.run("""
                MATCH (n) WHERE NONE(l IN labels(n) WHERE l STARTS WITH 'Sys')
                RETURN count(n) AS c
            """).single()
            if other and other["c"]:
                print(f"  [{other['c']} non-SysEdge nodes in this database are left untouched]",
                      file=sys.stderr)
            counts["wiped"] = _wipe_sys_nodes(s)

        # Everything below runs in one transaction: a failure part-way leaves
        # the graph as it was (after a full-restore wipe, restore from the
        # pre-seed backup) instead of half-seeded. It is also far faster than
        # one auto-commit round trip per statement.
        tx = s.begin_transaction()
        s = tx

        # Snapshot existing VERIFIES/IMPLEMENTS edges so seed never destroys live links
        _pre_verifies = s.run(
            "MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature) RETURN t.id AS tid, f.id AS fid"
        ).data()
        _pre_impl_sym = s.run(
            "MATCH (sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature) RETURN sym.id AS sid, f.id AS fid"
        ).data()
        _pre_impl_ep = s.run(
            "MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature) RETURN ep.id AS eid, f.id AS fid"
        ).data()

        # System
        for node in data.get("systems", []):
            s.run("MERGE (n:SysSystem {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["systems"] = counts.get("systems", 0) + 1

        # Domains
        for node in data.get("domains", []):
            s.run("MERGE (n:SysDomain {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            if "systemId" in node:
                s.run("""MATCH (sys:SysSystem {id:$sid}), (d:SysDomain {id:$did})
                         MERGE (sys)-[:CONTAINS]->(d)""",
                      sid=node["systemId"], did=node["id"])
            counts["domains"] = counts.get("domains", 0) + 1

        # Modules
        for node in data.get("modules", []):
            s.run("MERGE (n:SysModule {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            if "domainId" in node:
                s.run("""MATCH (d:SysDomain {id:$did}), (m:SysModule {id:$mid})
                         MERGE (d)-[:CONTAINS]->(m)""",
                      did=node["domainId"], mid=node["id"])
            counts["modules"] = counts.get("modules", 0) + 1

        # Features
        for node in data.get("features", []):
            s.run("MERGE (n:SysFeature {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("moduleId","parentId")})
            if "moduleId" in node:
                s.run("""MATCH (m:SysModule {id:$mid}), (f:SysFeature {id:$fid})
                         MERGE (m)-[:PROVIDES]->(f)""",
                      mid=node["moduleId"], fid=node["id"])
            if "parentId" in node:
                s.run("""MATCH (p:SysFeature {id:$pid}), (f:SysFeature {id:$fid})
                         MERGE (p)-[:CONTAINS]->(f)""",
                      pid=node["parentId"], fid=node["id"])
            counts["features"] = counts.get("features", 0) + 1

        # User stories
        for node in data.get("userStories", []):
            s.run("MERGE (n:SysUserStory {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k != "requiresFeatures"})
            for fid in node.get("requiresFeatures", []):
                s.run("""MATCH (us:SysUserStory {id:$uid}), (f:SysFeature {id:$fid})
                         MERGE (us)-[:REQUIRES]->(f)""",
                      uid=node["id"], fid=fid)
            counts["stories"] = counts.get("stories", 0) + 1

        # Architecture standards
        for node in data.get("archStandards", []):
            s.run("MERGE (n:SysArchStd {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("systemId","appliesTo")})
            if "systemId" in node:
                s.run("""MATCH (sys:SysSystem {id:$sid}), (a:SysArchStd {id:$aid})
                         MERGE (sys)-[:GOVERNED_BY]->(a)""",
                      sid=node["systemId"], aid=node["id"])
            for mid in node.get("appliesTo", []):
                s.run("""MATCH (m:SysModule {id:$mid}), (a:SysArchStd {id:$aid})
                         MERGE (m)-[:CONFORMS_TO]->(a)""",
                      mid=mid, aid=node["id"])
            counts["archStandards"] = counts.get("archStandards", 0) + 1

        # Scenarios + chapters
        for node in data.get("scenarios", []):
            props = {k: v for k, v in node.items() if k not in ("chapters",)}
            s.run("MERGE (n:SysScenario {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            counts["scenarios"] = counts.get("scenarios", 0) + 1
            for ch in node.get("chapters", []):
                ch_id = f"{node['id']}-CH-{ch['number']}"
                s.run("MERGE (n:SysScenarioChapter {id:$id}) SET n += $props",
                      id=ch_id, props={**ch, "id": ch_id, "scenarioId": node["id"]})
                s.run("""MATCH (sc:SysScenario {id:$sid}), (ch:SysScenarioChapter {id:$cid})
                         MERGE (sc)-[:HAS_CHAPTER]->(ch)""",
                      sid=node["id"], cid=ch_id)
                for fid in ch.get("exercisesFeatures", []):
                    s.run("""MATCH (ch:SysScenarioChapter {id:$cid}), (f:SysFeature {id:$fid})
                             MERGE (ch)-[:EXERCISES]->(f)""",
                          cid=ch_id, fid=fid)
                counts["scenarioChapters"] = counts.get("scenarioChapters", 0) + 1

        # Training programs + modules
        for node in data.get("trainingPrograms", []):
            props = {k: v for k, v in node.items() if k not in ("modules",)}
            s.run("MERGE (n:SysTrainingProgram {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            counts["trainingPrograms"] = counts.get("trainingPrograms", 0) + 1
            for mod in node.get("modules", []):
                mod_id = mod["id"]
                s.run("MERGE (n:SysTrainingModule {id:$id}) SET n += $props",
                      id=mod_id, props={**mod, "programId": node["id"]})
                s.run("""MATCH (p:SysTrainingProgram {id:$pid}), (m:SysTrainingModule {id:$mid})
                         MERGE (p)-[:HAS_MODULE]->(m)""",
                      pid=node["id"], mid=mod_id)
                for fid in mod.get("teachesFeatures", []):
                    s.run("""MATCH (m:SysTrainingModule {id:$mid}), (f:SysFeature {id:$fid})
                             MERGE (m)-[:TEACHES]->(f)""",
                          mid=mod_id, fid=fid)
                counts["trainingModules"] = counts.get("trainingModules", 0) + 1

        # Test packages (registry only — no relationships needed)
        for node in data.get("testPackages", []):
            s.run("MERGE (n:SysTestPackage {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["testPackages"] = counts.get("testPackages", 0) + 1

        # Use cases
        for node in data.get("useCases", []):
            s.run("MERGE (n:SysUseCase {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items()
                                        if k not in ("storyId","requiresFeatures")})
            if "storyId" in node:
                s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                         MERGE (us)-[:REALIZED_BY]->(uc)""",
                      sid=node["storyId"], uid=node["id"])
            for fid in node.get("requiresFeatures", []):
                s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                         MERGE (uc)-[:REQUIRES]->(f)""", uid=node["id"], fid=fid)
            counts["useCases"] = counts.get("useCases", 0) + 1

        # Users
        for node in data.get("users", []):
            s.run("MERGE (n:SysUser {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("initiatesStories","usesApps")})
            for sid in node.get("initiatesStories", []):
                s.run("""MATCH (u:SysUser {id:$uid}),(us:SysUserStory {id:$sid})
                         MERGE (u)-[:INITIATES]->(us)""", uid=node["id"], sid=sid)
            counts["users"] = counts.get("users", 0) + 1

        # Applications
        for node in data.get("applications", []):
            s.run("MERGE (n:SysApplication {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("containsModules","usedBy")})
            for mid in node.get("containsModules", []):
                s.run("""MATCH (a:SysApplication {id:$aid}),(m:SysModule {id:$mid})
                         MERGE (a)-[:CONTAINS]->(m)""", aid=node["id"], mid=mid)
            for uid in node.get("usedBy", []):
                s.run("""MATCH (u:SysUser {id:$uid}),(a:SysApplication {id:$aid})
                         MERGE (u)-[:USES]->(a)""", uid=uid, aid=node["id"])
            counts["applications"] = counts.get("applications", 0) + 1

        # Defects — preserve 'closed' status; a seed from an older backup must not reopen closed work
        for node in data.get("defects", []):
            props = {k:v for k,v in node.items() if k not in ("affectsFeatures",)}
            existing_d = s.run("MATCH (d:SysDefect {id:$id}) RETURN d.status AS st",
                               id=node["id"]).single()
            if existing_d and existing_d["st"] == "closed" and props.get("status") != "closed":
                props["status"] = "closed"   # never downgrade closed → open via restore
            s.run("MERGE (n:SysDefect {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            for fid in node.get("affectsFeatures", []):
                s.run("""MATCH (d:SysDefect {id:$did}),(f:SysFeature {id:$fid})
                         MERGE (d)-[:AFFECTS]->(f)""", did=node["id"], fid=fid)
            counts["defects"] = counts.get("defects", 0) + 1

        # Enhancements — preserve 'done' status; a seed from an older backup must not reopen closed work
        for node in data.get("enhancements", []):
            props = {k:v for k,v in node.items() if k not in ("extendsFeatures",)}
            existing = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.status AS st",
                             id=node["id"]).single()
            if existing and existing["st"] == "done" and props.get("status") != "done":
                props["status"] = "done"   # never downgrade done → proposed via restore
            s.run("MERGE (n:SysEnhancement {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            for fid in node.get("extendsFeatures", []):
                s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                         MERGE (e)-[:EXTENDS]->(f)""", eid=node["id"], fid=fid)
            counts["enhancements"] = counts.get("enhancements", 0) + 1

        # Directory → test category mappings
        for node in data.get("dirCategories", []):
            s.run("MERGE (n:SysDirCategory {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["dirCategories"] = counts.get("dirCategories", 0) + 1

        # Architecture decisions + CONSTRAINED_BY and GOVERNED_BY edges
        for node in data.get("archDecisions", []):
            s.run("MERGE (n:SysArchDecision {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items()
                                        if k not in ("constrainsModules","systemId")})
            if node.get("systemId"):
                s.run("""MATCH (sys:SysSystem {id:$sid}),(a:SysArchDecision {id:$aid})
                         MERGE (sys)-[:GOVERNED_BY]->(a)""",
                      sid=node["systemId"], aid=node["id"])
            for mid in node.get("constrainsModules", []):
                s.run("""MATCH (m:SysModule {id:$mid}),(a:SysArchDecision {id:$aid})
                         MERGE (m)-[:CONSTRAINED_BY]->(a)""",
                      mid=mid, aid=node["id"])
            counts["archDecisions"] = counts.get("archDecisions", 0) + 1

        # Module prerequisite edges
        for edge in data.get("prerequisiteFor", []):
            s.run("""MATCH (a:SysModule {id:$from}),(b:SysModule {id:$to})
                     MERGE (a)-[:PREREQUISITE_FOR]->(b)""",
                  **{"from": edge["fromId"], "to": edge["toId"]})
            counts["prerequisiteFor"] = counts.get("prerequisiteFor", 0) + 1

        # Defect DETECTED_IN edges (AFFECTS edges handled in defects section above)
        for node in data.get("defects", []):
            for mid in node.get("detectedInModules", []):
                s.run("""MATCH (d:SysDefect {id:$did}),(m:SysModule {id:$mid})
                         MERGE (d)-[:DETECTED_IN]->(m)""",
                      did=node["id"], mid=mid)

        # Module pattern usage
        for edge in data.get("usesPattern", []):
            s.run("""MATCH (m:SysModule {id:$mid}), (p:SysModule {id:$pid})
                     MERGE (m)-[:USES_PATTERN]->(p)""",
                  mid=edge["moduleId"], pid=edge["patternId"])
            counts["usesPattern"] = counts.get("usesPattern", 0) + 1

        # Feedback entries
        for node in data.get("feedback", []):
            s.run("MERGE (f:SysFeedback {id:$id}) SET f += $props",
                  id=node["id"], props=node)
            counts["feedback"] = counts.get("feedback", 0) + 1

        # Proposals
        for node in data.get("proposals", []):
            s.run("MERGE (n:SysProposal {id:$id}) SET n += $props",
                  id=node["id"], props={k: v for k, v in node.items() if k not in ("extendsFeatures",)})
            for fid in node.get("extendsFeatures", []):
                s.run("""MATCH (p:SysProposal {id:$pid}),(f:SysFeature {id:$fid})
                         MERGE (p)-[:EXTENDS]->(f)""", pid=node["id"], fid=fid)
            counts["proposals"] = counts.get("proposals", 0) + 1

        # Notes
        for node in data.get("notes", []):
            s.run("MERGE (n:SysNote {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["notes"] = counts.get("notes", 0) + 1

        # Test runs
        for node in data.get("testRuns", []):
            s.run("MERGE (n:SysTestRun {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["testRuns"] = counts.get("testRuns", 0) + 1

        # Doc artifacts
        for node in data.get("docArtifacts", []):
            s.run("MERGE (n:SysDocArtifact {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["docArtifacts"] = counts.get("docArtifacts", 0) + 1

        # Cross-cutting relationship edges
        for edge in data.get("blockedBy", []):
            s.run("""MATCH (a:SysEnhancement {id:$fid}),(b:SysEnhancement {id:$tid})
                     MERGE (a)-[:BLOCKED_BY]->(b)""",
                  fid=edge["fromId"], tid=edge["toId"])
        if data.get("blockedBy"):
            counts["blockedBy"] = len(data["blockedBy"])

        for edge in data.get("demonstrates", []):
            s.run("""MATCH (a:SysScenarioChapter {id:$fid}),(b:SysFeature {id:$tid})
                     MERGE (a)-[:DEMONSTRATES]->(b)""",
                  fid=edge["fromId"], tid=edge["toId"])
        if data.get("demonstrates"):
            counts["demonstrates"] = len(data["demonstrates"])

        for edge in data.get("addresses", []):
            s.run("""MATCH (a:SysArchDecision {id:$fid}),(b:SysArchStd {id:$tid})
                     MERGE (a)-[:ADDRESSES]->(b)""",
                  fid=edge["fromId"], tid=edge["toId"])
        if data.get("addresses"):
            counts["addresses"] = len(data["addresses"])

        for edge in data.get("supersedes", []):
            s.run("""MATCH (a:SysArchDecision {id:$fid}),(b:SysArchDecision {id:$tid})
                     MERGE (a)-[:SUPERSEDES]->(b)""",
                  fid=edge["fromId"], tid=edge["toId"])
        if data.get("supersedes"):
            counts["supersedes"] = len(data["supersedes"])

        # Symbols
        for node in data.get("symbols", []):
            s.run("MERGE (n:SysSymbol {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("implementsFeature",)})
            if node.get("implementsFeature"):
                s.run("""MATCH (sym:SysSymbol {id:$sid}),(f:SysFeature {id:$fid})
                         MERGE (sym)-[:IMPLEMENTS]->(f)""",
                      sid=node["id"], fid=node["implementsFeature"])
            if node.get("moduleId"):
                s.run("""MATCH (sym:SysSymbol {id:$sid}),(m:SysModule {id:$mid})
                         MERGE (m)-[:CONTAINS_SYMBOL]->(sym)""",
                      sid=node["id"], mid=node["moduleId"])
            counts["symbols"] = counts.get("symbols", 0) + 1

        # Tests
        for node in data.get("tests", []):
            s.run("MERGE (n:SysTest {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k != "verifiesFeatures"})
            # Re-create CONTAINS_TEST edge from packageId stored on the node.
            # Without this, a backup restore loses the Package→Test path that
            # the briefing query requires for tier detection (cmp/int/uc).
            if node.get("packageId"):
                s.run("""MATCH (p:SysTestPackage {id:$pid}), (t:SysTest {id:$tid})
                         MERGE (p)-[:CONTAINS_TEST]->(t)""",
                      pid=node["packageId"], tid=node["id"])
            for fid in node.get("verifiesFeatures", []):
                s.run("""MATCH (t:SysTest {id:$tid}), (f:SysFeature {id:$fid})
                         MERGE (t)-[:VERIFIES]->(f)""",
                      tid=node["id"], fid=fid)
            counts["tests"] = counts.get("tests", 0) + 1

        # Endpoints
        for node in data.get("endpoints", []):
            s.run("MERGE (n:SysEndpoint {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k != "implementsFeature"})
            if "implementsFeature" in node:
                s.run("""MATCH (e:SysEndpoint {id:$eid}), (f:SysFeature {id:$fid})
                         MERGE (e)-[:IMPLEMENTS]->(f)""",
                      eid=node["id"], fid=node["implementsFeature"])
            counts["endpoints"] = counts.get("endpoints", 0) + 1

        # Restore any VERIFIES/IMPLEMENTS edges that existed before seed ran.
        # Seed only adds edges from the file; it never explicitly deletes, but
        # seeding from a stale backup can leave links absent if the file lacks them.
        if _pre_verifies:
            s.run("""
                UNWIND $edges AS e
                MATCH (t:SysTest {id: e.tid}), (f:SysFeature {id: e.fid})
                MERGE (t)-[:VERIFIES]->(f)
            """, edges=_pre_verifies)
        if _pre_impl_sym:
            s.run("""
                UNWIND $edges AS e
                MATCH (sym:SysSymbol {id: e.sid}), (f:SysFeature {id: e.fid})
                MERGE (sym)-[:IMPLEMENTS]->(f)
            """, edges=_pre_impl_sym)
        if _pre_impl_ep:
            s.run("""
                UNWIND $edges AS e
                MATCH (ep:SysEndpoint {id: e.eid}), (f:SysFeature {id: e.fid})
                MERGE (ep)-[:IMPLEMENTS]->(f)
            """, edges=_pre_impl_ep)

        # Change log and baselines (restored as-is; the change log is append-only)
        for key, label in (("changes", "SysChange"), ("baselines", "SysBaseline")):
            for node in data.get(key, []):
                s.run(f"MERGE (n:{label} {{id:$id}}) SET n += $props", id=node["id"], props=node)
                counts[key] = counts.get(key, 0) + 1

        # Generic relationships (backups written since schema v3): restores every
        # edge type, including ones without a dedicated field above.
        if raw_relationships:
            keep = None
            if filter_instance or filter_module:
                keep = {n.get("id") for v in data.values() if isinstance(v, list)
                        for n in v if isinstance(n, dict) and n.get("id")}
            counts["relationships"] = _restore_relationships(s, raw_relationships, keep)

        tx.commit()

    drv.close()
    for k, v in sorted(counts.items()):
        print(f"  {k:20s} {v}")
    print(f"\n✓ Seed complete from {path}")


# ── Briefing ──────────────────────────────────────────────────────────────────

def cmd_briefing(args):
    instance = args.instance
    system   = getattr(args, "system", "") or ""
    drv = _driver()
    today = date.today().isoformat()

    with drv.session() as s:
        _validate_instance(instance, s)
        # Modules for this instance (optionally scoped to a system)
        modules = s.run("""
            MATCH (d:SysDomain)<-[:CONTAINS]-(sys:SysSystem)
            MATCH (d)-[:CONTAINS]->(m:SysModule {instance: $inst})
            WHERE $sys = '' OR sys.id = $sys
            RETURN m.id AS id, m.name AS name, m.type AS type, d.name AS domain
            ORDER BY d.name, m.name
        """, inst=instance, sys=system).data()

        if not modules:
            print(f"No modules found for instance '{instance}'.")
            print(f"  Run: python3 scripts/sys_edge.py seed <backup.json> --instance {instance}")
            drv.close()
            return

        mod_ids = [m["id"] for m in modules]

        # Feature coverage per module — Superseded features excluded from counts
        coverage = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE m.id IN $mids
              AND NOT f.status IN ['Superseded','Deprecated']
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN m.id AS mid, f.id AS fid, f.name AS fname,
                   f.status AS status,
                   coalesce(f.isUserFacing, true) AS isUserFacing,
                   count(t) AS testCount
            ORDER BY m.id, f.id
        """, mids=mod_ids).data()

        # V-model four-tier coverage per module (ADR-027)
        # component=unit, integration=API, usecase=UC flow, e2e=US journey
        tier_coverage = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE m.id IN $mids
              AND NOT f.status IN ['Superseded','Deprecated']
            OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
            WHERE (tc.testType IN ['component','go-unit'])
               OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
            OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
            WHERE (ti.testType = 'integration')
               OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
            OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
            WHERE (tu.testType = 'usecase')
               OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
            OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
            WHERE (te.testType = 'e2e')
               OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
            RETURN m.id AS mid,
                   count(DISTINCT CASE WHEN tc IS NOT NULL THEN f END) AS cmpCovered,
                   count(DISTINCT CASE WHEN ti IS NOT NULL THEN f END) AS intCovered,
                   count(DISTINCT CASE WHEN tu IS NOT NULL THEN f END) AS ucCovered,
                   count(DISTINCT CASE WHEN te IS NOT NULL THEN f END) AS e2eCovered,
                   count(DISTINCT f) AS total
            ORDER BY m.id
        """, mids=mod_ids).data()
        tier_map = {r["mid"]: r for r in tier_coverage}

        # Defects — primary filter is def.instance; module links used for display aggregation only.
        defects = s.run("""
            MATCH (def:SysDefect) WHERE def.status <> 'closed' AND def.instance = $inst
            OPTIONAL MATCH (def)-[:AFFECTS]->(f:SysFeature)<-[:PROVIDES]-(mf:SysModule)
            OPTIONAL MATCH (def)-[:DETECTED_IN]->(md:SysModule)
            WITH def, f,
                 CASE WHEN mf IS NOT NULL AND mf.id IN $mids THEN mf
                      WHEN md IS NOT NULL AND md.id IN $mids THEN md
                      ELSE coalesce(mf, md) END AS m
            WITH def, collect(DISTINCT f.id)[0] AS fid,
                 collect(DISTINCT f.name)[0] AS fname,
                 collect(DISTINCT m.id)[0] AS mid
            RETURN def.id AS did, def.title AS title,
                   coalesce(def.severity,'medium') AS sev,
                   def.source AS src,
                   def.occurrences AS occ,
                   fid, fname, mid
            ORDER BY
              CASE coalesce(def.severity,'medium')
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 ELSE 3 END,
              def.id
        """, mids=mod_ids, inst=instance).data()

        # Enhancements for this instance — include unlinked ones (instance filter)
        enhancements = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.instance = $inst AND e.status <> 'done'
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)
            OPTIONAL MATCH (f)<-[:PROVIDES]-(m:SysModule)
            RETURN e.id AS eid, e.title AS title,
                   coalesce(e.status,'proposed') AS status,
                   coalesce(e.priority,'Should') AS priority,
                   f.id AS fid, f.name AS fname, m.id AS mid
            ORDER BY
              CASE coalesce(e.status,'proposed')
                WHEN 'in-progress' THEN 0 WHEN 'approved' THEN 1
                WHEN 'proposed' THEN 2 ELSE 3 END,
              CASE coalesce(e.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              e.id
        """, inst=instance, mids=mod_ids).data()

        # Architecture standard compliance.
        # Only show standards that have at least one CONFORMS_TO edge pointing
        # at a module in this instance — standards with no appliesTo are
        # system-wide references and not shown per-instance.
        arch = s.run("""
            MATCH (sys:SysSystem)-[:GOVERNED_BY]->(a:SysArchStd)
            MATCH (m_any:SysModule)-[:CONFORMS_TO]->(a)
            WHERE m_any.id IN $mids
            WITH a, collect(DISTINCT m_any.id) AS conforming
            RETURN a.id AS aid, a.name AS aname, a.category AS cat,
                   conforming
            ORDER BY a.category, a.id
        """, mids=mod_ids).data()

        # Evidence map: which gap features have scenario/training backing
        # (must be inside session — queried after we know the gap fids)
        all_fids = [r["fid"] for v in coverage for r in [v]]
        evidence_raw = s.run("""
            UNWIND $fids AS fid
            MATCH (f:SysFeature {id: fid})
            OPTIONAL MATCH (ch:SysScenarioChapter)-[:EXERCISES]->(f)
            OPTIONAL MATCH (ch)<-[:HAS_CHAPTER]-(sc:SysScenario)
            OPTIONAL MATCH (tm:SysTrainingModule)-[:TEACHES]->(f)
            OPTIONAL MATCH (tm)<-[:HAS_MODULE]-(tp:SysTrainingProgram)
            RETURN fid,
                   collect(DISTINCT sc.name) AS scenarios,
                   collect(DISTINCT tp.name) AS training
        """, fids=all_fids).data()
        ev_map = {r["fid"]: r for r in evidence_raw}

        # Orphan test count — must be inside session block
        orphan_tests = s.run("""
            MATCH (t:SysTest)
            WHERE NOT ()-[:CONTAINS_TEST]->(t)
              AND NOT (t)-[:VERIFIES]->()
            RETURN count(t) AS n
        """).single()["n"]

    drv.close()

    # ── Format output ────────────────────────────────────────────────────────
    W = 62
    sys_tag = f"  [{system}]" if system else ""
    print("═" * W)
    print(f"  SYSTEM GRAPH BRIEFING — {instance}{sys_tag}  ({today})")
    print("═" * W)

    # Group coverage by module
    from collections import defaultdict
    by_mod = defaultdict(list)
    for row in coverage:
        by_mod[row["mid"]].append(row)

    # Separate user-facing from infrastructure features for coverage counts
    uf_rows = [r for v in by_mod.values() for r in v if r.get("isUserFacing", True)]
    total_f      = len(uf_rows)
    total_tested = sum(1 for r in uf_rows if r["testCount"] > 0)
    infra_count  = sum(1 for v in by_mod.values() for r in v if not r.get("isUserFacing", True))

    infra_note = f"  ({infra_count} infra excluded)" if infra_count else ""
    print(f"\nMODULES ({len(modules)})  —  features: {total_f}  tested: {total_tested}/{total_f}{infra_note}")
    print()

    compact = getattr(args, "compact", False)

    def tier_str(covered, total, short):
        mark = "✓" if covered == total else ("~" if covered > 0 else "✗")
        return f"{mark}{short} {covered}/{total}"

    for mod in modules:
        mid   = mod["id"]
        mrows = by_mod.get(mid, [])
        uf_mrows = [r for r in mrows if r.get("isUserFacing", True)]
        tested = sum(1 for r in uf_mrows if r["testCount"] > 0)
        all_pass = (tested == len(uf_mrows) and uf_mrows)
        pct    = f"{tested}/{len(uf_mrows)}" if uf_mrows else "0/0"
        tick   = " ✓" if all_pass else ""
        mtype  = f" [{mod['type']}]" if mod.get("type") else ""
        print(f"  {mid:<22} {mod['name']:<32} {pct}{tick}{mtype}")

        # Three-tier coverage line
        tc = tier_map.get(mid, {})
        if tc and tc.get("total", 0) > 0:
            n    = tc["total"]
            shown = [tier_str(tc.get("cmpCovered", 0), n, "cmp"),
                     tier_str(tc.get("intCovered", 0), n, "int")]
            if not _lightweight():
                shown += [tier_str(tc.get("ucCovered",  0), n, "uc "),
                          tier_str(tc.get("e2eCovered", 0), n, "e2e")]
            tiers = "  ".join(shown)
            print(f"    {'':22} {tiers}")

        # In compact mode skip per-feature list when module is fully passing
        if compact and all_pass:
            print()
            continue

        for r in mrows:
            tick2 = "✓" if r["testCount"] > 0 else "✗"
            infra_tag = " [infra]" if not r.get("isUserFacing", True) else ""
            print(f"    {tick2} {r['fid']:<14} {r['fname']}{infra_tag}")
        print()

    # Coverage gaps — user-facing features only (infrastructure excluded by default)
    gaps = [r for v in by_mod.values() for r in v
            if r["testCount"] == 0 and r.get("isUserFacing", True)]
    if gaps:
        print(f"COVERAGE GAPS ({len(gaps)})")
        for r in gaps:
            mid_label = next((m["id"] for m in modules
                              if any(x["fid"] == r["fid"] for x in by_mod.get(m["id"], []))), "?")
            ev = ev_map.get(r["fid"], {})
            scns = ev.get("scenarios", [])
            trns = ev.get("training", [])
            tag = ""
            if scns:
                tag += f"  ← scenario: {', '.join(scns)}"
            if trns:
                tag += f"  ← training: {', '.join(trns)}"
            urgency = "!" if (scns or trns) else " "
            print(f"  {urgency}✗ {r['fid']:<14} {r['fname']:<38} {mid_label}{tag}")
        print()

    # Enhancements
    if enhancements:
        from itertools import groupby
        print(f"ENHANCEMENTS ({len(enhancements)})")
        by_status = {}
        for e in enhancements:
            by_status.setdefault(e["status"], []).append(e)
        status_order = ["approved", "in-progress", "proposed"]
        for st in status_order:
            items = by_status.get(st, [])
            if not items:
                continue
            # Group ENHs that share a feature — avoids 60-line flat lists (FB-229)
            by_feat: dict = {}
            ungrouped = []
            for e in items:
                fid = e.get("fid")
                if fid:
                    by_feat.setdefault(fid, []).append(e)
                else:
                    ungrouped.append(e)
            # Print grouped first
            seen_feat: set = set()
            for e in items:
                fid = e.get("fid")
                if fid and fid in by_feat and fid not in seen_feat:
                    seen_feat.add(fid)
                    group = by_feat[fid]
                    prio = f"[{group[0]['priority']}]" if group[0].get("priority") else ""
                    if len(group) > 1:
                        ids = ", ".join(g["eid"] for g in group)
                        print(f"    {fid:<14} {prio:<8} [{len(group)} ENHs: {ids[:60]}]")
                    else:
                        print(f"    {e['eid']:<16} {prio:<8} {e['title'][:60]:<60} → {fid}")
                elif not fid:
                    prio = f"[{e['priority']}]" if e.get("priority") else ""
                    print(f"    {e['eid']:<16} {prio:<8} {e['title'][:60]}")
        print()
    else:
        print("ENHANCEMENTS — none\n")

    # Defects
    if defects:
        print(f"OPEN DEFECTS ({len(defects)})")
        for d in defects:
            src_tag = " [log]" if d.get("src") == "log" else ""
            occ_tag = f" ×{d['occ']}" if d.get("occ") and int(d["occ"]) > 1 else ""
            feat_ref = f"→ {d['fid']}" if d.get("fid") else f"[{d['mid']}]"
            print(f"  {d['did']:<18} [{d['sev']:<8}] {d['title'][:40]:<40} {feat_ref}{src_tag}{occ_tag}")
        print()
    else:
        print("OPEN DEFECTS — none\n")

    # Architecture — only show standards that explicitly list modules in this instance
    if arch:
        print("ARCHITECTURE STANDARDS")
        last_cat = None
        for a in arch:
            cat = a["cat"] or "General"
            if cat != last_cat:
                print(f"  {cat}")
                last_cat = cat
            conforming = a["conforming"]
            status = f"✓ {', '.join(conforming)}" if conforming else "no conforming modules"
            print(f"    {a['aid']:<18} {a['aname']:<38} {status}")
        print()

    # ── Orphan warning (FB-108) ───────────────────────────────────────────────
    if orphan_tests > 10:
        print(f"\n  ⚠  {orphan_tests} unlinked tests detected — run scan-tests to register them,"
              f" then link-feature to connect them to features")

    print("─" * W)
    print("  sys_edge.py link-feature / link-endpoint / link-symbol to update")
    print("─" * W)


# ── Link commands ─────────────────────────────────────────────────────────────

def cmd_link_test(args):
    """Link a specific test function to a feature.

    Prefers matching an already-scanned SysTest node by file+function before
    creating a new one, so scanned and manually-linked tests stay unified.
    """
    drv = _driver()
    with drv.session() as s:
        # Try to find an existing scanned node first (id = {file}::{class}::{fn} or {file}::{fn})
        existing = s.run("""
            MATCH (t:SysTest)
            WHERE t.file CONTAINS $file AND t.function = $fn
            RETURN t.id AS tid LIMIT 1
        """, file=args.file, fn=args.fn).single()

        if existing:
            tid = existing["tid"]
            if args.type:
                s.run("MATCH (t:SysTest {id:$tid}) SET t.testType=$ttype",
                      tid=tid, ttype=args.type)
        else:
            # Legacy path: create a node with the old T-... format
            tid = f"T-{args.file.replace('/', '-')}-{args.fn}"[:80]
            s.run("""
                MERGE (t:SysTest {id:$tid})
                SET t.file=$file, t.function=$fn, t.testType=$ttype
            """, tid=tid, file=args.file, fn=args.fn, ttype=args.type or "integration")

        s.run("""
            MATCH (t:SysTest {id:$tid}), (f:SysFeature {id:$fid})
            MERGE (t)-[:VERIFIES]->(f)
        """, tid=tid, fid=args.feature)
    drv.close()
    print(f"✓ {args.fn}  →  VERIFIES  →  {args.feature}")


def cmd_link_feature(args):
    """Bulk-link matching test nodes to a feature by pattern.

    --tests patterns are matched against SysTest.id. If the pattern contains
    '::' (pytest node ID style) the match is anchored — only tests whose ID
    ends with or equals the pattern match, preventing file-prefix bleed where
    all tests in a file link to every feature. Plain directory prefixes (no ::)
    still use substring matching for bulk-linking by directory.

    Examples:
        link-feature --feature F-API-001 --tests "test_orders_api.py::TestCreateOrder"
        link-feature --feature F-API-002 --tests "TestOrderUpdate::test_patch,TestOrderDelete"
        link-feature --feature F-WEB-003 --tests "tests/web/"  # directory bulk-link
    """
    fid      = args.feature
    raw_patterns = [p.strip() for p in args.tests.split(",") if p.strip()]
    # Auto-correct: strip repo root prefix from absolute paths to prevent zero-match footgun
    _root_prefix = str(_root) + "/"
    patterns = []
    for p in raw_patterns:
        if p.startswith(_root_prefix):
            corrected = p[len(_root_prefix):]
            print(f"  ℹ  auto-corrected absolute path: {corrected}", file=sys.stderr)
            patterns.append(corrected)
        elif p.startswith("/"):
            print(f"  ⚠  absolute path '{p[:60]}' will likely match nothing — "
                  f"use a relative path like 'backend/tests/p4/test_foo.py::TestClass'",
                  file=sys.stderr)
            patterns.append(p)
        else:
            patterns.append(p)
    drv      = _driver()
    total    = 0
    with drv.session() as s:
        if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
            print(f"ERROR: feature '{fid}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        for pat in patterns:
            # Anchored match when pattern contains '::' — prevents N:M bleed
            if "::" in pat:
                result = s.run("""
                    MATCH (t:SysTest)
                    WHERE t.id = $pat OR t.id ENDS WITH $pat
                    WITH t LIMIT 500
                    MATCH (f:SysFeature {id:$fid})
                    MERGE (t)-[:VERIFIES]->(f)
                    RETURN count(t) AS cnt
                """, pat=pat, fid=fid).single()
            else:
                result = s.run("""
                    MATCH (t:SysTest) WHERE t.id CONTAINS $pat
                    MATCH (f:SysFeature {id:$fid})
                    MERGE (t)-[:VERIFIES]->(f)
                    RETURN count(t) AS cnt
                """, pat=pat, fid=fid).single()
            cnt = result["cnt"] if result else 0
            total += cnt
            if cnt == 0:
                print(f"  ⚠ {pat}  →  0 matches — pattern did not resolve to any SysTest node",
                      file=sys.stderr)
                print(f"     Hint: run scan-tests first, then use the full path from scan output",
                      file=sys.stderr)
            else:
                tier = getattr(args, "tier", "") or ""
                if tier:
                    # Override testType on matched tests (e.g. promote inferred [integration] to [component])
                    if "::" in pat:
                        s.run("""
                            MATCH (t:SysTest) WHERE t.id = $pat OR t.id ENDS WITH $pat
                            SET t.testType = $tier
                        """, pat=pat, tier=tier)
                    else:
                        s.run("""
                            MATCH (t:SysTest) WHERE t.id CONTAINS $pat
                            SET t.testType = $tier
                        """, pat=pat, tier=tier)
                    print(f"  {pat}  →  {cnt} tests  →  {fid}  [tier: {tier}]")
                else:
                    print(f"  {pat}  →  {cnt} tests  →  {fid}")
                # Check for orphan tests (no CONTAINS_TEST edge → won't count in test-gaps)
                orphan = s.run("""
                    MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature {id:$fid})
                    WHERE t.id CONTAINS $pat
                      AND NOT (:SysTestPackage)-[:CONTAINS_TEST]->(t)
                    RETURN count(t) AS n
                """, fid=fid, pat=pat).single()["n"]
                if orphan > 0:
                    print(f"  ⚠ {orphan} matched test(s) have no CONTAINS_TEST edge — "
                          f"they won't count in test-gaps. Run scan-tests to register them.",
                          file=sys.stderr)
    drv.close()
    print(f"  total: {total} VERIFIES edges created")


def cmd_link_endpoint(args):
    eid = f"EP-{args.method}-{args.path.replace('/', '-')}"[:80]
    features = [f.strip() for f in (args.feature or "").split(",") if f.strip()]
    if not features:
        print("ERROR: --feature is required", file=sys.stderr); sys.exit(1)
    drv = _driver()
    with drv.session() as s:
        for fid in features:
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"ERROR: feature '{fid}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
        s.run("""
            MERGE (e:SysEndpoint {id:$eid})
            SET e.method=$method, e.path=$path,
                e.binary=$binary, e.permission=$perm
        """, eid=eid, method=args.method, path=args.path,
             binary=args.binary or "", perm=args.permission or "")
        for fid in features:
            s.run("""
                MATCH (e:SysEndpoint {id:$eid}), (f:SysFeature {id:$fid})
                MERGE (e)-[:IMPLEMENTS]->(f)
            """, eid=eid, fid=fid)
            print(f"✓ {args.method} {args.path}  →  IMPLEMENTS  →  {fid}")
    drv.close()


# Regex patterns for Go chi router route registrations
_GO_ROUTE_RE = re.compile(
    r'\br\.(Get|Post|Put|Patch|Delete|Head|Options|Connect|Trace)'
    r'\s*\(\s*"([^"]+)"'
)
# Permission string inside withPerm("perm", ...) on the same line
_GO_PERM_RE = re.compile(r'withPerm\s*\(\s*"([^"]+)"')
# Auth-only (no specific permission) — withAuth(...)
_GO_AUTH_RE = re.compile(r'withAuth\s*\(')
# RequirePermission middleware call on the same line
_GO_REQUIRE_PERM_RE = re.compile(r'RequirePermission\s*\(\s*"([^"]+)"')

@functools.lru_cache(maxsize=None)
def _project_config() -> dict:
    """Project-specific settings from <project root>/sysedge.yaml (optional).

    Keys (all optional):
      instances:       [names]            instance list for silent-instance reports
                                          (default: derived from SysModule.instance)
      binaries:        [{prefix, binary}] map source path prefixes to deployable
                                          binaries for scan-endpoints
      default_binary:  name               binary for paths no prefix matches
      go_root:         path               default root for Go route and test scans
    """
    path = _root / "sysedge.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"  ⚠  could not read {path}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _project_instances(session) -> list:
    """Configured instance names, else every SysModule.instance in the graph."""
    configured = _project_config().get("instances")
    if configured:
        return [str(i) for i in configured]
    rows = session.run("""
        MATCH (m:SysModule) WHERE m.instance IS NOT NULL AND m.instance <> ''
        RETURN DISTINCT m.instance AS inst ORDER BY inst
    """).data()
    return [r["inst"] for r in rows]


def _go_root() -> str:
    return str(_project_config().get("go_root") or ".")


def _lightweight() -> bool:
    """`model: lightweight` in sysedge.yaml: user stories and use cases are optional.

    Reports then show only the component and integration test tiers and stop
    flagging features or stories that have no use case. Nothing is deleted;
    `config set model full` turns the full V-model back on.
    """
    return str(_project_config().get("model", "full")).lower() == "lightweight"


def _infer_binary(file_path: str) -> str:
    """Deployable binary for a source path, from sysedge.yaml `binaries` (longest prefix wins)."""
    cfg = _project_config()
    rules = sorted(cfg.get("binaries") or [], key=lambda r: -len(str(r.get("prefix", ""))))
    for rule in rules:
        if file_path.startswith(str(rule.get("prefix", ""))):
            return str(rule.get("binary", ""))
    return str(cfg.get("default_binary") or "")


def _scan_go_routes(root: Path) -> list[dict]:
    """Parse Go files under root for chi router route registrations."""
    routes = []
    for f in sorted(root.rglob("*.go")):
        if "_test.go" in f.name:
            continue
        if any(part in _SKIP_DIRS for part in f.parts):
            continue
        rel = _repo_rel_path(f)
        binary = _infer_binary(rel)
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _GO_ROUTE_RE.search(line)
                if not m:
                    continue
                method, path = m.group(1).upper(), m.group(2)
                # Extract permission from the same line
                perm_m = _GO_PERM_RE.search(line) or _GO_REQUIRE_PERM_RE.search(line)
                perm = perm_m.group(1) if perm_m else ("authenticated" if _GO_AUTH_RE.search(line) else "")
                routes.append({
                    "method": method, "path": path,
                    "binary": binary, "permission": perm, "file": rel,
                })
        except Exception:
            pass
    return routes


def cmd_scan_endpoints(args):
    """Scan Go source for route registrations and diff against SysEndpoint graph nodes.

    Reports:
      MISSING  — registered in code but absent from graph
      STALE    — in graph but not found in code (may be phantom/deleted)
      PERM     — permission string differs between code and graph

    Use --apply to upsert missing/updated endpoints into the graph (no feature link is created).
    """
    root       = Path(getattr(args, 'path',     '') or _go_root())
    instance   = getattr(args, 'instance',  '') or ''
    apply_mode = getattr(args, 'apply',     False)
    binary_f   = getattr(args, 'binary',    '') or ''

    if not root.exists():
        print(f"ERROR: {root} does not exist", file=sys.stderr); sys.exit(1)

    code_routes = _scan_go_routes(root)
    if binary_f:
        code_routes = [r for r in code_routes if r["binary"] == binary_f]

    # Build lookup: (METHOD, path) → route
    code_index = {(r["method"], r["path"]): r for r in code_routes}

    drv = _driver()
    with drv.session() as s:
        inst_filter = "WHERE EXISTS { MATCH (e)-[:IMPLEMENTS]->(f:SysFeature)<-[:PROVIDES]-(:SysModule {instance:$inst}) }" if instance else ""
        graph_eps = s.run(f"""
            MATCH (e:SysEndpoint)
            {inst_filter}
            RETURN e.method AS method, e.path AS path,
                   e.binary AS binary, e.permission AS perm, e.id AS eid
        """, inst=instance).data()

    drv.close()

    graph_index = {(r["method"], r["path"]): r for r in graph_eps}

    # Diff
    missing, stale, perm_mismatch = [], [], []

    for key, route in code_index.items():
        if key not in graph_index:
            missing.append(route)
        else:
            g = graph_index[key]
            if route["permission"] and g.get("perm", "") != route["permission"]:
                perm_mismatch.append({**route, "graph_perm": g.get("perm", "")})

    for key, ep in graph_index.items():
        if key not in code_index:
            stale.append(ep)

    # Report
    total_code  = len(code_index)
    total_graph = len(graph_index)
    print(f"\nENDPOINT DRIFT REPORT  ({root})")
    print(f"  Code: {total_code} routes  |  Graph: {total_graph} SysEndpoint nodes\n")

    if missing:
        print(f"  MISSING IN GRAPH ({len(missing)}) — in code, not in graph:")
        for r in missing:
            perm_tag = f"  [{r['permission']}]" if r["permission"] else ""
            print(f"    {r['method']:<7} {r['path']:<60} {r['binary']}{perm_tag}")
    else:
        print("  MISSING IN GRAPH: none ✓")

    if stale:
        print(f"\n  STALE IN GRAPH ({len(stale)}) — in graph, not found in code:")
        for r in stale:
            print(f"    {r['method']:<7} {r['path']:<60} {r.get('binary','')}")
    else:
        print("\n  STALE IN GRAPH: none ✓")

    if perm_mismatch:
        print(f"\n  PERMISSION MISMATCH ({len(perm_mismatch)}):")
        for r in perm_mismatch:
            print(f"    {r['method']:<7} {r['path']:<55}  "
                  f"code={r['permission']}  graph={r['graph_perm']}")
    else:
        print("\n  PERMISSION MISMATCH: none ✓")

    if not missing and not stale and not perm_mismatch:
        print("\n  ✓ Graph endpoints are in sync with code.")

    # Apply: upsert missing + update perm mismatches
    if apply_mode and (missing or perm_mismatch):
        drv2 = _driver()
        with drv2.session() as s:
            for r in missing:
                eid = f"EP-{r['method']}-{r['path'].replace('/', '-')}"[:80]
                s.run("""
                    MERGE (e:SysEndpoint {id:$eid})
                    SET e.method=$method, e.path=$path, e.binary=$binary, e.permission=$perm
                """, eid=eid, method=r["method"], path=r["path"],
                     binary=r["binary"], perm=r["permission"])
                print(f"  ✓ upserted  {r['method']} {r['path']}")
            for r in perm_mismatch:
                eid = f"EP-{r['method']}-{r['path'].replace('/', '-')}"[:80]
                s.run("""
                    MERGE (e:SysEndpoint {id:$eid})
                    SET e.permission=$perm
                """, eid=eid, perm=r["permission"])
                print(f"  ✓ perm fix  {r['method']} {r['path']}  → {r['permission']}")
        drv2.close()
        print(f"\n  Applied {len(missing)+len(perm_mismatch)} changes. "
              f"Run link-endpoint --feature to add IMPLEMENTS edges.")
    elif apply_mode:
        print("\n  Nothing to apply.")

    if missing or stale or perm_mismatch:
        sys.exit(1)


def cmd_link_symbol(args):
    sid = f"SYM-{args.file.replace('/', '-')}-{args.symbol}"[:80]
    features = [f.strip() for f in (args.feature or "").split(",") if f.strip()]
    if not features:
        print("ERROR: --feature is required", file=sys.stderr); sys.exit(1)
    drv = _driver()
    with drv.session() as s:
        for fid in features:
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"ERROR: feature '{fid}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
        s.run("""
            MERGE (sym:SysSymbol {id:$sid})
            SET sym.file=$file, sym.symbol=$symbol,
                sym.line=$line, sym.symbolType=$stype
        """, sid=sid, file=args.file, symbol=args.symbol,
             line=args.line or 0, stype=args.symtype or "handler")
        for fid in features:
            s.run("""
                MATCH (sym:SysSymbol {id:$sid}), (f:SysFeature {id:$fid})
                MERGE (sym)-[:IMPLEMENTS]->(f)
            """, sid=sid, fid=fid)
            print(f"✓ {args.symbol}  →  IMPLEMENTS  →  {fid}")
        # Also wire CONTAINS_SYMBOL so briefing symbol counts are correct.
        # Explicit --module wins; otherwise find the module whose codePaths
        # or paths prefix-matches the file.
        explicit_mod = getattr(args, "module", None) or ""
        if explicit_mod:
            linked = s.run("""
                MATCH (m:SysModule {id:$mid}), (sym:SysSymbol {id:$sid})
                MERGE (m)-[:CONTAINS_SYMBOL]->(sym)
                RETURN m.id AS mid
            """, mid=explicit_mod, sid=sid).single()
            if linked:
                print(f"  CONTAINS_SYMBOL  ←  {linked['mid']}")
            else:
                print(f"  ⚠  module '{explicit_mod}' not found — CONTAINS_SYMBOL not created",
                      file=sys.stderr)
        else:
            # Infer module from file path prefix against codePaths / paths
            path_rows = s.run("""
                MATCH (m:SysModule)
                WHERE m.codePaths IS NOT NULL OR m.paths IS NOT NULL
                RETURN m.id AS mid,
                       coalesce(m.codePaths, []) + coalesce(m.paths, []) AS paths
            """).data()
            matched_mid = ""
            matched_len = 0
            for row in path_rows:
                for cp in row["paths"]:
                    cp_norm = cp.rstrip("/")
                    if args.file.startswith(cp_norm + "/") or args.file == cp_norm:
                        if len(cp_norm) > matched_len:
                            matched_mid = row["mid"]
                            matched_len = len(cp_norm)
            if matched_mid:
                s.run("""
                    MATCH (m:SysModule {id:$mid}), (sym:SysSymbol {id:$sid})
                    MERGE (m)-[:CONTAINS_SYMBOL]->(sym)
                """, mid=matched_mid, sid=sid)
                print(f"  CONTAINS_SYMBOL  ←  {matched_mid}  (inferred from codePaths)")
            else:
                print(f"  ⚠  no module codePath matches '{args.file}' — "
                      "CONTAINS_SYMBOL not created (pass --module MODULE_ID to set explicitly)",
                      file=sys.stderr)
    drv.close()


def cmd_refresh_lines(args):
    """Re-resolve SysSymbol line numbers by name — does not re-register symbols.

    Uses file-read + regex to find each symbol's current position, then patches
    sym.line without touching any other field or relationship. Safe to run at
    session start: current symbols are a no-op; only stale ones are updated.

    Patterns used:
      .go   — ^func [(*Receiver) ]Name(
      .py   — ^[async ]def name( | ^class Name[:(]
      .ts/x — ^[export ][async ]function Name( | ^[export ]const Name = (async )(
    """
    from collections import defaultdict as _dd
    instance  = getattr(args, "instance",  "") or ""
    module_id = getattr(args, "module",    "") or ""
    file_filt = getattr(args, "file",      "") or ""
    root      = (getattr(args, "root",     "") or "").rstrip("/")
    dry_run   = getattr(args, "dry_run",   False)

    drv = _driver()
    with drv.session() as s:
        if module_id:
            rows = s.run("""
                MATCH (m:SysModule {id:$mid})-[:CONTAINS_SYMBOL]->(sym:SysSymbol)
                RETURN sym.id AS sid, sym.file AS file, sym.symbol AS name,
                       coalesce(sym.line, 0) AS line
                ORDER BY sym.file, sym.id
            """, mid=module_id).data()
        elif instance:
            rows = s.run("""
                MATCH (m:SysModule {instance:$inst})-[:CONTAINS_SYMBOL]->(sym:SysSymbol)
                RETURN sym.id AS sid, sym.file AS file, sym.symbol AS name,
                       coalesce(sym.line, 0) AS line
                ORDER BY sym.file, sym.id
            """, inst=instance).data()
        elif file_filt:
            rows = s.run("""
                MATCH (sym:SysSymbol) WHERE sym.file CONTAINS $fp
                RETURN sym.id AS sid, sym.file AS file, sym.symbol AS name,
                       coalesce(sym.line, 0) AS line
                ORDER BY sym.file, sym.id
            """, fp=file_filt).data()
        else:
            print("ERROR: scope required — pass --instance, --module, or --file",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
    drv.close()

    if not rows:
        print("  No symbols found for the given scope.")
        return

    # Group by file so each file is read once
    by_file = _dd(list)
    for r in rows:
        by_file[r["file"]].append(r)

    # Per-extension regex: each pattern has one capture group = symbol name
    _PATTERNS = {
        ".go":  [re.compile(r'^func\s+(?:\([^)]+\)\s+)?(\w+)\s*[\(\[]'),
                 re.compile(r'^type\s+(\w+)\s+'),
                 re.compile(r'^(?:const|var)\s+(\w+)\s*(?:[=\s]|$)'),
                 re.compile(r'^\s+(\w+)\s+=\s+iota')],  # iota const block first entry
        ".py":  [re.compile(r'^(?:async\s+)?def\s+(\w+)\s*[\(:]'),
                 re.compile(r'^class\s+(\w+)\s*[:\(]')],
        ".ts":  [re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(\<]'),
                 re.compile(r'^(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(')],
        ".tsx": [re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(\<]'),
                 re.compile(r'^(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(')],
        ".js":  [re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(')],
        ".jsx": [re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(')],
    }

    updates = []      # (sid, new_line, old_line, name, short_file)
    not_found = missing_file = 0

    for filepath, syms in sorted(by_file.items()):
        # Resolve relative paths: try --root first, then cwd
        p = Path(filepath)
        if not p.is_absolute():
            if root:
                p = Path(root) / filepath
            else:
                p = Path.cwd() / filepath
        if not p.exists():
            missing_file += len(syms)
            print(f"  ⚠  {filepath}: file not found — {len(syms)} symbol(s) skipped",
                  file=sys.stderr)
            continue

        ext = p.suffix.lower()
        pats = _PATTERNS.get(ext, [])
        if not pats:
            # Unknown extension — fall back to simple 'name(' scan
            pats = [re.compile(r'(?:^|\s)(\w+)\s*[\(\[]')]

        name_to_line = {}
        try:
            for lineno, text in enumerate(p.read_text(errors="replace").splitlines(), 1):
                for pat in pats:
                    m = pat.match(text)
                    if m:
                        name_to_line.setdefault(m.group(1), lineno)  # first occurrence wins
                        break
        except Exception as e:
            print(f"  ⚠  {filepath}: read error — {e}", file=sys.stderr)
            continue

        short = filepath.split("/")[-1]
        for sym in syms:
            new_line = name_to_line.get(sym["name"])
            if new_line is None:
                not_found += 1
            elif new_line != sym["line"]:
                updates.append((sym["sid"], new_line, sym["line"], sym["name"], short))

    # Apply
    if updates:
        if dry_run:
            for _, nl, ol, name, short in updates:
                print(f"  [dry] {name:<40} {ol} → {nl}  ({short})")
        else:
            drv2 = _driver()
            with drv2.session() as s2:
                s2.run("""
                    UNWIND $rows AS r
                    MATCH (sym:SysSymbol {id: r.sid})
                    SET sym.line = r.line
                """, rows=[{"sid": sid, "line": nl} for sid, nl, *_ in updates])
            drv2.close()
            for _, nl, ol, name, short in updates:
                print(f"  ↻ {name:<40} {ol:>5} → {nl:<5}  ({short})")

    total = len(rows)
    n_upd = len(updates)
    n_cur = total - n_upd - not_found - missing_file
    label = "[dry-run] " if dry_run else ""
    print(f"\n  {label}{total} symbols  ·  {n_upd} refreshed  ·  "
          f"{n_cur} already current  ·  {not_found} name-not-found  ·  {missing_file} file-missing")
    if dry_run and n_upd:
        print(f"  Re-run without --dry-run to apply.")


# ── List commands ─────────────────────────────────────────────────────────────

def cmd_features(args):
    drv = _driver()
    with drv.session() as s:
        rows = s.run("""
            MATCH (m:SysModule {id:$mid})-[:PROVIDES]->(f:SysFeature)
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN f.id AS id, f.name AS name, f.status AS status,
                   coalesce(f.isUserFacing, true) AS isUserFacing,
                   count(t) AS tests
            ORDER BY f.id
        """, mid=args.module).data()
    drv.close()
    for r in rows:
        tick = "✓" if r["tests"] > 0 else "✗"
        infra_tag = " [infra]" if not r.get("isUserFacing", True) else ""
        print(f"  {tick} {r['id']:<16} {r['name']}{infra_tag}")


def cmd_stories(args):
    drv = _driver()
    with drv.session() as s:
        if getattr(args, "gap", False):
            rows = s.run("""
                MATCH (us:SysUserStory)
                WHERE NOT (us)-[:REALIZED_BY]->(:SysUseCase)
                RETURN us.id AS id, us.title AS title
                ORDER BY us.id
            """).data()
            drv.close()
            print(f"USER STORIES WITHOUT USE CASES ({len(rows)})")
            for r in rows:
                print(f"  {r['id']}  {r['title']}")
            return
        rows = s.run("""
            MATCH (us:SysUserStory)
            OPTIONAL MATCH (us)-[:REQUIRES]->(f:SysFeature)
            OPTIONAL MATCH (sc:SysScenario)-[:HAS_CHAPTER]->(:SysScenarioChapter)-[:EXERCISES]->(f)
            OPTIONAL MATCH (tp:SysTrainingProgram)-[:HAS_MODULE]->(:SysTrainingModule)-[:TEACHES]->(f)
            RETURN us.id AS id, us.title AS title,
                   collect(DISTINCT f.id) AS features,
                   collect(DISTINCT sc.name) AS scenarios,
                   collect(DISTINCT tp.name) AS training
            ORDER BY us.id
        """).data()
    drv.close()
    for r in rows:
        extras = []
        if r["scenarios"]: extras.append(f"scenario: {', '.join(r['scenarios'])}")
        if r["training"]:  extras.append(f"training: {', '.join(r['training'])}")
        suffix = f"  [{'; '.join(extras)}]" if extras else ""
        print(f"  {r['id']}  {r['title']}{suffix}")
        for fid in r["features"]:
            print(f"    → {fid}")


def cmd_scenarios(args):
    """Show all scenarios with chapter decomposition status."""
    drv = _driver()
    with drv.session() as s:
        scenarios = s.run("""
            MATCH (sc:SysScenario)
            RETURN sc.id AS id, sc.name AS name, sc.status AS status,
                   sc.audience AS audience, sc.durationS AS dur,
                   sc.aiCallCount AS aiCalls
            ORDER BY sc.id
        """).data()
        for sc in scenarios:
            dur_min   = f"{int(sc['dur'] or 0) // 60}m" if sc.get("dur") else "?"
            ai_tag    = f"  {sc['aiCalls']} AI call{'s' if sc['aiCalls']!=1 else ''}" if sc.get("aiCalls") else ""
            print(f"\n  {sc['id']}  {sc['name']}  [{sc.get('status','?')} · {dur_min}{ai_tag}]")
            if sc.get("audience"):
                print(f"    audience: {', '.join(sc['audience']) if isinstance(sc['audience'], list) else sc['audience']}")
            # Demonstrates links (narrative-intent)
            stories = s.run("""
                MATCH (sc:SysScenario {id:$sid})-[:DEMONSTRATES]->(us:SysUserStory)
                RETURN us.id AS uid, us.title AS title
                ORDER BY us.id
            """, sid=sc["id"]).data()
            for us in stories:
                print(f"    demonstrates: {us['uid']}  {us['title']}")
            # Chapter → feature links (evidence)
            chapters = s.run("""
                MATCH (sc:SysScenario {id:$sid})-[:HAS_CHAPTER]->(ch:SysScenarioChapter)
                OPTIONAL MATCH (ch)-[:EXERCISES]->(f:SysFeature)
                RETURN ch.number AS num, ch.title AS title,
                       collect(f.id) AS features
                ORDER BY ch.number
            """, sid=sc["id"]).data()
            for ch in chapters:
                linked = f"→ {', '.join(ch['features'])}" if ch["features"] else "  (not yet linked)"
                print(f"    Ch {ch['num']}  {ch['title']:<45} {linked}")

        print()
        # Training programs
        programs = s.run("""
            MATCH (tp:SysTrainingProgram)
            RETURN tp.id AS id, tp.name AS name, tp.audience AS audience
            ORDER BY tp.id
        """).data()
        for tp in programs:
            print(f"\n  {tp['id']}  {tp['name']}  [audience: {tp.get('audience','?')}]")
            modules = s.run("""
                MATCH (tp:SysTrainingProgram {id:$pid})-[:HAS_MODULE]->(m:SysTrainingModule)
                OPTIONAL MATCH (m)-[:TEACHES]->(f:SysFeature)
                RETURN m.id AS id, m.title AS title,
                       collect(f.id) AS features
                ORDER BY m.id
            """, pid=tp["id"]).data()
            for m in modules:
                linked = f"→ {', '.join(m['features'])}" if m["features"] else "  (not yet linked)"
                print(f"    {m['id']:<25} {m['title']:<45} {linked}")
    drv.close()


def cmd_link_chapter(args):
    """Link a scenario chapter to a feature it exercises."""
    ch_id = args.chapter
    drv = _driver()
    with drv.session() as s:
        result = s.run(
            "MATCH (ch:SysScenarioChapter {id:$cid}) RETURN ch.title AS title",
            cid=ch_id).single()
        if not result:
            print(f"ERROR: chapter '{ch_id}' not found. Run: sys_edge.py scenarios",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""MATCH (ch:SysScenarioChapter {id:$cid}), (f:SysFeature {id:$fid})
                 MERGE (ch)-[:EXERCISES]->(f)""",
              cid=ch_id, fid=args.feature)
    drv.close()
    print(f"✓ {ch_id}  →  EXERCISES  →  {args.feature}")


def cmd_link_scenario_story(args):
    """Create a DEMONSTRATES edge from a SysScenario to a SysUserStory.

    This is the narrative-intent link: the story the scenario was designed to tell.
    Feature-level coverage (EXERCISES → SysFeature ← REALIZES) is the evidence layer;
    DEMONSTRATES is the summary layer — maintained separately so queries are simpler.
    """
    drv = _driver()
    with drv.session() as s:
        sc = s.run(
            "MATCH (n:SysScenario {id:$id}) RETURN n.name AS name", id=args.scenario
        ).single()
        if not sc:
            print(f"ERROR: scenario '{args.scenario}' not found. Run: sys_edge.py scenarios",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        us = s.run(
            "MATCH (n:SysUserStory {id:$id}) RETURN n.title AS title", id=args.story
        ).single()
        if not us:
            print(f"ERROR: user story '{args.story}' not found. Run: sys_edge.py stories",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""MATCH (sc:SysScenario {id:$sid}), (us:SysUserStory {id:$uid})
                 MERGE (sc)-[:DEMONSTRATES]->(us)""",
              sid=args.scenario, uid=args.story)
    drv.close()
    print(f"✓ {args.scenario}  →  DEMONSTRATES  →  {args.story}")


def cmd_link_training(args):
    """Link a training module to a feature it teaches."""
    drv = _driver()
    with drv.session() as s:
        result = s.run(
            "MATCH (m:SysTrainingModule {id:$mid}) RETURN m.title AS title",
            mid=args.module).single()
        if not result:
            print(f"ERROR: training module '{args.module}' not found. Run: sys_edge.py scenarios",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""MATCH (m:SysTrainingModule {id:$mid}), (f:SysFeature {id:$fid})
                 MERGE (m)-[:TEACHES]->(f)""",
              mid=args.module, fid=args.feature)
    drv.close()
    print(f"✓ {args.module}  →  TEACHES  →  {args.feature}")


# ── Test run recording ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_pytest_json_report(path: str) -> dict:
    """Parse a pytest-json-report (pip install pytest-json-report) output file.

    Returns {test_id: status} where status is 'passed'|'failed'|'skipped'|'xfailed'.
    test_id format matches SysTest.id: '<file>::<class>::<function>' or '<file>::<function>'.
    """
    with open(path) as f:
        report = json.load(f)
    results = {}
    for test in report.get("tests", []):
        node_id = test.get("nodeid", "")
        outcome = test.get("outcome", "")
        # normalise xfailed/xpassed → xfailed for graph consistency
        if outcome in ("xfailed", "xpassed"):
            outcome = "xfailed"
        if node_id and outcome:
            results[node_id] = outcome
    return results


def cmd_record_run(args):
    """Record a test execution result against a test node.

    Called from pytest after-run hooks or manually:
        python3 scripts/sys_edge.py record-run \\
            --package backend/tests/integration \\
            --passed 42 --failed 2 --duration 38.4

    With per-test outcomes (pytest-json-report):
        pytest --json-report --json-report-file=results.json backend/tests/integration/
        python3 scripts/sys_edge.py record-run \\
            --package backend/tests/integration \\
            --passed 42 --failed 0 --duration 38.4 \\
            --results-json results.json
    """
    results_json_path = getattr(args, 'results_json', '') or ''
    per_test_outcomes = {}
    if results_json_path:
        try:
            per_test_outcomes = _parse_pytest_json_report(results_json_path)
        except Exception as e:
            print(f"⚠  Could not parse --results-json '{results_json_path}': {e}", file=sys.stderr)

    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    drv = _driver()
    with drv.session() as s:
        status = "passed" if args.failed == 0 else "failed"
        # Create a SysTestRun node for the package
        s.run("""
            MERGE (pkg:SysTestPackage {id: $pkgId})
            SET pkg.path = $path, pkg.lastRun = $now, pkg.lastPassed = $passed,
                pkg.lastFailed = $failed, pkg.lastSkipped = $skipped,
                pkg.lastXFailed = $xfailed, pkg.lastDuration = $dur,
                pkg.lastStatus = $status
            WITH pkg
            CREATE (run:SysTestRun {
                id: $runId, packageId: $pkgId,
                ranAt: $now, passed: $passed, failed: $failed,
                skipped: $skipped, xfailed: $xfailed,
                durationS: $dur, status: $status, notes: $notes,
                skipReason: $skipReason
            })
            MERGE (pkg)-[:HAS_RUN]->(run)
        """, pkgId=args.package, path=args.package,
             runId=run_id, now=_now_iso(),
             passed=args.passed, failed=args.failed,
             skipped=getattr(args, 'skipped', 0),
             xfailed=getattr(args, 'xfailed', 0),
             dur=args.duration, status=status,
             notes=getattr(args, 'notes', ''),
             skipReason=getattr(args, 'skip_reason', '') or '')

        now = _now_iso()
        if per_test_outcomes:
            # Per-test accuracy: stamp each SysTest with its actual outcome
            per_test_updates = 0
            for node_id, outcome in per_test_outcomes.items():
                res = s.run("""
                    MATCH (t:SysTest {id: $tid})
                    SET t.lastTestedAt = $now, t.lastStatus = $status
                    RETURN count(t) AS cnt
                """, tid=node_id, now=now, status=outcome).single()["cnt"]
                per_test_updates += res
            # Also stamp any remaining package tests that weren't in the report (package fallback)
            updated = s.run("""
                MATCH (pkg:SysTestPackage {id: $pkgId})-[:CONTAINS_TEST]->(t:SysTest)
                WHERE t.lastTestedAt IS NULL OR t.lastTestedAt < $now
                SET t.lastTestedAt = $now, t.lastStatus = $pkgStatus
                RETURN count(t) AS cnt
            """, pkgId=args.package, now=now,
                 pkgStatus="passed" if args.failed == 0 else "failed").single()["cnt"]
            print(f"     per-test outcomes applied: {per_test_updates} matched, "
                  f"{updated} filled from package aggregate")
        else:
            # Package-aggregate fallback: stamp all linked tests with the package outcome
            status_label = "passed" if args.failed == 0 else "failed"
            updated = s.run("""
                MATCH (pkg:SysTestPackage {id: $pkgId})-[:CONTAINS_TEST]->(t:SysTest)
                SET t.lastTestedAt = $now, t.lastStatus = $status
                RETURN count(t) AS cnt
            """, pkgId=args.package, now=now, status=status_label).single()["cnt"]

            if updated == 0:
                # Fallback: file-path prefix match for manually seeded tests without a package link
                updated = s.run("""
                    MATCH (t:SysTest)
                    WHERE (t.file = $pkg OR t.file STARTS WITH $pkg)
                      AND t.packageId IS NULL
                    SET t.lastTestedAt = $now, t.lastStatus = $status
                    RETURN count(t) AS cnt
                """, pkg=args.package, now=now,
                     status=status_label).single()["cnt"]

    drv.close()
    status_str = "✓" if args.failed == 0 else f"✗ {args.failed} failed"
    skip_str   = f"  {getattr(args,'skipped',0)}s" if getattr(args,'skipped',0) else ""
    xfail_str  = f"  {getattr(args,'xfailed',0)}x" if getattr(args,'xfailed',0) else ""
    notes_str  = f"  [{getattr(args,'notes','')}]" if getattr(args,'notes','') else ""
    print(f"  {status_str} {args.passed}✓{skip_str}{xfail_str}  {args.package}{notes_str}")
    if not per_test_outcomes and updated:
        print(f"     updated lastTestedAt on {updated} test nodes")


def cmd_run_history(args):
    """Show pass-rate trend from SysTestRun nodes, with flaky-test detection."""
    instance  = getattr(args, 'instance', '') or ''
    package   = getattr(args, 'package',  '') or ''
    runs_back = getattr(args, 'runs', 10)

    drv = _driver()
    with drv.session() as s:
        if package:
            pkgs = [{"id": package}]
        else:
            inst_filter = "WHERE pkg.instance = $inst" if instance else ""
            pkgs = s.run(f"""
                MATCH (pkg:SysTestPackage)
                {inst_filter}
                WHERE EXISTS {{ MATCH (pkg)-[:HAS_RUN]->(:SysTestRun) }}
                RETURN pkg.id AS id ORDER BY pkg.id
            """, inst=instance).data()

        if not pkgs:
            print("No test packages with run history found.")
            drv.close(); return

        print(f"\nRUN HISTORY  (last {runs_back} runs per package)\n")

        for pkg_row in pkgs:
            pkg_id = pkg_row["id"]
            runs = s.run("""
                MATCH (pkg:SysTestPackage {id:$id})-[:HAS_RUN]->(run:SysTestRun)
                RETURN run.ranAt AS ranAt, run.passed AS passed, run.failed AS failed,
                       run.durationS AS dur, run.status AS status
                ORDER BY run.ranAt DESC LIMIT $n
            """, id=pkg_id, n=runs_back).data()

            if not runs:
                continue

            # Detect flaky runs: any failure in a package that also has passes
            statuses   = [r["status"] for r in runs]
            any_fail   = any(s == "failed" for s in statuses)
            any_pass   = any(s == "passed" for s in statuses)
            flaky_tag  = "  ⚡ FLAKY" if any_fail and any_pass else ""

            pass_rate = int(100 * statuses.count("passed") / len(statuses))
            bar       = ("█" * (pass_rate // 10)).ljust(10, "░")
            print(f"  {pkg_id}")
            print(f"    pass rate: {bar} {pass_rate}%  ({len(runs)} runs){flaky_tag}")

            for r in runs:
                sym  = "✓" if r["status"] == "passed" else "✗"
                ts   = (r["ranAt"] or "")[:16].replace("T", " ")
                dur  = f"{r['dur']:.1f}s" if r.get("dur") else "—"
                p, f = r.get("passed") or 0, r.get("failed") or 0
                print(f"    {sym}  {ts}  {p}✓ {f}✗  {dur}")
            print()

    drv.close()


def cmd_prune_runs(args):
    """Delete SysTestRun nodes older than N days, keeping at least --keep recent runs per package."""
    days = getattr(args, 'days', 30)
    keep = getattr(args, 'keep', 5)
    dry  = getattr(args, 'dry_run', False)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    drv = _driver(admin=True)
    with drv.session() as s:
        # Find candidates: older than cutoff and not in the most-recent `keep` per package
        candidates = s.run("""
            MATCH (pkg:SysTestPackage)-[:HAS_RUN]->(run:SysTestRun)
            WHERE run.ranAt < $cutoff
            WITH pkg, run ORDER BY run.ranAt DESC
            WITH pkg, collect(run) AS runs
            WITH pkg, [r IN runs WHERE r.ranAt < $cutoff] AS old_runs,
                 runs[0..$keep] AS recent
            WITH pkg,
                 [r IN old_runs WHERE NOT r IN recent] AS prunable
            UNWIND prunable AS r
            RETURN r.id AS rid, r.ranAt AS ran, pkg.id AS pkg
            ORDER BY r.ranAt ASC
        """, cutoff=cutoff, keep=keep).data()

        if not candidates:
            print(f"  Nothing to prune (all runs within {days} days or within keep={keep} window).")
            drv.close(); return

        print(f"  {'[DRY RUN] ' if dry else ''}Pruning {len(candidates)} SysTestRun nodes "
              f"(older than {days}d, keeping last {keep} per package):")
        by_pkg: dict = {}
        for c in candidates:
            by_pkg.setdefault(c["pkg"], []).append(c["rid"])

        for pkg_id, rids in by_pkg.items():
            print(f"    {pkg_id}: {len(rids)} runs")

        if not dry:
            run_ids = [c["rid"] for c in candidates]
            s.run("""
                UNWIND $ids AS rid
                MATCH (run:SysTestRun {id: rid})
                DETACH DELETE run
            """, ids=run_ids)
            print(f"\n  ✓ Deleted {len(candidates)} SysTestRun nodes.")
        else:
            print(f"\n  [DRY RUN] Would delete {len(candidates)} nodes — rerun without --dry-run to apply.")

    drv.close()


def cmd_prune_stale_tests(args):
    """Find and remove SysTest→SysFeature VERIFIES edges that are stale.

    Detects two categories:
    - Legacy T- prefix nodes (old link-test format, truncated IDs, no category)
    - Path-format nodes whose referenced Python class no longer exists in the file

    Dry-run by default. Pass --yes to delete without prompting.
    Scope with --feature <prefix> (e.g. F-MST) to limit the scan.
    """
    feat_prefix = getattr(args, 'feature', '') or ''
    dry = getattr(args, 'dry_run', False)
    yes = getattr(args, 'yes', False)

    drv = _driver(admin=True)
    stale = []

    with drv.session() as s:
        # ── 1. T- prefix nodes (legacy link-test format) ─────────────────────
        cypher_filter = "WHERE t.id STARTS WITH 'T-'"
        if feat_prefix:
            cypher_filter += " AND f.id STARTS WITH $prefix"

        old_t = s.run(f"""
            MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature)
            {cypher_filter}
            RETURN t.id AS tid, f.id AS fid, t.category AS cat
            ORDER BY f.id, t.id
        """, prefix=feat_prefix).data()
        for row in old_t:
            stale.append({
                "tid": row["tid"],
                "fid": row["fid"],
                "reason": "legacy T- prefix (old link-test format)",
                "cat": row.get("cat"),
            })

        # ── 2. Path-format nodes whose class no longer exists ─────────────────
        path_filter = "WHERE NOT t.id STARTS WITH 'T-'"
        if feat_prefix:
            path_filter += " AND f.id STARTS WITH $prefix"

        path_tests = s.run(f"""
            MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature)
            {path_filter}
            RETURN t.id AS tid, f.id AS fid, t.category AS cat
            ORDER BY f.id, t.id
        """, prefix=feat_prefix).data()

        class_cache: dict = {}  # file_path -> set of class names | None (unreadable)

        for row in path_tests:
            tid = row["tid"]
            parts = tid.split("::")
            if len(parts) < 3:
                continue  # file::fn format — no class to check

            file_path = parts[0]
            class_name = parts[1]
            if not class_name.startswith("Test"):
                continue

            if file_path not in class_cache:
                full = _root / file_path
                if not full.exists():
                    class_cache[file_path] = None
                else:
                    try:
                        classes = set()
                        for line in full.read_text(encoding="utf-8", errors='replace').splitlines():
                            if line.startswith("class "):
                                cls = line.split("class ", 1)[1].split(":")[0].split("(")[0].strip()
                                classes.add(cls)
                        class_cache[file_path] = classes
                    except Exception:
                        class_cache[file_path] = None

            known = class_cache[file_path]
            if known is None:
                stale.append({
                    "tid": tid,
                    "fid": row["fid"],
                    "reason": f"file not found on disk: {file_path}",
                    "cat": row.get("cat"),
                })
            elif class_name not in known:
                stale.append({
                    "tid": tid,
                    "fid": row["fid"],
                    "reason": f"class {class_name} removed from {file_path}",
                    "cat": row.get("cat"),
                })

    if not stale:
        print("  No stale test links found.")
        drv.close()
        return

    # ── Report ──────────────────────────────────────────────────────────────
    scope = f" (feature prefix: {feat_prefix})" if feat_prefix else ""
    print(f"\nSTALE TEST LINKS{scope}: {len(stale)} found\n")
    by_reason: dict = {}
    for entry in stale:
        by_reason.setdefault(entry['reason'], []).append(entry)

    for reason, items in sorted(by_reason.items()):
        print(f"  ── {reason}  ({len(items)} nodes)")
        for item in sorted(items, key=lambda x: (x['fid'], x['tid'])):
            cat = item.get('cat') or 'no-cat'
            print(f"     [{cat}] {item['tid'][:72]} → {item['fid']}")
        print()

    if dry:
        print("  (dry run — pass --yes to delete without prompting)")
        drv.close()
        return

    if not yes:
        try:
            resp = input(f"Delete {len(stale)} stale VERIFIES edges (and orphan SysTest nodes)? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() != 'y':
            print("Aborted.")
            drv.close()
            return

    tids = list({entry['tid'] for entry in stale})
    with drv.session() as s:
        edge_result = s.run("""
            UNWIND $tids AS tid
            MATCH (t:SysTest {id: tid})-[r:VERIFIES]->(:SysFeature)
            DELETE r
            RETURN count(r) AS n
        """, tids=tids).single()
        edges_deleted = edge_result["n"] if edge_result else 0

        # Delete SysTest nodes that now have no outbound VERIFIES edges
        node_result = s.run("""
            UNWIND $tids AS tid
            MATCH (t:SysTest {id: tid})
            WHERE NOT (t)-[:VERIFIES]->()
            DETACH DELETE t
            RETURN count(t) AS n
        """, tids=tids).single()
        nodes_deleted = node_result["n"] if node_result else 0

    drv.close()
    print(f"\n✓ Removed {edges_deleted} VERIFIES edges, "
          f"deleted {nodes_deleted} orphaned SysTest nodes")


def _rekey_node(session, label: str, old_id: str, new_id: str, props: dict) -> None:
    """Move a node to a new id: copy properties, re-point every relationship, delete the old node.

    If a node with new_id already exists (e.g. re-scanned under the corrected
    path) the old node's relationships are merged into it.
    """
    session.run(f"""
        MATCH (old:{label} {{id:$old}})
        MERGE (new:{label} {{id:$new}})
        ON CREATE SET new += properties(old)
        SET new += $props, new.id = $new
    """, old=old_id, new=new_id, props=props)
    rels = session.run(f"""
        MATCH (old:{label} {{id:$old}})-[r]-(other)
        RETURN type(r) AS t, startNode(r) = old AS outgoing, elementId(other) AS oid,
               properties(r) AS props
    """, old=old_id).data()
    for r in rels:
        if not re.fullmatch(r"[A-Z_]+", r["t"]):
            continue  # relationship types are code-defined; skip anything unexpected
        pattern = f"(new)-[r:{r['t']}]->(o)" if r["outgoing"] else f"(o)-[r:{r['t']}]->(new)"
        session.run(f"""
            MATCH (new:{label} {{id:$new}}), (o) WHERE elementId(o) = $oid
            MERGE {pattern}
            SET r += $props
        """, new=new_id, oid=r["oid"], props=r["props"] or {})
    session.run(f"MATCH (old:{label} {{id:$old}}) DETACH DELETE old", old=old_id)


def cmd_check_test_links(args):
    """Check SysTest / SysTestPackage file paths against this repo and optionally repair them.

    Classifies every test and package path (see _resolve_test_file):
      fixed   — wrong format (absolute, backslashes, missing prefix): re-keyed to the repo-relative path
      missing — file no longer in this repo: node and its VERIFIES links are deleted
      foreign — belongs to another repo (repo tag differs): left untouched
      nofile  — no path recorded: reported only

    Dry run by default; --fix applies the changes (prompting unless --yes).
    """
    fix = getattr(args, "fix", False)
    yes = getattr(args, "yes", False)
    verbose = getattr(args, "verbose", False)
    here = _repo_id()

    drv = _driver(admin=fix)
    with drv.session() as s:
        tests = s.run("""
            MATCH (t:SysTest)
            OPTIONAL MATCH (t)-[v:VERIFIES]->()
            RETURN t.id AS id, t.file AS file, t.repo AS repo, count(v) AS links
        """).data()
        pkgs = s.run("""
            MATCH (p:SysTestPackage)
            RETURN p.id AS id, coalesce(p.path, p.id) AS file, p.repo AS repo
        """).data()

    def classify(rows):
        out = {"ok": [], "fixed": [], "missing": [], "foreign": [], "nofile": []}
        for r in rows:
            stored = r["file"] or _test_file_of(r)
            status, path = _resolve_test_file(stored, r.get("repo") or "")
            if status == "ok" and not r["file"]:
                status = "fixed"  # path only in the id: record it as the file
            r = dict(r, file=r["file"] or stored)
            out[status].append(dict(r, path=path))
        return out

    t_cls, p_cls = classify(tests), classify(pkgs)
    # Legacy T- ids carry no file and are handled by prune-stale-tests
    t_cls["nofile"] = [r for r in t_cls["nofile"] if not r["id"].startswith("T-")]

    linked = sum(1 for r in tests if r["links"])
    bad_links = sum(1 for k in ("fixed", "missing", "foreign", "nofile")
                    for r in t_cls[k] if r["links"])
    print(f"\n  TEST LINK CHECK  (repo: {here})\n")
    print(f"  {'':<10}{'tests':>8}{'with links':>12}{'packages':>10}")
    for k in ("ok", "fixed", "missing", "foreign", "nofile"):
        print(f"  {k:<10}{len(t_cls[k]):>8}{sum(1 for r in t_cls[k] if r['links']):>12}"
              f"{len(p_cls[k]):>10}")
    pct = (100.0 * bad_links / linked) if linked else 0.0
    print(f"\n  {bad_links} of {linked} linked tests ({pct:.1f}%) do not resolve to a file in this repo as stored.")

    for k, title in (("fixed", "path format — will be re-keyed"),
                     ("missing", "file gone — will be deleted"),
                     ("foreign", "other repo — left untouched"),
                     ("nofile", "no path — report only")):
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
        drv.close()
        return
    if not (to_fix or to_del):
        print("\n  ✓ Nothing to repair\n")
        drv.close()
        return
    if not yes:
        try:
            resp = input(f"\n  Re-key {to_fix} and delete {to_del} nodes? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() != "y":
            print("  Aborted.")
            drv.close()
            return

    rekeyed = merged = 0
    with drv.session() as s:
        for label, rows, field in (("SysTestPackage", p_cls["fixed"], "path"),
                                   ("SysTest", t_cls["fixed"], "file")):
            for r in rows:
                new_id = r["id"].replace(r["file"], r["path"], 1) if r["file"] and r["file"] in r["id"] \
                    else _repo_rel_path(r["id"])
                if new_id == r["id"]:
                    s.run(f"MATCH (n:{label} {{id:$id}}) SET n.{field}=$path, n.repo=$repo",
                          id=r["id"], path=r["path"], repo=here)
                else:
                    exists = s.run(f"MATCH (n:{label} {{id:$id}}) RETURN count(n) AS c",
                                   id=new_id).single()["c"]
                    _rekey_node(s, label, r["id"], new_id, {field: r["path"], "repo": here})
                    merged += 1 if exists else 0
                rekeyed += 1
        s.run("""
            UNWIND $tids AS tid
            MATCH (t:SysTest {id: tid}) DETACH DELETE t
        """, tids=[r["id"] for r in t_cls["missing"]])
        s.run("""
            UNWIND $pids AS pid
            MATCH (p:SysTestPackage {id: pid}) DETACH DELETE p
        """, pids=[r["id"] for r in p_cls["missing"]])
        # Tag the remaining in-repo nodes so later checks can tell repos apart
        s.run("UNWIND $ids AS id MATCH (t:SysTest {id:id}) WHERE t.repo IS NULL SET t.repo=$repo",
              ids=[r["id"] for r in t_cls["ok"]], repo=here)
        s.run("UNWIND $ids AS id MATCH (p:SysTestPackage {id:id}) WHERE p.repo IS NULL SET p.repo=$repo",
              ids=[r["id"] for r in p_cls["ok"]], repo=here)
    drv.close()
    print(f"\n  ✓ Re-keyed {rekeyed} nodes ({merged} merged into existing), deleted {to_del}. "
          f"Run a backup to keep the result.\n")


def cmd_test_status(args):
    """Show last-run timestamps per test package and stale test nodes."""
    drv = _driver()
    with drv.session() as s:
        # Filter by instance if specified
        if getattr(args, 'instance', None):
            inst_filter = "WHERE pkg.instance = $inst"
            inst_param  = {"inst": args.instance}
        else:
            inst_filter = ""
            inst_param  = {}

        packages = s.run(f"""
            MATCH (pkg:SysTestPackage)
            {inst_filter}
            RETURN pkg.id AS id, pkg.lastRun AS lastRun,
                   pkg.lastPassed AS passed, pkg.lastFailed AS failed,
                   pkg.lastSkipped AS skipped, pkg.lastXFailed AS xfailed,
                   pkg.lastDuration AS dur, pkg.area AS area, pkg.instance AS inst
            ORDER BY pkg.area, pkg.id
        """, **inst_param).data()

        never   = [p for p in packages if not p["lastRun"]]
        ran     = [p for p in packages if p["lastRun"]]
        failing = [p for p in ran if (p["failed"] or 0) > 0]

        print(f"\nTEST PACKAGES — {len(ran)}/{len(packages)} run  "
              f"({len(failing)} failing  {len(never)} never run)")
        if ran:
            print()
            last_area = None
            for p in sorted(ran, key=lambda x: (x["area"] or "", x["id"])):
                area = p["area"] or "?"
                if area != last_area:
                    print(f"  {area}")
                    last_area = area
                ts     = (p["lastRun"] or "")[:19]
                tick   = "✓" if (p["failed"] or 0) == 0 else "✗"
                dur    = f" {p['dur']:.0f}s" if p.get("dur") else ""
                skip_s = f" {p['skipped']}s" if p.get("skipped") else ""
                xf_s   = f" {p['xfailed']}x" if p.get("xfailed") else ""
                name   = p["id"].split("/")[-1].replace(".py","")
                print(f"    {tick} {name:<55} {ts}  "
                      f"{p['passed'] or 0}✓{skip_s}{xf_s} {p['failed'] or 0}✗{dur}")

        # Tests with no lastTestedAt (never run since graph was populated)
        never = s.run("""
            MATCH (t:SysTest)
            WHERE t.lastTestedAt IS NULL
            RETURN count(t) AS cnt
        """).single()["cnt"]

        stale_cutoff = args.days or 7
        stale = s.run("""
            MATCH (t:SysTest)
            WHERE t.lastTestedAt IS NOT NULL
              AND datetime(t.lastTestedAt) < datetime() - duration({days: $days})
            RETURN count(t) AS cnt
        """, days=stale_cutoff).single()["cnt"]

        print(f"\n  never run: {never} tests")
        print(f"  stale (>{stale_cutoff}d): {stale} tests\n")

    drv.close()


# ── Backup and restore ────────────────────────────────────────────────────────

def _default_arch_standards_path() -> Path | None:
    """Return the path to the bundled arch-standards.yaml, or None if not found."""
    candidates = [
        Path(__file__).parent / "examples" / "arch-standards.yaml",
        # Source layout: scripts/sys_edge.py -> ../tools/sysedge/examples/
        Path(__file__).parent.parent / "tools" / "sysedge" / "examples" / "arch-standards.yaml",
        # Common project-root locations
        Path.cwd() / "tools" / "sysedge" / "examples" / "arch-standards.yaml",
        Path.cwd() / "examples" / "arch-standards.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def cmd_seed_standards(args):
    """Load architecture standards from a YAML file into SysArchStd nodes.

    The YAML is organised by category (execution, development, operations,
    security, infrastructure). Each entry becomes a SysArchStd node linked
    to the SysSystem node via GOVERNED_BY.

    Usage:
        seed-standards                    # use bundled arch-standards.yaml
        seed-standards path/to/custom.yaml
    """
    _require_licence("seed-standards")
    path = Path(args.file) if args.file else _default_arch_standards_path()
    if not path:
        print("ERROR: No standards file specified and bundled arch-standards.yaml not found.",
              file=sys.stderr)
        print("       Run: python3 scripts/sys_edge.py seed-standards <path/to/arch-standards.yaml>",
              file=sys.stderr)
        sys.exit(1)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr); sys.exit(1)
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML not installed — run: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    drv = _driver()
    n_stds = n_edges = 0
    with drv.session() as s:
        sys_node = s.run("MATCH (sys:SysSystem) RETURN sys.id AS id LIMIT 1").single()
        sys_id = sys_node["id"] if sys_node else None

        for category, standards in data.items():
            if not isinstance(standards, list):
                continue
            for std in standards:
                sid = std.get("id") or std.get("name","?").upper().replace(" ","-")
                title = std.get("title") or std.get("name","")
                desc = (std.get("description")
                        or std.get("rationale","")
                        or std.get("complianceNote",""))
                # Upsert the standard node
                s.run("""
                    MERGE (a:SysArchStd {id:$id})
                    SET a.name=$name, a.title=$title, a.category=$cat, a.type=$type,
                        a.source=$src, a.rationale=$rat, a.complianceNote=$note,
                        a.description=$desc,
                        a.tier = COALESCE($tier, a.tier),
                        a.enforcement = COALESCE($enforcement, a.enforcement),
                        a.evaluationMethod = COALESCE($eval, a.evaluationMethod),
                        a.appliesToTypes = COALESCE($types, a.appliesToTypes),
                        a.appliesWhen = COALESCE($when, a.appliesWhen),
                        a.checkKey = COALESCE($check, a.checkKey)
                """, id=sid, name=std.get("name",""), title=title,
                     cat=category, type=std.get("type","standard"),
                     src=std.get("source","internal"),
                     rat=std.get("rationale",""), note=std.get("complianceNote",""),
                     desc=desc, tier=std.get("tier"), enforcement=std.get("enforcement"),
                     eval=std.get("evaluation"), types=std.get("appliesToTypes"),
                     when=std.get("appliesWhen"), check=std.get("checkKey"))
                if sys_id:
                    s.run("MATCH (sys:SysSystem {id:$sid}),(a:SysArchStd {id:$aid}) "
                          "MERGE (sys)-[:GOVERNED_BY]->(a)", sid=sys_id, aid=sid)
                n_stds += 1

                # Create ADDRESSES edges from referenced decisions/patterns
                for ref_id in (std.get("addressed_by") or []):
                    ref_id = str(ref_id).strip()
                    if not ref_id:
                        continue
                    r = s.run("""
                        MATCH (fr) WHERE fr.id = $fid
                        MATCH (to:SysArchStd {id:$tid})
                        MERGE (fr)-[:ADDRESSES]->(to)
                        RETURN count(*) AS c
                    """, fid=ref_id, tid=sid).single()
                    if r and r["c"] > 0:
                        n_edges += 1
                    else:
                        print(f"  WARN: '{ref_id}' not found — ADDRESSES edge for {sid} skipped",
                              file=sys.stderr)

    drv.close()
    print(f"✓ {n_stds} standards seeded, {n_edges} ADDRESSES edges created — from {path}")


def _backup_node_total(export: dict) -> int:
    """Node count of a backup dict (lists of node dicts; edge lists are excluded)."""
    edge_keys = {"_relationships", "blockedBy", "demonstrates", "addresses", "supersedes",
                 "usesPattern", "prerequisiteFor"}
    return sum(len(v) for k, v in export.items() if isinstance(v, list) and k not in edge_keys)


def _do_backup(out_path: Path, allow_empty: bool = False, quiet: bool = False) -> bool:
    """Write a backup to out_path (shared by cmd_backup and the pre-seed auto-backup).

    Returns False without writing when the graph has no SysEdge nodes (a wrong
    URI or a fresh container must not produce a "successful" empty backup),
    unless allow_empty is set.
    """
    drv = _driver()

    export = {"_schema_version": SCHEMA_VERSION}
    with drv.session() as s:
        if not allow_empty and _sys_node_count(s) == 0:
            drv.close()
            return False

        def fetch(label, extra_rels=None):
            rows = s.run(f"MATCH (n:{label}) RETURN n {{ .* }} AS node ORDER BY n.id").data()
            return [r["node"] for r in rows]

        def rels(from_label, rel_type, to_label, from_key="id", to_key="id"):
            return s.run(f"""
                MATCH (a:{from_label})-[:{rel_type}]->(b:{to_label})
                RETURN a.id AS fromId, b.id AS toId
            """).data()

        export["systems"]  = fetch("SysSystem")
        export["domains"]  = fetch("SysDomain")

        # Domains — add systemId from CONTAINS relationship
        sys_dom = {r["toId"]: r["fromId"] for r in rels("SysSystem","CONTAINS","SysDomain")}
        for d in export["domains"]:
            if d["id"] in sys_dom:
                d["systemId"] = sys_dom[d["id"]]

        export["modules"] = fetch("SysModule")
        dom_mod = {r["toId"]: r["fromId"] for r in rels("SysDomain","CONTAINS","SysModule")}
        for m in export["modules"]:
            if m["id"] in dom_mod:
                m["domainId"] = dom_mod[m["id"]]

        export["features"] = fetch("SysFeature")
        mod_feat  = {r["toId"]: r["fromId"] for r in rels("SysModule","PROVIDES","SysFeature")}
        par_feat  = {r["toId"]: r["fromId"] for r in rels("SysFeature","CONTAINS","SysFeature")}
        for f in export["features"]:
            if f["id"] in mod_feat:
                f["moduleId"] = mod_feat[f["id"]]
            if f["id"] in par_feat:
                f["parentId"] = par_feat[f["id"]]

        export["userStories"] = fetch("SysUserStory")
        story_feats = {}
        for r in rels("SysUserStory","REQUIRES","SysFeature"):
            story_feats.setdefault(r["fromId"], []).append(r["toId"])
        for us in export["userStories"]:
            us["requiresFeatures"] = story_feats.get(us["id"], [])

        export["archStandards"] = fetch("SysArchStd")
        std_sys   = {r["toId"]: r["fromId"] for r in rels("SysSystem","GOVERNED_BY","SysArchStd")}
        std_mods  = {}
        for r in rels("SysModule","CONFORMS_TO","SysArchStd"):
            std_mods.setdefault(r["toId"], []).append(r["fromId"])
        for a in export["archStandards"]:
            if a["id"] in std_sys:
                a["systemId"] = std_sys[a["id"]]
            a["appliesTo"] = std_mods.get(a["id"], [])

        # Scenarios + chapters
        scenarios_raw = fetch("SysScenario")
        for sc in scenarios_raw:
            chapters = s.run("""
                MATCH (sc:SysScenario {id:$sid})-[:HAS_CHAPTER]->(ch:SysScenarioChapter)
                OPTIONAL MATCH (ch)-[:EXERCISES]->(f:SysFeature)
                RETURN ch { .* } AS ch, collect(f.id) AS feats
                ORDER BY ch.number
            """, sid=sc["id"]).data()
            sc["chapters"] = []
            for c in chapters:
                node = {k: v for k, v in c["ch"].items() if k not in ("id","scenarioId")}
                node["exercisesFeatures"] = c["feats"]
                sc["chapters"].append(node)
        export["scenarios"] = scenarios_raw

        # Training programs + modules
        programs_raw = fetch("SysTrainingProgram")
        for tp in programs_raw:
            modules = s.run("""
                MATCH (tp:SysTrainingProgram {id:$pid})-[:HAS_MODULE]->(m:SysTrainingModule)
                OPTIONAL MATCH (m)-[:TEACHES]->(f:SysFeature)
                RETURN m { .* } AS m, collect(f.id) AS feats
                ORDER BY m.id
            """, pid=tp["id"]).data()
            tp["modules"] = []
            for mod in modules:
                node = {k: v for k, v in mod["m"].items() if k not in ("programId",)}
                node["teachesFeatures"] = mod["feats"]
                tp["modules"].append(node)
        export["trainingPrograms"] = programs_raw

        # Test packages (registry with run history)
        export["testPackages"] = fetch("SysTestPackage")

        # Test nodes with both VERIFIES (feature links) and packageId (CONTAINS_TEST)
        export["tests"] = fetch("SysTest")

        # VERIFIES edges: test → feature
        test_feats = {}
        for r in rels("SysTest","VERIFIES","SysFeature"):
            test_feats.setdefault(r["fromId"], []).append(r["toId"])
        for t in export["tests"]:
            t["verifiesFeatures"] = test_feats.get(t["id"], [])

        export["endpoints"] = fetch("SysEndpoint")
        ep_feat = {r["fromId"]: r["toId"] for r in rels("SysEndpoint","IMPLEMENTS","SysFeature")}
        for e in export["endpoints"]:
            if e["id"] in ep_feat:
                e["implementsFeature"] = ep_feat[e["id"]]

        export["symbols"] = fetch("SysSymbol")
        sym_feat = {r["fromId"]: r["toId"] for r in rels("SysSymbol","IMPLEMENTS","SysFeature")}
        for sym in export["symbols"]:
            if sym["id"] in sym_feat:
                sym["implementsFeature"] = sym_feat[sym["id"]]

        export["usesPattern"] = [
            {"moduleId": r["fromId"], "patternId": r["toId"]}
            for r in rels("SysModule","USES_PATTERN","SysModule")
        ]

        # Directory → test category mappings (fully self-contained, no edges)
        export["dirCategories"] = fetch("SysDirCategory")

        # ── Previously missing — now fully captured ──────────────────────────

        # Users + their INITIATES→stories and USES→apps edges
        export["users"] = fetch("SysUser")
        user_stories, user_apps = {}, {}
        for r in rels("SysUser","INITIATES","SysUserStory"):
            user_stories.setdefault(r["fromId"], []).append(r["toId"])
        for r in rels("SysUser","USES","SysApplication"):
            user_apps.setdefault(r["fromId"], []).append(r["toId"])
        for u in export["users"]:
            u["initiatesStories"] = user_stories.get(u["id"], [])
            u["usesApps"]         = user_apps.get(u["id"], [])

        # Applications + their CONTAINS→modules edges
        export["applications"] = fetch("SysApplication")
        app_mods = {}
        for r in rels("SysApplication","CONTAINS","SysModule"):
            app_mods.setdefault(r["fromId"], []).append(r["toId"])
        for a in export["applications"]:
            a["containsModules"] = app_mods.get(a["id"], [])

        # Enhancements + EXTENDS→feature edges
        export["enhancements"] = fetch("SysEnhancement")
        enh_feats = {}
        for r in rels("SysEnhancement","EXTENDS","SysFeature"):
            enh_feats.setdefault(r["fromId"], []).append(r["toId"])
        for e in export["enhancements"]:
            e["extendsFeatures"] = enh_feats.get(e["id"], [])

        # Defects + AFFECTS→feature and DETECTED_IN→module edges
        export["defects"] = fetch("SysDefect")
        def_feats, def_mods = {}, {}
        for r in rels("SysDefect","AFFECTS","SysFeature"):
            def_feats.setdefault(r["fromId"], []).append(r["toId"])
        for r in rels("SysDefect","DETECTED_IN","SysModule"):
            def_mods.setdefault(r["fromId"], []).append(r["toId"])
        for d in export["defects"]:
            d["affectsFeatures"]    = def_feats.get(d["id"], [])
            d["detectedInModules"]  = def_mods.get(d["id"], [])

        # Use cases + REQUIRES→feature and story REALIZED_BY edges
        export["useCases"] = fetch("SysUseCase")
        uc_feats, uc_story = {}, {}
        for r in rels("SysUseCase","REQUIRES","SysFeature"):
            uc_feats.setdefault(r["fromId"], []).append(r["toId"])
        for r in rels("SysUserStory","REALIZED_BY","SysUseCase"):
            uc_story[r["toId"]] = r["fromId"]
        for uc in export["useCases"]:
            uc["requiresFeatures"] = uc_feats.get(uc["id"], [])
            if uc["id"] in uc_story:
                uc["storyId"] = uc_story[uc["id"]]

        # Architecture decisions + CONSTRAINED_BY edges (module → ADR)
        export["archDecisions"] = fetch("SysArchDecision")
        adr_mods, adr_sys = {}, {}
        for r in rels("SysModule","CONSTRAINED_BY","SysArchDecision"):
            adr_mods.setdefault(r["toId"], []).append(r["fromId"])
        for r in rels("SysSystem","GOVERNED_BY","SysArchDecision"):
            adr_sys[r["toId"]] = r["fromId"]
        for a in export["archDecisions"]:
            a["constrainsModules"] = adr_mods.get(a["id"], [])
            if a["id"] in adr_sys:
                a["systemId"] = adr_sys[a["id"]]

        # Module prerequisite edges (toolLayer ordering)
        export["prerequisiteFor"] = [
            {"fromId": r["fromId"], "toId": r["toId"]}
            for r in rels("SysModule","PREREQUISITE_FOR","SysModule")
        ]

        # Feedback entries
        export["feedback"] = fetch("SysFeedback")

        # Proposals + EXTENDS→feature edges
        export["proposals"] = fetch("SysProposal")
        prop_feats = {}
        for r in rels("SysProposal", "EXTENDS", "SysFeature"):
            prop_feats.setdefault(r["fromId"], []).append(r["toId"])
        for p in export["proposals"]:
            p["extendsFeatures"] = prop_feats.get(p["id"], [])

        # Notes (instance memory — active + archived)
        export["notes"] = fetch("SysNote")

        # Test runs
        export["testRuns"] = fetch("SysTestRun")

        # Doc artifacts
        export["docArtifacts"] = fetch("SysDocArtifact")

        # Compliance: append-only change log and baseline records
        export["changes"] = fetch("SysChange")
        export["baselines"] = fetch("SysBaseline")

        # Cross-cutting relationship edges not captured via node fetch
        export["blockedBy"] = [
            {"fromId": r["fromId"], "toId": r["toId"]}
            for r in rels("SysEnhancement", "BLOCKED_BY", "SysEnhancement")
        ]
        export["demonstrates"] = [
            {"fromId": r["fromId"], "toId": r["toId"]}
            for r in rels("SysScenarioChapter", "DEMONSTRATES", "SysFeature")
        ]
        export["addresses"] = [
            {"fromId": r["fromId"], "toId": r["toId"]}
            for r in rels("SysArchDecision", "ADDRESSES", "SysArchStd")
        ]
        export["supersedes"] = [
            {"fromId": r["fromId"], "toId": r["toId"]}
            for r in rels("SysArchDecision", "SUPERSEDES", "SysArchDecision")
        ]

        # Every relationship between Sys* nodes, generically, so edge types
        # without a dedicated field above (HAS_RUN, NOTED_ON, FEEDBACK_ON, …)
        # survive a restore.
        export["_relationships"] = s.run("""
            MATCH (a)-[r]->(b)
            WHERE a.id IS NOT NULL AND b.id IS NOT NULL
              AND any(l IN labels(a) WHERE l STARTS WITH 'Sys')
              AND any(l IN labels(b) WHERE l STARTS WITH 'Sys')
            RETURN head([l IN labels(a) WHERE l STARTS WITH 'Sys']) AS fromLabel, a.id AS from,
                   type(r) AS type, properties(r) AS props,
                   head([l IN labels(b) WHERE l STARTS WITH 'Sys']) AS toLabel, b.id AS to
            ORDER BY fromLabel, from, type, toLabel, to
        """).data()

        # Counts for summary
        total = sum(len(v) for v in export.values() if isinstance(v, list))

    drv.close()

    # Warn when the graph shrank sharply since the last backup
    nodes_now = _backup_node_total(export)
    try:
        previous = sorted(out_path.parent.glob("sys-backup-*.json"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        previous = [p for p in previous if p.resolve() != out_path.resolve()]
        if previous:
            prev_nodes = _backup_node_total(json.loads(previous[0].read_text(encoding="utf-8")))
            if prev_nodes and nodes_now < 0.8 * prev_nodes:
                print(f"  ⚠  {nodes_now} nodes vs {prev_nodes} in {previous[0].name} "
                      f"({100 - 100 * nodes_now // prev_nodes}% fewer) — check the connection "
                      f"settings before relying on this backup", file=sys.stderr)
    except (OSError, ValueError):
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")

    if quiet:
        return True
    print(f"✓ Backup written to {out_path}")
    for k, v in sorted(export.items()):
        if isinstance(v, list) and v:
            print(f"  {k:<25} {len(v)}")
    print(f"  total nodes/edges: {total}")
    return True


_RESET_PHRASE = "DELETE ALL SYS NODES"


def _read_from_terminal(prompt: str) -> str | None:
    """Read a line from the controlling terminal, or None when there is none.

    Piped stdin is deliberately ignored, so an agent cannot confirm by
    `echo PHRASE | sys_edge.py reset`.
    """
    if not sys.stdin.isatty():
        return None
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip()
    except OSError:
        return None


def cmd_reset(args):
    """Delete every SysEdge node (SysSchema kept) after a verified backup and a typed confirmation."""
    drv = _driver(admin=True)
    with drv.session() as s:
        counts = s.run("""
            MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'Sys') AND NOT n:SysSchema
            RETURN head([l IN labels(n) WHERE l STARTS WITH 'Sys']) AS label, count(*) AS c
            ORDER BY label
        """).data()
    total = sum(r["c"] for r in counts)
    if not total:
        print("  Graph has no SysEdge nodes — nothing to reset.")
        drv.close()
        return

    print("\n  WIPE ALL SysEdge NODES — every agent session loses its context until re-seeded.\n")
    for r in counts:
        print(f"    {r['label']:<28} {r['c']}")
    print(f"    {'total':<28} {total}\n")

    if not sys.stdin.isatty():
        print("✗ reset needs an interactive terminal (stdin is not a TTY). Run it yourself in a shell.",
              file=sys.stderr)
        drv.close()
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup = Path(f"data/sys-prereset-{ts}.json")
    if not _do_backup(backup, quiet=True):
        print("✗ Pre-reset backup failed — graph unchanged.", file=sys.stderr)
        drv.close()
        sys.exit(1)
    print(f"  ✓ Pre-reset backup → {backup}\n")

    answer = _read_from_terminal(f"  Type exactly '{_RESET_PHRASE}' to proceed: ")
    if answer != _RESET_PHRASE:
        print("  Aborted — graph unchanged.")
        drv.close()
        return

    with drv.session() as s:
        wiped = _wipe_sys_nodes(s)
    drv.close()
    print(f"\n✓ Deleted {wiped} SysEdge nodes. Restore with:  sys_edge.py seed {backup} --full-restore")


def _set_config_value(key: str, value: str) -> Path:
    """Set a top-level scalar in sysedge.yaml, keeping comments and other keys."""
    path = _root / "sysedge.yaml"
    text = path.read_text(encoding="utf-8") if path.exists() else \
        "# SysEdge project settings (see sys_edge.py config --help)\n"
    line = f"{key}: {value}"
    pattern = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
    text = pattern.sub(line, text, count=1) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")
    _project_config.cache_clear()
    return path


def cmd_config(args):
    """Show or change project settings in sysedge.yaml."""
    if args.action == "get":
        cfg = _project_config()
        keys = [args.key] if args.key else sorted(cfg) or ["model"]
        for k in keys:
            print(f"{k}: {cfg.get(k, 'full' if k == 'model' else '')}")
        return
    if args.key != "model" or args.value not in ("lightweight", "full"):
        print("ERROR: supported: config set model lightweight|full", file=sys.stderr)
        sys.exit(1)
    path = _set_config_value("model", args.value)
    print(f"✓ model: {args.value}  ({path})")


def _load_mcp_module():
    import importlib.util as _ilu
    path = Path(os.path.dirname(os.path.abspath(__file__))) / "sys_edge_mcp.py"
    if not path.exists():
        print("ERROR: sys_edge_mcp.py not found next to sys_edge.py", file=sys.stderr)
        sys.exit(1)
    spec = _ilu.spec_from_file_location("sys_edge_mcp", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_mcp(args):
    """Run SysEdge as an MCP server on stdio (for any MCP-capable agent)."""
    _load_mcp_module().serve()


def cmd_register_mcp(args):
    """Register the SysEdge MCP server in the project's .mcp.json (Claude Code project scope)."""
    target = _root / ".mcp.json"
    try:
        config = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except ValueError:
        print(f"ERROR: {target} is not valid JSON — fix or remove it first", file=sys.stderr)
        sys.exit(1)
    script = Path(os.path.abspath(__file__)).with_name("sys_edge_mcp.py")
    try:
        script_arg = str(script.relative_to(_root.resolve()))
    except ValueError:
        script_arg = str(script)
    if os.environ.get("SYSEDGE_ENTRYPOINT"):   # installed with pip/pipx: `sysedge mcp`
        entry = {"command": os.environ["SYSEDGE_ENTRYPOINT"], "args": ["mcp"]}
    else:
        entry = {"command": "python3", "args": [script_arg.replace("\\", "/")]}
    config.setdefault("mcpServers", {})["sysedge"] = entry
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"✓ Registered MCP server 'sysedge' in {target}")
    print("  Other clients: add the same mcpServers entry to their MCP configuration.")


def cmd_backup(args):
    """Export all Sys* nodes and relationships to a JSON file (seed-compatible format)."""
    ts       = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = Path(args.output) if args.output else Path(f"data/sys-backup-{ts}.json")
    if not _do_backup(out_path, allow_empty=getattr(args, "allow_empty", False)):
        print("✗ No SysEdge nodes in the graph — nothing backed up (no file written).\n"
              "  Check SYSEDGE_NEO4J_URI, or pass --allow-empty to write an empty backup.",
              file=sys.stderr)
        sys.exit(1)

@functools.lru_cache(maxsize=None)
def _repo_id() -> str:
    """Stable identity of the repo this CLI runs in.

    SYSEDGE_REPO overrides; otherwise owner/name from the git origin URL
    (e.g. acme/app); else the repo folder name.
    """
    rid = os.environ.get("SYSEDGE_REPO", "").strip()
    if not rid:
        try:
            url = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                                 capture_output=True, text=True, cwd=_root,
                                 timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            url = ""
        # owner/name is stable across https, ssh and proxy remotes
        parts = [x for x in re.split(r"[/:]", re.sub(r"\.git$", "", url)) if x]
        if len(parts) >= 2:
            rid = "/".join(parts[-2:])
    return rid or _root.name


def _repo_rel_path(path) -> str:
    """Normalise a path to repo-relative POSIX form.

    Backslashes become '/', absolute paths under the repo root are made
    relative, and a leading './' is dropped. Paths outside the repo are
    returned normalised but still absolute.
    """
    p = str(path or "").strip().replace("\\", "/")
    if not p:
        return ""
    root = str(_root.resolve()).replace("\\", "/").rstrip("/") + "/"
    if re.match(r"^[A-Za-z]:/", p) or p.startswith("/"):
        try:
            resolved = str(Path(p).resolve()).replace("\\", "/")
        except OSError:
            resolved = p
        for candidate in (p, resolved):
            if candidate.startswith(root):
                return candidate[len(root):]
        return p
    while p.startswith("./"):
        p = p[2:]
    return p


@functools.lru_cache(maxsize=None)
def _repo_file_index() -> dict:
    """basename -> [repo-relative paths] of tracked files (cached; git ls-files)."""
    index: dict = {}
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=_root, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for rel in out.splitlines():
        index.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    return index


def _test_file_of(node: dict) -> str:
    """Stored file of a SysTest, falling back to the path prefix of a `path::name` id."""
    tid = str(node.get("id") or "")
    return node.get("file") or (tid.split("::", 1)[0] if "::" in tid else "")


def _resolve_test_file(file, repo: str = "") -> tuple:
    """Classify a stored test path. Returns (status, path).

    ok      — path exists in this repo as stored
    fixed   — exists after normalisation, or as the unique tracked file ending
              with the stored path (e.g. 'internal/x_test.go' → 'backend/internal/x_test.go')
    foreign — tagged with another repo, or an absolute path outside this repo
    missing — should be in this repo but the file is gone
    nofile  — no path recorded
    """
    raw = str(file or "").strip()
    if not raw:
        return "nofile", ""
    if repo and repo != _repo_id():
        return "foreign", raw
    rel = _repo_rel_path(raw)
    if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        return "foreign", rel
    if (_root / rel).exists():
        return ("ok" if rel == raw else "fixed"), rel
    matches = [m for m in _repo_file_index().get(rel.rsplit("/", 1)[-1], [])
               if m.endswith("/" + rel)]
    if len(matches) == 1:
        return "fixed", matches[0]
    return "missing", rel


# ── Test file scanner ────────────────────────────────────────────────────────

_TS_DESCRIBE = __import__("re").compile(
    r"""(?:^|\s)(?:describe|describe\.only|describe\.skip)\s*\(\s*['"`]([^'"`]+)['"`]""",
    __import__("re").MULTILINE)
_TS_IT = __import__("re").compile(
    r"""(?:^|\s)(?:it|test|it\.only|test\.only|it\.skip|test\.skip)\s*\(\s*['"`]([^'"`]+)['"`]""",
    __import__("re").MULTILINE)

def _extract_tests_from_ts_file(file_path: Path, pkg_id: str, test_type: str) -> list[dict]:
    """Extract vitest/Jest test names from a TypeScript/TSX test file using regex."""
    rel = _repo_rel_path(file_path)
    results = []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  WARN: could not read {file_path}: {e}", file=sys.stderr)
        return results

    # Build a list of (line_number, name, is_describe) pairs
    by_line = []
    for m in _TS_DESCRIBE.finditer(source):
        line = source[:m.start()].count("\n") + 1
        by_line.append((line, m.group(1), True))
    for m in _TS_IT.finditer(source):
        line = source[:m.start()].count("\n") + 1
        by_line.append((line, m.group(1), False))
    by_line.sort()

    # Pair each it/test with the most recent preceding describe block (if any)
    current_describe = ""
    for line, name, is_describe in by_line:
        if is_describe:
            current_describe = name
        else:
            fn_name = f"{current_describe} > {name}" if current_describe else name
            node_id = f"{rel}::{fn_name}"
            results.append({
                "id":        node_id,
                "file":      rel,
                "function":  fn_name,
                "className": current_describe or "",
                "type":      test_type,
                "testType":  test_type,
                "line":      line,
                "packageId": pkg_id,
            })
    return results


def _extract_tests_from_file(file_path: Path, pkg_id: str, test_type: str) -> list[dict]:
    """Parse a Python test file with AST and return a list of test descriptors."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree   = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        print(f"  WARN: could not parse {file_path}: {e}", file=sys.stderr)
        return []

    rel_path = _repo_rel_path(file_path)
    results  = []

    for node in ast.walk(tree):
        # Class-level test methods
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in ast.walk(node):
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith("test_")
                        and item is not node):
                    # Only direct methods (not nested classes)
                    if any(item is m for m in node.body
                           if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))):
                        node_id = f"{rel_path}::{node.name}::{item.name}"
                        results.append({
                            "id":        node_id,
                            "file":      rel_path,
                            "function":  item.name,
                            "className": node.name,
                            "type":      test_type,
                            "line":      item.lineno,
                            "packageId": pkg_id,
                        })

    # Module-level test functions (not inside any class)
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    top_level   = {n.name: n for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, node in top_level.items():
        if name.startswith("test_"):
            node_id = f"{rel_path}::{name}"
            results.append({
                "id":        node_id,
                "file":      rel_path,
                "function":  name,
                "className": "",
                "type":      test_type,
                "line":      node.lineno,
                "packageId": pkg_id,
            })

    return results


# Regex for entity IDs that may appear in test comments/names.
# Matches: UC-ENV-001, US-001, F-DOC-001, etc.
_TEST_ENTITY_REF_RE = __import__("re").compile(
    r"\b(UC-[A-Z0-9]+-\d+|US-[A-Z0-9]+-\d+|F-[A-Z0-9]+-\d+|UC-\d+|US-\d+|F-\d+)\b"
)


def _collect_graph_entity_ids(session) -> set[str]:
    """Return the set of all SysUseCase, SysUserStory, and SysFeature ids in the graph."""
    rows = session.run("""
        MATCH (n) WHERE n:SysUseCase OR n:SysUserStory OR n:SysFeature
        RETURN n.id AS id
    """).data()
    return {r["id"] for r in rows}


def _extract_test_entity_refs(source: str, valid_ids: set[str]) -> list[str]:
    """Scan test source for references to existing UC/US/Feature IDs."""
    refs = []
    seen = set()
    for m in _TEST_ENTITY_REF_RE.finditer(source):
        eid = m.group(1)
        if eid in valid_ids and eid not in seen:
            seen.add(eid)
            refs.append(eid)
    return refs


def _load_candidate_entities(session, module_id: str = "", instance: str = "") -> list[dict]:
    """Return UC/US/Feature entities that could plausibly be verified by tests.

    If module_id is given, prefer entities linked to that module. If instance is
    given and no module match, fall back to entities in that instance. Always
    include all entities as a last resort so the AI can do cross-module mapping.
    """
    params = {"module": module_id or "", "inst": instance or ""}

    # module-linked features/use-cases
    rows = session.run("""
        MATCH (f:SysFeature)
        WHERE $module <> '' AND EXISTS {
            MATCH (f)<-[:PROVIDES]-(:SysModule {id: $module})
        }
        RETURN f.id AS id, f.name AS name, f.description AS description,
               labels(f)[0] AS type, 'module' AS scope
        ORDER BY f.id
    """, **params).data()

    rows += session.run("""
        MATCH (uc:SysUseCase)
        WHERE $module <> '' AND EXISTS {
            MATCH (uc)-[:REQUIRES]->(:SysFeature)<-[:PROVIDES]-(:SysModule {id: $module})
        }
        RETURN uc.id AS id, uc.title AS name, uc.description AS description,
               labels(uc)[0] AS type, 'module' AS scope
        ORDER BY uc.id
    """, **params).data()

    rows += session.run("""
        MATCH (us:SysUserStory)
        WHERE $module <> '' AND EXISTS {
            MATCH (us)-[:REQUIRES]->(:SysFeature)<-[:PROVIDES]-(:SysModule {id: $module})
        }
        RETURN us.id AS id, us.title AS name, us.description AS description,
               labels(us)[0] AS type, 'module' AS scope
        ORDER BY us.id
    """, **params).data()

    # instance-wide fallback
    if instance:
        rows += session.run("""
            MATCH (f:SysFeature)
            WHERE $inst <> '' AND EXISTS {
                MATCH (f)<-[:PROVIDES]-(:SysModule {instance: $inst})
            }
            AND NOT f.id IN $known
            RETURN f.id AS id, f.name AS name, f.description AS description,
                   labels(f)[0] AS type, 'instance' AS scope
            ORDER BY f.id
        """, **params, known=[r["id"] for r in rows]).data()

    # global fallback
    known = [r["id"] for r in rows]
    rows += session.run("""
        MATCH (n) WHERE n:SysUseCase OR n:SysUserStory OR n:SysFeature
        AND NOT n.id IN $known
        RETURN n.id AS id, coalesce(n.title, n.name) AS name,
               n.description AS description, labels(n)[0] AS type,
               'global' AS scope
        ORDER BY n.id
    """, known=known).data()

    # deduplicate
    seen = set()
    out = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    return out


def _emit_ai_analysis_request(args, file_tests: list, inst_filter: str):
    """Build and emit a JSON request for the assistant to analyse unlinked tests.

    The request contains the candidate entities, test files, test names, and a
    prompt. The assistant runs the analysis in its own session (using its own
    model and token budget), writes the response JSON, and the user then calls
    scan-tests --apply-ai-results <file>.
    """
    max_files = getattr(args, "max_files", 50)
    if max_files > 0 and len(file_tests) > max_files:
        file_tests = file_tests[:max_files]

    drv = _driver()
    with drv.session() as s:
        candidates = _load_candidate_entities(
            s, module_id=getattr(args, "module", "") or "",
            instance=inst_filter)
    drv.close()

    if not candidates:
        print("  No candidate UC/US/Feature entities found — nothing to analyse.")
        return

    # Read source for each file, truncated to keep the request bounded
    files_payload = []
    for file_path, tests in file_tests:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  WARN: could not read {file_path}: {e}", file=sys.stderr)
            continue
        MAX_SOURCE = 12000
        if len(source) > MAX_SOURCE:
            source = source[:MAX_SOURCE] + "\n\n[...source truncated for analysis...]"
        files_payload.append({
            "file": str(file_path),
            "tests": [{"id": t["id"], "function": t["function"]} for t in tests],
            "source": source,
        })

    request = {
        "schemaVersion": 1,
        "instruction": "Analyse each test function and map it to the UC/US/Feature entities it verifies.",
        "responseFormat": {
            "mappings": [
                {
                    "testId": "full SysTest id from files[].tests[].id",
                    "entityId": "candidate entity id",
                    "confidence": "0.0-1.0",
                    "coverage": "none | partial | full",
                    "paths": ["happy"],
                    "missingPaths": ["error", "boundary"],
                    "reason": "one-sentence justification"
                }
            ]
        },
        "rules": [
            "Only include mappings where confidence >= 0.5.",
            "coverage = 'full' only if the test exercises all obvious paths (happy + expected error/boundary cases). Use 'partial' for happy-path-only or single-aspect tests.",
            "paths and missingPaths are short labels like ['happy'], ['validation-error'], ['auth-denied'], ['empty-state'], ['boundary'].",
            "If a test verifies no candidate entity, omit it from mappings."
        ],
        "candidates": [
            {
                "id": c["id"],
                "type": c["type"],
                "name": (c.get("name") or "").strip() or "(no title)",
                "description": (c.get("description") or "").strip()[:300]
            }
            for c in candidates
        ],
        "files": files_payload,
    }

    # Build a human-readable prompt for copy/paste into the assistant session
    prompt_lines = [
        "You are a requirements-traceability analyst. Review the test files below and map each test function to the UC/US/Feature entities it verifies.",
        "",
        "Return STRICT JSON in this format:",
        json.dumps(request["responseFormat"], indent=2),
        "",
        "Rules:",
        "- Only include mappings where confidence >= 0.5.",
        "- coverage = 'full' only if the test exercises all obvious paths (happy + expected error/boundary cases).",
        "- Use 'partial' for happy-path-only or single-aspect tests.",
        "- paths / missingPaths are short labels: ['happy'], ['validation-error'], ['auth-denied'], ['empty-state'], ['boundary'].",
        "- Omit tests that verify no candidate entity.",
        "",
        "Candidate entities:",
    ]
    for c in request["candidates"]:
        prompt_lines.append(f"- {c['id']} [{c['type']}]: {c['name']}")
        if c["description"]:
            prompt_lines.append(f"    {c['description']}")
    prompt_lines.append("")
    prompt_lines.append("Test files:")
    for f in files_payload:
        prompt_lines.append(f"\n### {f['file']}")
        for t in f["tests"]:
            prompt_lines.append(f"- {t['function']}")
        prompt_lines.append("\n```typescript")
        prompt_lines.append(f["source"])
        prompt_lines.append("```")

    request["prompt"] = "\n".join(prompt_lines)

    output_path = getattr(args, "output_request", "") or ""
    if output_path:
        Path(output_path).write_text(json.dumps(request, indent=2), encoding="utf-8")
        print(f"\n  AI analysis request written to: {output_path}")
    else:
        print("\n" + "="*62)
        print("  AI ANALYSIS REQUEST")
        print("="*62)
        print(request["prompt"])
        print("\n" + "="*62)
        print("  MACHINE-READABLE REQUEST JSON")
        print("="*62)
        print(json.dumps(request, indent=2))

    print("\n  Next step: run the analysis in your AI session, save the result JSON,")
    print("  then apply it with:")
    print(f"    python3 scripts/sys_edge.py scan-tests --apply-ai-results <result.json>")


def _apply_ai_results(path: str) -> int:
    """Read assistant-produced JSON mappings and write VERIFIES edges."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: result file not found: {path}", file=sys.stderr)
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not parse {path}: {e}", file=sys.stderr)
        return 0

    mappings = data.get("mappings", []) if isinstance(data, dict) else data
    if not mappings:
        print("  No mappings found in result file.")
        return 0

    # Normalise field names (assistant may use camelCase or snake_case)
    normalised = []
    for m in mappings:
        test_id = m.get("testId") or m.get("test_id") or ""
        entity_id = m.get("entityId") or m.get("entity_id") or ""
        conf = float(m.get("confidence", 0))
        if not test_id or not entity_id or conf < 0.5:
            continue
        normalised.append({
            "test_id": test_id,
            "entity_id": entity_id,
            "confidence": conf,
            "coverage": m.get("coverage", "partial"),
            "paths": m.get("paths", []),
            "missingPaths": m.get("missingPaths", []),
            "reason": m.get("reason", ""),
        })

    drv = _driver()
    with drv.session() as s:
        _upsert_ai_mappings(s, normalised)
    drv.close()
    print(f"  Applied {len(normalised)} AI mappings from {path}")
    return len(normalised)


def _upsert_ai_mappings(session, mappings: list[dict]) -> int:
    """Write AI-generated VERIFIES edges with coverage metadata."""
    if not mappings:
        return 0
    session.run("""
        UNWIND $mappings AS m
        MATCH (t:SysTest {id: m.test_id})
        MATCH (x) WHERE (x:SysUseCase OR x:SysUserStory OR x:SysFeature)
                  AND x.id = m.entity_id
        MERGE (t)-[r:VERIFIES]->(x)
        SET r.confidence   = m.confidence,
            r.coverage     = m.coverage,
            r.paths        = m.paths,
            r.missingPaths = m.missingPaths,
            r.reason       = m.reason,
            r.source       = 'ai',
            r.analysedAt   = datetime().epochMillis
    """, mappings=mappings)

    # Also mirror a summary onto the test node for cheap reporting
    session.run("""
        UNWIND $mappings AS m
        MATCH (t:SysTest {id: m.test_id})
        SET t.aiCoverageConfidence = m.confidence,
            t.aiCoverageSummary    = m.coverage + ': ' + m.reason
    """, mappings=mappings)
    return len(mappings)


def _upsert_tests(session, tests: list[dict]) -> int:
    """Bulk-upsert SysTest nodes and CONTAINS_TEST edges. Returns count created."""
    if not tests:
        return 0
    session.run("""
        UNWIND $tests AS t
        MERGE (n:SysTest {id: t.id})
        SET n.file      = t.file,
            n.function  = t.function,
            n.className = t.className,
            n.type      = t.type,
            n.testType  = t.type,
            n.line      = t.line,
            n.packageId = t.packageId,
            n.repo      = $repo
        WITH n, t
        MATCH (pkg:SysTestPackage {id: t.packageId})
        MERGE (pkg)-[:CONTAINS_TEST]->(n)
    """, tests=tests, repo=_repo_id())

    # Create VERIFIES edges to any UC/US/Feature IDs referenced by the test.
    verifies = []
    for t in tests:
        for eid in t.get("verifies", []):
            verifies.append({"tid": t["id"], "eid": eid})
    if verifies:
        session.run("""
            UNWIND $edges AS e
            MATCH (t:SysTest {id: e.tid})
            MATCH (x) WHERE (x:SysUseCase OR x:SysUserStory OR x:SysFeature)
                      AND x.id = e.eid
            MERGE (t)-[:VERIFIES]->(x)
        """, edges=verifies)
    return len(tests)


def cmd_scan_go_tests(args):
    """Scan Go *_test.go files and register test functions as SysTest nodes.

    testType is derived from dir-category mappings (longest-prefix-first), with
    the filename heuristic (*_test.go → component) as the fallback.  Tests in
    tests/ or kernel/ subdirectories therefore register as component type and
    satisfy the component-gap tier in test-gaps without needing a separate scan.
    """
    root = Path(args.path) if args.path else Path(_go_root())
    if not root.exists():
        if not getattr(args, "path", None):
            print(f"ERROR: default path '{root}' not found — pass --path <go-root-dir> "
                  f"for non-standard layouts (e.g. --path kernel/ for pure-Go projects)",
                  file=sys.stderr)
        else:
            print(f"ERROR: {root} not found", file=sys.stderr)
        sys.exit(1)

    drv    = _driver()
    total  = 0
    files  = 0
    total_links = 0

    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)
        valid_entity_ids = _collect_graph_entity_ids(s)

        _go_files = [root] if root.is_file() else sorted(root.rglob("*_test.go"))
        for f in _go_files:
            # Only skip directory-level exclusions — _test.go is intentionally included here
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            rel   = _repo_rel_path(f)
            pkg_id = rel  # use file path as package ID

            # Derive category and instance from graph config — never hardcoded
            forced_cat = getattr(args, "category", "") or ""
            category = forced_cat if forced_cat else _derive_test_category("unit", rel, dir_mappings=dir_mappings)
            instance = _derive_instance(rel, dir_mappings)
            s.run("""
                MERGE (p:SysTestPackage {id:$id})
                SET p.path=$path, p.area=$area, p.testType=$cat,
                    p.testCategory=$cat, p.canonicalRoot='go', p.repo=$repo
            """, id=pkg_id, path=rel, cat=category, repo=_repo_id(),
                 area=rel.split("/")[2] if rel.count("/") >= 3 else "shared")
            if instance:
                s.run("MATCH (p:SysTestPackage {id:$id}) SET p.instance=$inst",
                      id=pkg_id, inst=instance)

            # Extract test functions
            tests = []
            try:
                source = f.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(source.splitlines(), 1):
                    for pat in (_GO_TEST_FUNC, _GO_BENCH_FUNC):
                        m = pat.match(line)
                        if m:
                            fn   = m.group(1)
                            tid  = f"{rel}::{fn}"
                            tests.append({"id": tid, "file": rel, "function": fn,
                                          "className": "", "type": category,
                                          "line": lineno, "packageId": pkg_id})
            except Exception:
                source = ""

            if tests:
                refs = _extract_test_entity_refs(source, valid_entity_ids)
                if refs:
                    for t in tests:
                        t["verifies"] = refs
                    total_links += len(refs)
                _upsert_tests(s, tests)
                total += len(tests)
                files += 1

    drv.close()
    print(f"  Go tests: {files} files, {total} test functions registered", end="")
    if total_links:
        print(f", {total_links} VERIFIES links")
    else:
        print()


def cmd_reclassify_tests(args):
    """Bulk-update testType on existing SysTest and SysTestPackage nodes.

    Useful after a scan convention change (e.g. ENH-2242) left existing nodes
    with the wrong type.  Applies a Cypher-only in-graph update — no file I/O.

    Example — fix all Go tests typed as 'unit':
        reclassify-tests --from unit --to component --file-suffix _test.go

    Scope filters (all optional, combinable):
        --path PREFIX     only nodes whose file/path starts with PREFIX
        --file-suffix S   only nodes whose file ends with S
        --instance INST   only nodes whose package instance matches INST
    """
    from_type  = args.from_type
    to_type    = args.to_type
    path_pfx   = getattr(args, "path",        "") or ""
    file_sfx   = getattr(args, "file_suffix", "") or ""
    inst_filter = getattr(args, "instance",   "") or ""
    dry_run    = getattr(args, "dry_run",     False)

    valid_types = {"component", "integration", "usecase", "e2e", "unit", "go-unit"}
    if from_type not in valid_types:
        print(f"ERROR: --from must be one of {sorted(valid_types)}", file=sys.stderr)
        sys.exit(1)
    if to_type not in valid_types:
        print(f"ERROR: --to must be one of {sorted(valid_types)}", file=sys.stderr)
        sys.exit(1)
    if from_type == to_type:
        print("Nothing to do — --from and --to are the same.", file=sys.stderr)
        sys.exit(0)

    drv = _driver()
    with drv.session() as s:
        # Build WHERE clauses
        test_filters = ["t.testType = $from_type"]
        pkg_filters  = ["p.testType = $from_type"]
        params: dict = {"from_type": from_type, "to_type": to_type}

        if path_pfx:
            test_filters.append("(t.file CONTAINS $pfx OR t.id CONTAINS $pfx)")
            pkg_filters.append("(p.path CONTAINS $pfx OR p.id CONTAINS $pfx)")
            params["pfx"] = path_pfx
        if file_sfx:
            test_filters.append("t.file ENDS WITH $sfx")
            pkg_filters.append("p.path ENDS WITH $sfx")
            params["sfx"] = file_sfx
        if inst_filter:
            # Join package to get instance for SysTest
            test_filters.append(
                "EXISTS { MATCH (:SysTestPackage {id: t.packageId, instance: $inst}) }"
            )
            pkg_filters.append("p.instance = $inst")
            params["inst"] = inst_filter

        test_where = " AND ".join(test_filters)
        pkg_where  = " AND ".join(pkg_filters)

        # Count first
        test_count = s.run(
            f"MATCH (t:SysTest) WHERE {test_where} RETURN count(t) AS n",
            **params,
        ).single()["n"]
        pkg_count = s.run(
            f"MATCH (p:SysTestPackage) WHERE {pkg_where} RETURN count(p) AS n",
            **params,
        ).single()["n"]

        if dry_run:
            print(f"  DRY RUN — would update:")
            print(f"    SysTest nodes    : {test_count}  ({from_type} → {to_type})")
            print(f"    SysTestPackage   : {pkg_count}  ({from_type} → {to_type})")
            drv.close()
            return

        if test_count == 0 and pkg_count == 0:
            print(f"  No nodes found matching the given filters — nothing updated.")
            if path_pfx and not dry_run:
                print(f"  Hint: path filter uses CONTAINS matching — check the path is a substring of the stored file path.")
                print(f"  Run with --dry-run (no --path) to see the full scope before committing.")
            drv.close()
            return

        # Safety gate: broad reclassify (no scope filter) on a large set requires --confirm
        is_broad = not path_pfx and not file_sfx and not inst_filter
        confirm = getattr(args, "confirm", False)
        if is_broad and not dry_run and not confirm and (test_count + pkg_count) > 10:
            print(f"  ⚠  BROAD RECLASSIFY — no --path, --file-suffix, or --instance scope set.")
            print(f"     Would update {test_count} SysTest + {pkg_count} SysTestPackage nodes.")
            print(f"  Run with --dry-run first to review, then add --confirm to apply:")
            print(f"     reclassify-tests --from {from_type} --to {to_type} --dry-run")
            print(f"     reclassify-tests --from {from_type} --to {to_type} --confirm")
            drv.close()
            return

        # Apply updates
        s.run(
            f"MATCH (t:SysTest) WHERE {test_where} SET t.testType = $to_type, t.type = $to_type",
            **params,
        )
        s.run(
            f"MATCH (p:SysTestPackage) WHERE {pkg_where} SET p.testType = $to_type, p.testCategory = $to_type",
            **params,
        )

    drv.close()
    print(f"  reclassify-tests: {from_type} → {to_type}")
    print(f"    SysTest updated    : {test_count}")
    print(f"    SysTestPackage updated : {pkg_count}")
    scope_parts = []
    if path_pfx:   scope_parts.append(f"path contains '{path_pfx}'")
    if file_sfx:   scope_parts.append(f"file suffix '{file_sfx}'")
    if inst_filter: scope_parts.append(f"instance '{inst_filter}'")
    if scope_parts:
        print(f"    Scope: {', '.join(scope_parts)}")
    print(f"  Re-run test-gaps to see updated coverage.")


def _load_dir_categories(session) -> list[dict]:
    """Load SysDirCategory nodes from the graph, sorted longest-prefix-first.

    Returns a list of {prefix, pattern, category, instance} dicts.  The scanner
    calls this once per scan run and passes the result to _derive_test_category
    and _derive_instance so the graph is only queried once.
    """
    rows = session.run("""
        MATCH (dc:SysDirCategory)
        RETURN dc.prefix AS prefix, dc.pattern AS pattern,
               dc.category AS category, dc.instance AS instance,
               dc.moduleId AS moduleId
        ORDER BY size(coalesce(dc.prefix,'')) DESC, size(coalesce(dc.pattern,'')) DESC
    """).data()
    return [{"prefix":   r["prefix"]   or "",
             "pattern":  r["pattern"]  or "",
             "category": r["category"] or "integration",
             "instance": r["instance"] or "",
             "moduleId": r["moduleId"] or ""}
            for r in rows]


def _derive_instance(file_path: str, dir_mappings: list | None = None) -> str:
    """Derive instance from file path using SysDirCategory mappings (longest prefix first).

    Returns the instance string (e.g. 'manage', 'plan', 'core') or '' if no
    mapping matches.  Never overwrites an existing instance if '' is returned —
    callers should skip the SET when the return value is empty.
    """
    if not dir_mappings:
        return ""
    fp = file_path or ""
    fn = fp.split("/")[-1]
    for m in dir_mappings:
        if not m.get("instance"):
            continue
        if fp.startswith(m["prefix"]):
            pat = m["pattern"]
            if not pat or pat in fn:
                return m["instance"]
    return ""


def _derive_module_from_dir(file_path: str, dir_mappings: list | None = None) -> str:
    """Derive moduleId from file path using SysDirCategory mappings (longest prefix first).

    Returns the moduleId string or '' if no mapping matches.  Callers should
    fall back to _find_module / SysModule.paths when this returns empty.
    """
    if not dir_mappings:
        return ""
    fp = file_path or ""
    fn = fp.split("/")[-1]
    for m in dir_mappings:
        if not m.get("moduleId"):
            continue
        if fp.startswith(m["prefix"]):
            pat = m["pattern"]
            if not pat or pat in fn:
                return m["moduleId"]
    return ""


def _read_pytestmark(file_path: str) -> str:
    """Read module-level pytestmark from a Python test file via AST.

    Returns the mark tier ('usecase', 'integration', 'component', 'e2e') or ''.
    Looks for: pytestmark = pytest.mark.<tier>
    """
    _MARK_TO_TIER = {
        "usecase": "usecase", "ui": "usecase", "ui_flow": "usecase",
        "integration": "integration", "api": "integration", "functional": "integration",
        "component": "component", "unit": "component",
        "e2e": "e2e", "end_to_end": "e2e",
    }
    if not file_path or not file_path.endswith(".py"):
        return ""
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except Exception:
        return ""
    for node in tree.body:
        # pytestmark = pytest.mark.X  or  pytestmark = [pytest.mark.X, ...]
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "pytestmark":
                val = node.value
                # Single mark: pytest.mark.X
                if isinstance(val, ast.Attribute) and isinstance(val.value, ast.Attribute):
                    mark_name = val.attr
                    if mark_name in _MARK_TO_TIER:
                        return _MARK_TO_TIER[mark_name]
                # List of marks: [pytest.mark.X, ...]
                if isinstance(val, ast.List):
                    for elt in val.elts:
                        if isinstance(elt, ast.Attribute) and elt.attr in _MARK_TO_TIER:
                            return _MARK_TO_TIER[elt.attr]
    return ""


def _derive_test_category(test_type: str, file_path: str = "",
                           explicit: str = "",
                           dir_mappings: list | None = None) -> str:
    """Derive testCategory in priority order:

    1. Explicit override (--category flag)
    2. Unambiguous filename patterns (test_e2e_*.py → e2e)
    3. Module-level pytestmark (pytestmark = pytest.mark.usecase → usecase)
    4. Graph-stored SysDirCategory mappings (longest prefix + pattern match)
    5. testType field heuristics
    6. Filename heuristics
    7. Safe default: 'integration'
    """
    if explicit:
        return explicit

    fp = file_path or ""
    fn = fp.split("/")[-1]   # filename only for pattern matching
    fnl = fn.lower()

    # Unambiguous filename patterns take priority over dir-mappings (FB-222).
    # test_e2e_*.py is always e2e regardless of dir category.
    if fnl.startswith("test_e2e_"):
        return "e2e"

    # Module-level pytestmark takes priority over dir-mappings (ENH-897 / FB-271).
    # Reads AST — only attempted for .py files; silent on parse error.
    mark_tier = _read_pytestmark(fp)
    if mark_tier:
        # Warn if dir-mapping would have given a different answer
        if dir_mappings:
            for m in dir_mappings:
                if fp.startswith(m["prefix"]):
                    pat = m["pattern"]
                    if not pat or pat in fn:
                        dir_cat = m["category"]
                        if dir_cat != mark_tier:
                            print(f"  ⚠  pytestmark.{mark_tier} overrides dir-mapping "
                                  f"'{dir_cat}' for {fn}", file=sys.stderr)
                        break
        return mark_tier

    # Graph directory mappings (loaded once per scan run)
    if dir_mappings:
        for m in dir_mappings:   # already sorted longest-prefix-first
            if fp.startswith(m["prefix"]):
                pat = m["pattern"]
                if not pat or pat in fn:
                    return m["category"]

    # testType field heuristics
    # V-model tier derivation (ADR-027)
    # e2e     → User Story level: cross-instance full journeys
    # usecase → Use Case level:   single-UC Playwright flows
    # integration → Feature level: API/service tests
    # component   → Symbol level:  unit/handler tests
    tt = (test_type or "").lower()
    if tt in ("unit", "frontend"):
        return "component"
    if tt == "e2e":
        return "e2e"
    if tt in ("scenarios", "ui", "ui_full", "ui_walkthrough",
              "ui_scenarios", "ui_business"):
        return "usecase"
    if tt in ("api", "api_full", "functional", "integration", "extended"):
        return "integration"

    # Filename heuristics
    if "_e2e" in fnl:
        return "e2e"
    if any(x in fnl for x in ("_ui_", "walkthrough", "scenarios",
                                "_frontend", "playwright")):
        return "usecase"
    if any(x in fnl for x in ("_functional", "_api", "_integration")):
        return "integration"
    if fnl.endswith("_test.go") or fnl.endswith("_test.ts"):
        return "component"

    return "integration"   # safe default


def cmd_scan_package(args):
    """Scan one test file and register all test functions as SysTest nodes."""
    pkg_id = _repo_rel_path(args.package)
    drv    = _driver()

    explicit_cat = getattr(args, "category", "") or ""

    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)
        valid_entity_ids = _collect_graph_entity_ids(s)

        row = s.run(
            "MATCH (p:SysTestPackage {id:$id}) RETURN p.path AS path, p.testType AS tt, "
            "p.testCategory AS registered_cat",
            id=pkg_id).single()
        if not row:
            # Package not yet registered — auto-register it from the path
            file_path = Path(pkg_id)
            if not file_path.exists():
                print(f"ERROR: package '{pkg_id}' not registered and path not found.",
                      file=sys.stderr)
                drv.close(); sys.exit(1)
            # FB-110: default to '' not 'integration' so filename/dir heuristics run first
            test_type = "unit" if pkg_id.endswith("_test.go") else ""
            category  = _derive_test_category(test_type, pkg_id,
                                              explicit=explicit_cat,
                                              dir_mappings=dir_mappings)
            area = pkg_id.split("/")[-1].replace(".py","").replace("_test.go","")
            s.run("""
                MERGE (p:SysTestPackage {id:$id})
                SET p.path=$path, p.area=$area, p.testType=$tt,
                    p.testCategory=$cat, p.canonicalRoot='auto', p.repo=$repo
            """, id=pkg_id, path=pkg_id, area=area, tt=test_type, cat=category,
                 repo=_repo_id())
        else:
            file_path = Path(row["path"])
            test_type = row["tt"] or "integration"
            # Prefer: (1) explicit --category flag, (2) registered testCategory, (3) derived
            registered = row.get("registered_cat") or ""
            category  = explicit_cat or registered or _derive_test_category(
                test_type, row["path"] or "", dir_mappings=dir_mappings)
            s.run("MATCH (p:SysTestPackage {id:$id}) SET p.testCategory=$cat",
                  id=pkg_id, cat=category)

        if not file_path.exists():
            print(f"ERROR: file not found: {file_path}", file=sys.stderr)
            drv.close(); sys.exit(1)

        # Route to the right extractor based on file extension
        suffix = file_path.suffix.lower()
        if suffix in (".ts", ".tsx"):
            tests = _extract_tests_from_ts_file(file_path, pkg_id, test_type)
        else:
            tests = _extract_tests_from_file(file_path, pkg_id, test_type)

        if tests:
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                source = ""
            refs = _extract_test_entity_refs(source, valid_entity_ids)
            if refs:
                for t in tests:
                    t["verifies"] = refs

        created = _upsert_tests(s, tests)

    drv.close()
    link_note = f" (+{len(refs)} links)" if tests and refs else ""
    print(f"  {pkg_id}: {created} test functions registered{link_note}  [{category}]")


def _register_discovered_test_package(session, file_path: Path,
                                      dir_mappings: list | None = None) -> dict:
    """Upsert a SysTestPackage node for a discovered test file."""
    path_str = _repo_rel_path(file_path)
    pkg_id = path_str

    # area = next directory segment after a logical test dir, else file stem
    area = file_path.stem.replace(".test", "").replace(".spec", "")
    parts = list(file_path.parts)
    for td in _TEST_DIR_NAMES:
        if td in parts:
            idx = parts.index(td)
            if idx + 1 < len(parts):
                area = parts[idx + 1].replace(".py", "").replace(".test", "").replace(".spec", "")
                break

    suffix = file_path.suffix.lower()
    if suffix in (".ts", ".tsx"):
        test_type = "component"
        canonical_root = "vitest"
    else:
        test_type = ""
        canonical_root = "tests"

    category = _derive_test_category(test_type, path_str, dir_mappings=dir_mappings)
    instance = _derive_instance(path_str, dir_mappings) or ""

    session.run("""
        MERGE (p:SysTestPackage {id:$id})
        SET p.path=$path, p.area=$area, p.testType=$tt,
            p.testCategory=$cat, p.canonicalRoot=$root, p.instance=$inst, p.repo=$repo
    """, id=pkg_id, path=path_str, area=area, tt=test_type,
         cat=category, root=canonical_root, inst=instance, repo=_repo_id())
    return {"id": pkg_id, "path": path_str}


def cmd_scan_all(args):
    """Scan all registered test packages, auto-discovering test files first."""
    root_filter  = args.root  or ""   # "p6", "backend", "go", "vitest"
    area_filter  = args.area  or ""   # "p1", "p8", etc.
    inst_filter  = args.instance or ""

    # ── Auto-discover test files and register packages ────────────────────────
    discovered_roots = _discover_test_roots()
    has_filter = bool(root_filter or area_filter or inst_filter)
    if not discovered_roots and not has_filter:
        if not getattr(args, "yes", False):
            prompted = _prompt_directories(
                "No test directories discovered. Enter the test directories to scan:")
            if not prompted:
                print("ERROR: No test directories provided.", file=sys.stderr)
                sys.exit(1)
            discovered_roots = prompted
        else:
            print("ERROR: No test directories found.", file=sys.stderr)
            print("  Register testPaths on SysModule nodes, or run with --path <file>.",
                  file=sys.stderr)
            sys.exit(1)
    elif len(discovered_roots) > 1 and not getattr(args, "yes", False) and not has_filter:
        chosen = _prompt_choice(
            discovered_roots,
            "Discovered multiple test directories. Choose which to scan (comma-separated, or 'all'):",
            allow_multiple=True,
        )
        if chosen is None:
            print("ERROR: No test directories selected.", file=sys.stderr)
            sys.exit(1)
        discovered_roots = [discovered_roots[i] for i in chosen]

    discovered_files = _discover_test_files()
    if discovered_files:
        drv_reg = _driver()
        with drv_reg.session() as s:
            dir_mappings = _load_dir_categories(s)
            for f in discovered_files:
                _register_discovered_test_package(s, Path(f), dir_mappings)
        drv_reg.close()
        print(f"  Discovered {len(discovered_files)} test files in {len(discovered_roots)} directories")

    drv = _driver()
    with drv.session() as s:
        pkgs = s.run("""
            MATCH (p:SysTestPackage)
            WHERE ($root = '' OR p.canonicalRoot = $root)
              AND ($area = '' OR p.area = $area)
              AND ($inst = '' OR p.instance = $inst)
            RETURN p.id AS id, p.path AS path, p.testType AS tt,
                   p.canonicalRoot AS root, p.area AS area,
                   p.testCategory AS existing_cat
            ORDER BY p.area, p.id
        """, root=root_filter, area=area_filter, inst=inst_filter).data()
        # Load dir mappings and existing UC/US/Feature IDs once for the whole scan
        dir_mappings = _load_dir_categories(s)
        valid_entity_ids = _collect_graph_entity_ids(s)

    total_tests  = 0
    total_files  = 0
    total_links  = 0
    errors       = []
    last_area    = None
    explicit_cat = getattr(args, "category", "") or ""

    drv2 = _driver()
    file_tests = []  # [(file_path, tests)] for optional AI analysis
    with drv2.session() as s:
        for pkg in pkgs:
            path_str = pkg.get("path") or ""
            if not path_str:
                errors.append(pkg["id"])
                continue
            file_path = Path(path_str)
            if not file_path.exists() or file_path.is_dir():
                errors.append(pkg["id"])
                continue

            # Skip Go packages — handled by scan-go-tests
            if pkg["root"] == "go":
                continue

            area = pkg["area"] or "?"
            if area != last_area:
                print(f"\n  {area}")
                last_area = area

            # Preserve manually-set testCategory; only derive when not already set
            existing_cat = pkg.get("existing_cat") or ""
            category = explicit_cat or existing_cat or _derive_test_category(
                pkg["tt"] or "", pkg["path"] or "", dir_mappings=dir_mappings)
            s.run("MATCH (p:SysTestPackage {id:$id}) SET p.testCategory=$cat",
                  id=pkg["id"], cat=category)

            # Route to TypeScript extractor for .ts/.tsx vitest files
            if file_path.suffix.lower() in (".ts", ".tsx"):
                tests = _extract_tests_from_ts_file(file_path, pkg["id"], pkg["tt"] or "component")
            else:
                tests = _extract_tests_from_file(file_path, pkg["id"], pkg["tt"] or "integration")

            # If the test file mentions existing UC/US/Feature IDs, link every
            # test in the file to those entities. This makes coverage reports
            # useful even before tests are explicitly annotated.
            had_explicit_links = False
            if tests:
                try:
                    source = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    source = ""
                refs = _extract_test_entity_refs(source, valid_entity_ids)
                if refs:
                    for t in tests:
                        t["verifies"] = refs
                    total_links += len(refs)
                    had_explicit_links = True

            created = _upsert_tests(s, tests)
            total_tests += created
            total_files += 1
            name = file_path.stem
            link_note = f" (+{len(refs)} links)" if tests and refs else ""
            print(f"    {name:<55} {created:4d} tests{link_note}")

            # Retain for semantic analysis pass (skip files already explicitly linked)
            if tests and getattr(args, "analyse", False) and not had_explicit_links:
                file_tests.append((file_path, tests))

    drv2.close()

    # ── Assistant-driven semantic mapping request ─────────────────────────────
    if getattr(args, "analyse", False) and file_tests:
        _emit_ai_analysis_request(args, file_tests, inst_filter)
        # Stop here — the assistant must run the analysis and call --apply-ai-results
        return

    # ── Apply assistant-produced AI mapping results ───────────────────────────
    ai_mappings_total = 0
    apply_path = getattr(args, "apply_ai_results", "") or ""
    if apply_path:
        ai_mappings_total = _apply_ai_results(apply_path)

    print(f"\n{'─'*62}")
    print(f"  Scanned {total_files} files  →  {total_tests} test functions registered")
    if total_links:
        print(f"  {total_links} VERIFIES links created from UC/US/Feature IDs found in test source")
    if ai_mappings_total:
        print(f"  {ai_mappings_total} VERIFIES links created by AI semantic analysis")
    if errors:
        print(f"  {len(errors)} files not found: {errors[:5]}")


# ── Code scanner ─────────────────────────────────────────────────────────────

import re as _re

# ── Go patterns ──────────────────────────────────────────────────────────────
_GO_EXPORTED_FUNC   = _re.compile(r'^func ([A-Z][A-Za-z0-9_]*)\s*[\(\[]')
_GO_EXPORTED_METHOD = _re.compile(r'^func \(([^)]+)\)\s+([A-Z][A-Za-z0-9_]*)\s*[\(\[]')
_GO_HANDLER_VAR     = _re.compile(r'^var\s+(handle[A-Za-z0-9_]+)\s*=')
_GO_TYPE_STRUCT     = _re.compile(r'^type ([A-Z][A-Za-z0-9_]+)\s+struct')
_GO_ENTRY_FUNC      = _re.compile(r'^func (main|init)\s*\(')  # cmd/ entry points

# ── TypeScript/TSX patterns ───────────────────────────────────────────────────
_TS_EXPORT_FUNC     = _re.compile(r'^export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z][A-Za-z0-9_]*)')
_TS_EXPORT_CONST    = _re.compile(r'^export\s+const\s+([A-Za-z][A-Za-z0-9_]*)\s*[:=<(]')
_TS_EXPORT_CLASS    = _re.compile(r'^export\s+(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z][A-Za-z0-9_]*)')
_TS_EXPORT_IFACE    = _re.compile(r'^export\s+(?:type|interface)\s+([A-Za-z][A-Za-z0-9_]*)')
_TS_EXPORT_ENUM     = _re.compile(r'^export\s+(?:const\s+)?enum\s+([A-Za-z][A-Za-z0-9_]*)')


def _go_sym_type(name: str, receiver: str) -> str:
    if name.lower().startswith("handle"):  return "handler"
    if receiver:                           return "method"
    return "function"


def _ts_sym_type(name: str, from_const: bool) -> str:
    if name[0].isupper():  return "component"
    if name.startswith("use"):  return "hook"
    if from_const:         return "const"
    return "function"


def _extract_symbols_go(file_path: Path, module_id: str) -> list[dict]:
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            line = raw.strip()
            m = _GO_EXPORTED_METHOD.match(raw)
            if m:
                receiver, name = m.group(1).strip(), m.group(2)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": _go_sym_type(name, receiver),
                                 "language": "go", "exported": True,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
                continue
            m = _GO_EXPORTED_FUNC.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": _go_sym_type(name, ""),
                                 "language": "go", "exported": True,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
                continue
            m = _GO_HANDLER_VAR.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "handler",
                                 "language": "go", "exported": False,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
                continue
            m = _GO_ENTRY_FUNC.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "entrypoint",
                                 "language": "go", "exported": False,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


def _extract_symbols_ts(file_path: Path, module_id: str) -> list[dict]:
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for pattern, from_const in [(_TS_EXPORT_FUNC, False), (_TS_EXPORT_CONST, True),
                                         (_TS_EXPORT_CLASS, False), (_TS_EXPORT_IFACE, False),
                                         (_TS_EXPORT_ENUM, False)]:
                m = pattern.match(raw.strip())
                if m:
                    name = m.group(1)
                    if len(name) < 2:
                        break
                    stype = _ts_sym_type(name, from_const)
                    results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                     "line": lineno, "symbolType": stype,
                                     "language": "ts", "exported": True,
                                     "signature": raw.strip()[:120], "moduleId": module_id})
                    break
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


def _extract_symbols_py(file_path: Path, module_id: str) -> list[dict]:
    """Extract top-level classes and functions from a Python file using ast."""
    rel = str(file_path)
    results = []
    try:
        import ast as _ast
        tree = _ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.col_offset == 0:  # top-level only
                    stype = "function"
                    results.append({"id": f"{rel}::{node.name}", "file": rel,
                                     "symbol": node.name, "line": node.lineno,
                                     "symbolType": stype, "language": "python",
                                     "exported": not node.name.startswith("_"),
                                     "signature": f"def {node.name}(...)", "moduleId": module_id})
            elif isinstance(node, _ast.ClassDef):
                if node.col_offset == 0:
                    results.append({"id": f"{rel}::{node.name}", "file": rel,
                                     "symbol": node.name, "line": node.lineno,
                                     "symbolType": "class", "language": "python",
                                     "exported": not node.name.startswith("_"),
                                     "signature": f"class {node.name}", "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


_JAVA_PUBLIC = __import__("re").compile(
    r"^\s*public\s+(?:static\s+)?(?:final\s+)?(?:abstract\s+)?"
    r"(?:<[^>]+>\s+)?"
    r"(\w[\w<>\[\]]*)\s+(\w+)\s*[\({]"
)
_JAVA_CLASS  = __import__("re").compile(
    r"^\s*public\s+(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum|record)\s+(\w+)"
)

def _extract_symbols_java(file_path: Path, module_id: str) -> list[dict]:
    """Extract public classes and methods from a Java file using regex."""
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = _JAVA_CLASS.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "class", "language": "java",
                                 "exported": True, "signature": raw.strip()[:120],
                                 "moduleId": module_id})
                continue
            m = _JAVA_PUBLIC.match(raw)
            if m:
                name = m.group(2)
                if name not in ("if","for","while","switch","catch","return","new","throw"):
                    results.append({"id": f"{rel}::{name}_{lineno}", "file": rel,
                                     "symbol": name, "line": lineno,
                                     "symbolType": "method", "language": "java",
                                     "exported": True, "signature": raw.strip()[:120],
                                     "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


_CS_PUBLIC = __import__("re").compile(
    r"^\s*public\s+(?:static\s+)?(?:virtual\s+)?(?:override\s+)?(?:async\s+)?"
    r"(?:abstract\s+)?(?:readonly\s+)?"
    r"(\w[\w<>\[\]?,\s]*)\s+(\w+)\s*[\({<]"
)
_CS_CLASS = __import__("re").compile(
    r"^\s*public\s+(?:abstract\s+)?(?:sealed\s+)?(?:static\s+)?"
    r"(?:partial\s+)?(?:class|interface|enum|struct|record)\s+(\w+)"
)

def _extract_symbols_cs(file_path: Path, module_id: str) -> list[dict]:
    """Extract public classes and methods from a C# file using regex."""
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = _CS_CLASS.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "class", "language": "csharp",
                                 "exported": True, "signature": raw.strip()[:120],
                                 "moduleId": module_id})
                continue
            m = _CS_PUBLIC.match(raw)
            if m:
                name = m.group(2)
                if name not in ("if","for","while","return","new","using","namespace","void"):
                    results.append({"id": f"{rel}::{name}_{lineno}", "file": rel,
                                     "symbol": name, "line": lineno,
                                     "symbolType": "method", "language": "csharp",
                                     "exported": True, "signature": raw.strip()[:120],
                                     "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


def _load_module_path_map(session) -> list[tuple[str, str]]:
    """Return [(path_prefix, module_id)] sorted longest-first for specificity.

    Uses SysModule.codePaths first, falling back to legacy paths for backwards
    compatibility.
    """
    rows = session.run("""
        MATCH (m:SysModule)
        RETURN m.id AS mid,
               coalesce(m.codePaths, []) AS codePaths,
               coalesce(m.paths, []) AS paths
    """).data()
    pairs = []
    for r in rows:
        for p in (r["codePaths"] or []) + (r["paths"] or []):
            pairs.append((p.rstrip("/"), r["mid"]))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def _discover_code_roots() -> list[tuple[str, str]]:
    """Discover top-level source-code roots from project markers in cwd.

    Returns a sorted list of (path, lang_hint) tuples.  Lang hint is inferred
    from the project marker (go.mod -> go, package.json -> ts, etc.).  If no
    marker is present the hint is 'auto'.

    Only the current working directory is checked for markers; nested monorepo
    package files are ignored so `scan-code --all` scans the whole project.
    """
    roots: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(path: str, lang: str) -> None:
        p = path.rstrip("/")
        if p and p not in seen and Path(p).is_dir():
            seen.add(p)
            roots.append((p, lang))

    MARKERS = [
        ("go.mod", "go"),
        ("package.json", "ts"),
        ("pyproject.toml", "py"),
        ("setup.py", "py"),
        ("pom.xml", "java"),
        ("build.gradle", "java"),
        ("*.csproj", "cs"),
        ("*.sln", "cs"),
    ]
    cwd = Path.cwd()
    for marker, lang in MARKERS:
        if marker.startswith("*"):
            for f in cwd.glob(marker):
                _add(str(f.parent), lang)
        else:
            if (cwd / marker).exists():
                _add(str(cwd), lang)
                break

    return sorted(roots)


# Logical test-directory names discovered under project roots.
_TEST_DIR_NAMES = {"tests", "test", "__tests__", "e2e", "playwright",
                   "cypress", "integration", "unit", "specs", "spec"}


def _is_test_file(path: Path) -> bool:
    """Return True for Python/TypeScript test files (Go tests use scan-go-tests)."""
    if not path.is_file():
        return False
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in (".ts", ".tsx"):
        return ".test." in name or ".spec." in name
    if suffix == ".py":
        return name.startswith("test_") or name.endswith("_test.py") or ".test." in name
    return False


def _discover_test_roots() -> list[str]:
    """Return top-level logical test directories discovered under code roots."""
    candidates: set[str] = set()
    code_roots = _discover_code_roots()
    for root, _ in code_roots:
        root_path = Path(root)
        # Directories whose name is a known test-directory name
        for subdir in root_path.rglob("*"):
            if not subdir.is_dir():
                continue
            if any(part in _SKIP_DIRS for part in subdir.relative_to(root_path).parts):
                continue
            if subdir.name in _TEST_DIR_NAMES:
                candidates.add(str(subdir))
        # Directories that contain test files by filename pattern
        for pattern in ("*.test.ts", "*.spec.ts", "*.test.tsx", "*.spec.tsx",
                        "test_*.py", "*_test.py"):
            for f in root_path.rglob(pattern):
                if any(part in _SKIP_DIRS for part in f.relative_to(root_path).parts):
                    continue
                candidates.add(str(f.parent))

    # Prune to top-level roots only: drop any candidate whose parent is also a candidate
    sorted_candidates = sorted(candidates, key=len)
    roots: list[str] = []
    for c in sorted_candidates:
        if not any(c.startswith(r + "/") or c == r for r in roots):
            roots.append(c)
    return sorted(roots)


def _discover_test_files() -> list[str]:
    """Discover test files from SysModule.testPaths and logical test directories.

    Returns a sorted list of absolute/normalised file paths.  Does NOT include
    Go *_test.go files — those are handled by scan-go-tests.
    """
    files: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        p = str(Path(path).resolve())
        if p and p not in seen and Path(p).is_file():
            seen.add(p)
            files.append(p)

    # 1. Test paths declared on SysModule nodes
    drv = _driver()
    try:
        with drv.session() as s:
            rows = s.run("""
                MATCH (m:SysModule)
                UNWIND coalesce(m.testPaths, []) AS tp
                RETURN DISTINCT tp AS path
            """).data()
            for r in rows:
                p = r["path"]
                if not p:
                    continue
                pp = Path(p)
                if pp.is_dir():
                    for f in pp.rglob("*"):
                        if _is_test_file(f):
                            _add(str(f))
                elif _is_test_file(pp):
                    _add(p)
    finally:
        drv.close()

    # 2. Logical test directories and filename patterns under discovered code roots
    code_roots = _discover_code_roots()
    for root, _ in code_roots:
        root_path = Path(root)
        # Logical test directories
        for subdir in root_path.rglob("*"):
            if not subdir.is_dir():
                continue
            if any(part in _SKIP_DIRS for part in subdir.relative_to(root_path).parts):
                continue
            if subdir.name in _TEST_DIR_NAMES:
                for f in subdir.rglob("*"):
                    if _is_test_file(f):
                        _add(str(f))
        # Filename patterns anywhere under the root
        for pattern in ("*.test.ts", "*.spec.ts", "*.test.tsx", "*.spec.tsx",
                        "test_*.py", "*_test.py"):
            for f in root_path.rglob(pattern):
                if any(part in _SKIP_DIRS for part in f.relative_to(root_path).parts):
                    continue
                _add(str(f))

    return sorted(files)


def _is_interactive() -> bool:
    """Return True when stdin is a TTY, so interactive prompts are safe."""
    return sys.stdin.isatty()


def _prompt_choice(options: list[str], prompt: str, allow_multiple: bool = False):
    """Print numbered options and return selected index(es), or None on cancel/error."""
    if not _is_interactive() or not options:
        return None
    print(prompt, file=sys.stderr)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}", file=sys.stderr)
    try:
        raw = input("Enter number(s), 'all', or 'q' to cancel: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw.lower() == "q":
        return None
    if allow_multiple and raw.lower() == "all":
        return list(range(len(options)))
    try:
        idxs = [int(x) - 1 for x in raw.split(",") if x.strip()]
        if not idxs or not all(0 <= i < len(options) for i in idxs):
            print("  Invalid selection.", file=sys.stderr)
            return None
        return idxs if allow_multiple else idxs[0]
    except ValueError:
        print("  Invalid input.", file=sys.stderr)
        return None


def _prompt_directories(prompt: str) -> list[str]:
    """Prompt for one or more directories and return resolved, validated paths."""
    if not _is_interactive():
        return []
    print(prompt, file=sys.stderr)
    try:
        raw = input("Enter directories (comma-separated), or 'q' to cancel: ").strip()
    except (EOFError, KeyboardInterrupt):
        return []
    if raw.lower() == "q":
        return []
    dirs: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        pp = Path(p).expanduser()
        if not pp.exists():
            print(f"  Path does not exist: {p}", file=sys.stderr)
        elif not pp.is_dir():
            print(f"  Not a directory: {p}", file=sys.stderr)
        else:
            dirs.append(str(pp.resolve()))
    return dirs


def _persist_module_paths(session, module_id: str, field: str, paths: list[str]) -> None:
    """Persist chosen code or test paths back to a SysModule node."""
    if not paths:
        return
    try:
        session.run(
            f"MATCH (m:SysModule {{id:$id}}) SET m.{field} = coalesce(m.{field}, []) + $paths",
            id=module_id, paths=paths
        )
    except Exception as e:
        print(f"  ⚠ could not persist {field} on {module_id}: {e}", file=sys.stderr)


def _find_module(file_str: str, path_map: list[tuple[str, str]]) -> str:
    for prefix, mid in path_map:
        if file_str.startswith(prefix):
            return mid
    return ""


def _upsert_symbols(session, symbols: list[dict]) -> int:
    if not symbols:
        return 0
    session.run("""
        UNWIND $syms AS s
        MERGE (n:SysSymbol {id: s.id})
        SET n.file       = s.file,
            n.symbol     = s.symbol,
            n.line       = s.line,
            n.symbolType = s.symbolType,
            n.language   = s.language,
            n.exported   = s.exported,
            n.signature  = s.signature,
            n.moduleId   = s.moduleId
        WITH n, s
        MATCH (m:SysModule {id: s.moduleId})
        MERGE (m)-[:CONTAINS_SYMBOL]->(n)
    """, syms=[s for s in symbols if s["moduleId"]])
    # Symbols with no module match — still create but no edge
    unowned = [s for s in symbols if not s["moduleId"]]
    if unowned:
        session.run("""
            UNWIND $syms AS s
            MERGE (n:SysSymbol {id: s.id})
            SET n.file = s.file, n.symbol = s.symbol, n.line = s.line,
                n.symbolType = s.symbolType, n.language = s.language,
                n.exported = s.exported, n.signature = s.signature
        """, syms=unowned)
    return len(symbols)


# Skip patterns — files that should not be scanned for symbols
_SKIP_DIRS  = {"node_modules", ".git", "dist", "build", "vendor",
               "__pycache__", ".next", "coverage", "frontend-OLD", "backend-OLD",
               "_ul", "ontologies", "translations"}
_GO_TEST_FUNC  = _re.compile(r'^func (Test[A-Za-z0-9_]+)\s*\(t \*testing\.')
_GO_BENCH_FUNC = _re.compile(r'^func (Benchmark[A-Za-z0-9_]+)\s*\(')


def _should_skip(path: Path) -> bool:
    for part in path.parts:
        if part in _SKIP_DIRS:
            return True
    name = path.name
    if name.endswith("_test.go"):      return True
    if name.startswith("test_"):       return True
    if name.endswith(".min.js"):       return True
    if "generated" in name.lower():    return True
    return False


def orig_cmd_scan_code(args):
    """Scan one directory (or file) for exported symbols and upsert as SysSymbol nodes."""
    target = Path(args.path)
    if not target.exists():
        print(f"ERROR: {target} does not exist", file=sys.stderr); sys.exit(1)

    drv = _driver()
    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)
        path_map     = _load_module_path_map(s)
        files    = [target] if target.is_file() else list(target.rglob("*"))
        total    = 0
        lang_filter = getattr(args, "lang", "auto")
        for f in sorted(files):
            if _should_skip(f): continue
            mod = _derive_module_from_dir(str(f), dir_mappings) or _find_module(str(f), path_map)
            if f.suffix == ".go" and lang_filter in ("auto","go"):
                syms = _extract_symbols_go(f, mod)
            elif f.suffix in (".ts", ".tsx") and lang_filter in ("auto","ts"):
                syms = _extract_symbols_ts(f, mod)
            elif f.suffix == ".py" and lang_filter in ("auto","py","python"):
                if not f.name.startswith("test_") and not f.name.endswith("_test.py"):
                    syms = _extract_symbols_py(f, mod)
                else:
                    continue
            elif f.suffix == ".java" and lang_filter in ("auto","java"):
                syms = _extract_symbols_java(f, mod)
            elif f.suffix == ".cs" and lang_filter in ("auto","cs","csharp"):
                syms = _extract_symbols_cs(f, mod)
            else:
                continue
            total += _upsert_symbols(s, syms)
    drv.close()
    print(f"  {target}: {total} symbols upserted")


def cmd_scan_code_all(args):
    """Scan all known code roots: explicit --path, module codePaths, discovered project roots, or prompted dirs."""
    if args.path:
        # Override with explicit path
        roots = [(args.path, "auto")]
    else:
        roots = _discover_code_roots()
        if not roots:
            if getattr(args, "yes", False):
                print("ERROR: No code roots found.", file=sys.stderr)
                print("  Register codePaths on SysModule nodes, or run with --path <dir>.",
                      file=sys.stderr)
                sys.exit(1)
            prompted = _prompt_directories(
                "No code roots discovered. Enter the code directories to scan:")
            if not prompted:
                print("ERROR: No code directories provided.", file=sys.stderr)
                sys.exit(1)
            roots = [(p, "auto") for p in prompted]
        elif len(roots) > 1 and not getattr(args, "yes", False):
            chosen = _prompt_choice(
                [f"{p}  [{l}]" for p, l in roots],
                "Discovered multiple code roots. Choose which to scan (comma-separated, or 'all'):",
                allow_multiple=True,
            )
            if chosen is None:
                print("ERROR: No code roots selected.", file=sys.stderr)
                sys.exit(1)
            roots = [roots[i] for i in chosen]

    drv = _driver()
    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)
        path_map     = _load_module_path_map(s)

        def _mod(file_str: str) -> str:
            return _derive_module_from_dir(file_str, dir_mappings) or _find_module(file_str, path_map)

        grand_total = 0
        for root_str, lang_hint in roots:
            root = Path(root_str)
            if not root.exists():
                print(f"  (skip {root_str} — not found)")
                continue

            area_total = 0
            file_count = 0
            last_parent = None

            for f in sorted(root.rglob("*")):
                if not f.is_file() or _should_skip(f):
                    continue
                # Respect --lang flag if provided on args
                lang_override = getattr(args, "lang", "auto") or "auto"
                effective_lang = lang_override if lang_override != "auto" else lang_hint

                if f.suffix == ".go" and effective_lang in ("go", "auto"):
                    syms = _extract_symbols_go(f, _mod(str(f)))
                elif f.suffix in (".ts", ".tsx") and effective_lang in ("ts", "auto"):
                    syms = _extract_symbols_ts(f, _mod(str(f)))
                else:
                    continue
                if not syms:
                    continue
                parent = f.parent
                if parent != last_parent:
                    rel_parent = str(parent)
                    print(f"\n  {rel_parent}")
                    last_parent = parent
                n = _upsert_symbols(s, syms)
                area_total += n
                file_count += 1
                print(f"    {f.name:<60} {n:4d}")

            print(f"\n  ── {root_str}: {file_count} files, {area_total} symbols")
            grand_total += area_total

    drv.close()
    print(f"\n{'═'*62}")
    print(f"  Total symbols upserted: {grand_total}")


def cmd_test_gaps(args):
    """Show test coverage gaps by tier for an instance or the P6 master test suite.

    Three tiers:
      component   — Go *_test.go unit tests
      integration — Python API/functional tests (test_pX_api.py, test_pX_functional.py)
      usecase     — Playwright/E2E tests (test_pX_ui_*.py, *_e2e.py, *_scenarios.py)

    Usage:
        python3 scripts/sys_edge.py test-gaps --instance core
        python3 scripts/sys_edge.py test-gaps --instance core --tier component
        python3 scripts/sys_edge.py test-gaps --p6          # show P6 packages with no feature links
    """
    instance   = args.instance or ""
    system     = getattr(args, "system", "") or ""
    tier_arg   = args.tier or "all"
    p6_mode    = getattr(args, "p6", False)
    drv        = _driver()
    today      = date.today().isoformat()
    W          = 62

    with drv.session() as s:
        if instance and not p6_mode:
            _validate_instance(instance, s)

        if p6_mode:
            # P6 master view: test packages with few or no feature links
            pkgs = s.run("""
                MATCH (pkg:SysTestPackage)
                OPTIONAL MATCH (pkg)-[:CONTAINS_TEST]->(t:SysTest)-[:VERIFIES]->(f:SysFeature)
                WITH pkg, count(DISTINCT t) AS linkedTests, count(DISTINCT f) AS features,
                     count{(pkg)-[:CONTAINS_TEST]->()} AS totalTests
                RETURN pkg.id AS id, pkg.area AS area,
                       pkg.testCategory AS cat, pkg.testType AS ttype,
                       totalTests, linkedTests, features
                ORDER BY features ASC, totalTests DESC
            """).data()

            print("═" * W)
            print(f"  P6 TEST PACKAGE GAPS — feature-link coverage  ({today})")
            print("═" * W)

            by_cat = {}
            for p in pkgs:
                cat = p.get("cat") or "integration"
                by_cat.setdefault(cat, []).append(p)

            for cat in ("component", "integration", "usecase"):
                items = by_cat.get(cat, [])
                if not items:
                    continue
                unlinked = [p for p in items if p["features"] == 0 and p["totalTests"] > 0]
                partial  = [p for p in items if 0 < p["features"] < 3 and p["totalTests"] > 5]
                print(f"\n{'─' * W}")
                print(f"  {cat.upper()} ({len(items)} packages"
                      f"  — {len(unlinked)} fully unlinked  {len(partial)} partial)")
                print(f"{'─' * W}")
                if unlinked:
                    print(f"  Packages with tests but NO feature links:")
                    for p in sorted(unlinked, key=lambda x: -x["totalTests"])[:20]:
                        name = p["id"].split("/")[-1].replace(".py","")
                        print(f"    {p['area']:<8} {name:<50} {p['totalTests']:4d} tests")
                    if len(unlinked) > 20:
                        print(f"    … and {len(unlinked)-20} more")
                if partial:
                    print(f"  Packages with few feature links (worth extending):")
                    for p in sorted(partial, key=lambda x: x["features"])[:10]:
                        name = p["id"].split("/")[-1].replace(".py","")
                        print(f"    {p['area']:<8} {name:<50} {p['features']:2d} features / {p['totalTests']:4d} tests")

            print(f"\n{'─' * W}")
            print(f"  To link a test package to features:")
            print(f"    python3 scripts/sys_edge.py link-feature \\")
            print(f"      --feature F-P3-005 --tests 'test_p3_api.py::TestExtraction'")
            print(f"{'─' * W}")
            drv.close()
            return

        # ── Instance mode ──────────────────────────────────────────────────────
        if system and instance:
            mod_rows = s.run("""
                MATCH (:SysSystem {id:$sys})-[:CONTAINS]->(:SysDomain)-[:CONTAINS]->(m:SysModule {instance:$inst})
                MATCH (m)-[:PROVIDES]->(f:SysFeature)
                RETURN m.id AS mid, m.name AS mname,
                       coalesce(m.codePaths, m.paths, []) AS codePaths,
                       coalesce(m.testPaths, m.paths, []) AS testPaths,
                       f.id AS fid, f.name AS fname, f.status AS fstatus
                ORDER BY m.id, f.id
            """, inst=instance, sys=system).data()
        elif system:
            # System-only mode: show all modules under this system without filtering by instance
            mod_rows = s.run("""
                MATCH (:SysSystem {id:$sys})-[:CONTAINS]->(:SysDomain)-[:CONTAINS]->(m:SysModule)
                MATCH (m)-[:PROVIDES]->(f:SysFeature)
                RETURN m.id AS mid, m.name AS mname, m.instance AS inst,
                       coalesce(m.codePaths, m.paths, []) AS codePaths,
                       coalesce(m.testPaths, m.paths, []) AS testPaths,
                       f.id AS fid, f.name AS fname, f.status AS fstatus
                ORDER BY m.id, f.id
            """, sys=system).data()
        else:
            mod_rows = s.run("""
                MATCH (m:SysModule {instance:$inst})-[:PROVIDES]->(f:SysFeature)
                RETURN m.id AS mid, m.name AS mname,
                       coalesce(m.codePaths, m.paths, []) AS codePaths,
                       coalesce(m.testPaths, m.paths, []) AS testPaths,
                       f.id AS fid, f.name AS fname, f.status AS fstatus
                ORDER BY m.id, f.id
            """, inst=instance).data()

        if not mod_rows:
            scope = f"system '{system}'" if system else f"instance '{instance}'"
            print(f"No features found for {scope}.")
            drv.close(); return

        # Build feature → {component, integration, usecase} coverage map
        all_fids = list({r["fid"] for r in mod_rows})
        # ENH-899: match briefing's coverage logic — testType OR package.testCategory
        # Both paths must use the same criteria to avoid contradictory results.
        cov_data = s.run("""
            UNWIND $fids AS fid
            MATCH (f:SysFeature {id:fid})
            OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
            WHERE (tc.testType IN ['component','go-unit'])
               OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
            OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
            WHERE (ti.testType = 'integration')
               OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
            OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
            WHERE (tu.testType = 'usecase')
               OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
            OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
            WHERE (te.testType = 'e2e')
               OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
            RETURN fid,
                   count(DISTINCT tc) AS cmp,
                   count(DISTINCT ti) AS int_,
                   count(DISTINCT tu) AS uc,
                   count(DISTINCT te) AS e2e
        """, fids=all_fids).data()
        cov_map = {r["fid"]: r for r in cov_data}

        # Module → existing test packages (for suggestions)
        mod_ids = list({r["mid"] for r in mod_rows})
        mod_pkgs = s.run("""
            MATCH (m:SysModule) WHERE m.id IN $mids
            OPTIONAL MATCH (p:SysTestPackage)
            WHERE any(path IN coalesce(m.testPaths, m.paths, []) WHERE p.path CONTAINS split(path, '/')[0]
                        OR p.area = split(m.id, 'MOD-')[1])
            RETURN m.id AS mid, collect(DISTINCT p.id) AS pkgs
        """, mids=mod_ids).data()
        mod_pkg_map = {r["mid"]: r["pkgs"] for r in mod_pkgs}

        # Group rows by module
        from collections import defaultdict
        by_mod = defaultdict(list)
        for r in mod_rows:
            by_mod[r["mid"]].append(r)

    drv.close()

    print("═" * W)
    scope_label = system if (system and not instance) else (f"{system}/{instance}" if system else instance)
    print(f"  TEST GAPS — {scope_label}  ({today})")
    print("═" * W)

    # V-model tiers (ADR-027) — component→symbol, integration→feature, usecase→UC, e2e→US
    TIERS = [
        ("component",   "cmp",  "Unit tests (Go *_test.go, vitest)",           "scan-go-tests + link-feature"),
        ("integration", "int_", "Integration/API tests (Python pytest)",        "test_pX_api.py or test_pX_functional.py"),
        ("usecase",     "uc",   "UI flow tests — one Playwright test per UC",   "test_pX_*_ui.py — see uc-ui-testing.md"),
        ("e2e",         "e2e",  "E2E tests — full User Story journeys (master)","test_*_e2e.py spanning multiple tools"),
    ]

    if _lightweight() and tier_arg == "all":
        TIERS = [t for t in TIERS if t[0] in ("component", "integration")]
    if tier_arg != "all":
        TIERS = [t for t in TIERS if t[0] == tier_arg]
    elif instance and instance != "master":
        # E2E tier is owned by master — suppress for other instances unless explicitly requested
        TIERS = [t for t in TIERS if t[0] != "e2e"]

    for tier_name, tier_key, tier_desc, tier_hint in TIERS:
        gaps = [(mid, r) for mid, rows in by_mod.items()
                for r in rows
                if cov_map.get(r["fid"], {}).get(tier_key, 0) == 0
                and r.get("fstatus") != "Superseded"]

        total_f = sum(len(rows) for rows in by_mod.values())
        covered = total_f - len(gaps)

        print(f"\n{'─' * W}")
        marker = "✓" if not gaps else "✗"
        print(f"  {marker} {tier_name.upper()} GAPS  ({len(gaps)} of {total_f} features uncovered)")
        print(f"    {tier_desc}")
        print(f"{'─' * W}")

        if not gaps:
            print(f"  ✓ All features have {tier_name} test coverage")
            continue

        # Group by module
        gaps_by_mod = defaultdict(list)
        for mid, r in gaps:
            gaps_by_mod[mid].append(r)

        for mid, rows in sorted(gaps_by_mod.items()):
            mod_r  = next(r for r in mod_rows if r["mid"] == mid)
            paths  = mod_r.get("codePaths") or []
            pkgs   = mod_pkg_map.get(mid, [])
            pkg_names = [p.split("/")[-1].replace(".py","") for p in pkgs if p][:3]

            print(f"\n  {mid}  {mod_r['mname']}  ({len(rows)} gaps)")
            if paths:
                for p in paths[:2]:
                    print(f"    Code: {p}")
            if pkg_names:
                print(f"    Existing test files: {', '.join(pkg_names)}")

            for r in rows:
                existing = cov_map.get(r["fid"], {})
                have = []
                if existing.get("cmp",  0) > 0: have.append("cmp")
                if existing.get("int_", 0) > 0: have.append("int")
                if existing.get("uc",   0) > 0: have.append("uc")
                has_str = f"  [has: {','.join(have)}]" if have else ""
                print(f"    ✗ {r['fid']:<16} {r['fname']}{has_str}")

        print(f"\n  → Hint: {tier_hint}")

    print(f"\n{'═' * W}")
    all_gaps = sum(
        1 for r in mod_rows
        if not all(cov_map.get(r["fid"], {}).get(k, 0) > 0 for _, k, _, _ in TIERS)
        and r.get("fstatus") != "Superseded"
    )
    print(f"  {instance}: {all_gaps} feature(s) missing at least one tier of test coverage")
    print(f"{'═' * W}")

    # ── Endpoints with no test coverage ──────────────────────────────────────
    drv2 = _driver()
    with drv2.session() as s2:
        untest_eps = s2.run("""
            MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            WHERE m.id IN $mids
            AND NOT exists((:SysTest)-[:VERIFIES]->(f))
            RETURN ep.method AS method, ep.path AS path,
                   f.id AS fid, m.id AS mid
            ORDER BY m.id, ep.method, ep.path
        """, mids=mod_ids).data()
    drv2.close()
    if untest_eps:
        print(f"\n{'─' * W}")
        print(f"  ⚠ ENDPOINTS WITH NO TEST COVERAGE ({len(untest_eps)})")
        print(f"    Linked to features that have no verifying tests.")
        print(f"{'─' * W}")
        for ep in untest_eps:
            print(f"    {ep.get('method','?'):6} {ep.get('path','?'):<45} → {ep['fid']}")
        print(f"  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")
        print(f"{'═' * W}")

    # ENH-1423: recently closed ENHs whose linked features still have no tests
    days_back = getattr(args, "days", None) or 30
    from datetime import timedelta as _tgd
    _since_iso = (date.today() - _tgd(days=int(days_back))).isoformat()
    drv3 = _driver()
    with drv3.session() as s3:
        recent_untested = s3.run("""
            MATCH (e:SysEnhancement {instance:$inst})
            WHERE e.status = 'done' AND e.completedAt >= $since
            MATCH (e)-[:EXTENDS]->(f:SysFeature)
            WHERE NOT (:SysTest)-[:VERIFIES]->(f)
              AND coalesce(f.status,'') <> 'Superseded'
            RETURN e.id AS eid, e.title AS title,
                   left(coalesce(e.completedAt,''),10) AS closed,
                   collect(DISTINCT f.id) AS fids
            ORDER BY closed DESC
        """, inst=instance, since=_since_iso).data()
    drv3.close()
    if recent_untested:
        print(f"\n{'─' * W}")
        print(f"  ⚠ RECENTLY CLOSED ENHs WITH UNTESTED FEATURES (last {days_back}d)")
        print(f"    These features were implemented but have no verifying tests.")
        print(f"{'─' * W}")
        for ru in recent_untested:
            fid_str = ", ".join(ru["fids"][:4])
            if len(ru["fids"]) > 4:
                fid_str += f" … +{len(ru['fids'])-4}"
            print(f"    {ru['eid']}  closed {ru['closed']}  {ru['title'][:45]}")
            print(f"         Untested: {fid_str}")
        print(f"  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")
        print(f"{'═' * W}")


def cmd_actor_coverage(args):
    """Show which actors have stories per feature, and which are missing.

    Derives the reference actor set from all actors present in the scope, then
    shows a matrix: features × actors, flagging gaps.

    Usage:
        actor-coverage --instance manage
        actor-coverage --feature F-MGT-001
    """
    instance  = getattr(args, "instance", "") or ""
    feature   = getattr(args, "feature",  "") or ""
    if not instance and not feature:
        print("ERROR: provide --instance or --feature", file=sys.stderr); sys.exit(1)

    drv = _driver()
    W = 70
    with drv.session() as s:
        if feature:
            # Single-feature mode
            feat_row = s.run("MATCH (f:SysFeature {id:$id}) RETURN f.name AS name", id=feature).single()
            if not feat_row:
                print(f"ERROR: feature '{feature}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
            rows = s.run("""
                MATCH (f:SysFeature {id:$fid})
                OPTIONAL MATCH (us:SysUserStory)-[:REQUIRES]->(f)
                RETURN f.id AS fid, f.name AS fname,
                       collect(DISTINCT CASE WHEN us.actor IS NOT NULL AND us.actor <> ''
                                              THEN us.actor END) AS actors,
                       count(DISTINCT us) AS story_count
            """, fid=feature).data()
            # For single-feature, reference set = actors from module-siblings
            ref_rows = s.run("""
                MATCH (:SysModule)-[:PROVIDES]->(f:SysFeature {id:$fid})
                MATCH (:SysModule)-[:PROVIDES]->(f2:SysFeature)
                MATCH (us:SysUserStory)-[:REQUIRES]->(f2)
                WHERE us.actor IS NOT NULL AND us.actor <> ''
                RETURN DISTINCT us.actor AS actor ORDER BY actor
            """, fid=feature).data()
            all_actors = [r["actor"] for r in ref_rows]
        else:
            rows = s.run("""
                MATCH (m:SysModule {instance:$inst})-[:PROVIDES]->(f:SysFeature)
                OPTIONAL MATCH (us:SysUserStory)-[:REQUIRES]->(f)
                WITH f, collect(DISTINCT CASE WHEN us.actor IS NOT NULL AND us.actor <> ''
                                               THEN us.actor END) AS actors,
                     count(DISTINCT us) AS story_count
                RETURN f.id AS fid, f.name AS fname, actors, story_count
                ORDER BY f.id
            """, inst=instance).data()
            ref_rows = s.run("""
                MATCH (m:SysModule {instance:$inst})-[:PROVIDES]->(f:SysFeature)
                MATCH (us:SysUserStory)-[:REQUIRES]->(f)
                WHERE us.actor IS NOT NULL AND us.actor <> ''
                RETURN DISTINCT us.actor AS actor ORDER BY actor
            """, inst=instance).data()
            all_actors = [r["actor"] for r in ref_rows]

    drv.close()

    if not rows:
        print(f"No features found for {'feature ' + feature if feature else 'instance ' + instance}")
        return

    label = feature if feature else instance
    print(f"\n{'═' * W}")
    print(f"  ACTOR COVERAGE — {label}")
    if all_actors:
        print(f"  Reference actors: {', '.join(all_actors)}")
    else:
        print(f"  (No actor data — set --actor on stories to enable matrix)")
    print(f"{'═' * W}")

    no_stories   = [r for r in rows if r["story_count"] == 0]
    no_actor_tag = [r for r in rows if r["story_count"] > 0 and not [a for a in r["actors"] if a]]
    has_gaps     = [r for r in rows if all_actors and r["story_count"] > 0
                    and set(all_actors) - set(r["actors"])]
    full_cover   = [r for r in rows if all_actors and r["story_count"] > 0
                    and not (set(all_actors) - set(r["actors"]))]

    if all_actors:
        # Matrix header
        col_w = max(12, max(len(a) for a in all_actors) + 2)
        header = f"  {'Feature':<18}"
        for a in all_actors:
            header += f"  {a[:col_w-2]:<{col_w-2}}"
        header += "  Missing"
        print(header)
        print(f"  {'─'*18}" + (f"  {'─'*(col_w-2)}" * len(all_actors)) + "  ─────────────────────")
        for r in rows:
            if r.get("fstatus") == "Superseded":
                continue
            present = set(a for a in r["actors"] if a)
            missing = [a for a in all_actors if a not in present]
            row_str = f"  {r['fid']:<18}"
            for a in all_actors:
                mark = "✓" if a in present else "·"
                row_str += f"  {mark:<{col_w-2}}"
            row_str += f"  {', '.join(missing) if missing else '—'}"
            print(row_str)
    else:
        # No actor data — show story counts only
        for r in rows:
            mark = f"{r['story_count']} stories" if r["story_count"] > 0 else "(no stories)"
            print(f"  {r['fid']:<20}  {mark}")

    print(f"\n{'─' * W}")
    if no_stories:
        print(f"  ⚠  {len(no_stories)} feature(s) with no stories: "
              f"{', '.join(r['fid'] for r in no_stories[:6])}"
              + ("…" if len(no_stories) > 6 else ""))
    if no_actor_tag:
        print(f"  ⚠  {len(no_actor_tag)} feature(s) have stories but no actor set — run:")
        print(f"     update-story --id US-xxx --actor \"Role\"")
    if all_actors and has_gaps:
        print(f"  ⚠  {len(has_gaps)} feature(s) missing actor coverage — add stories or check actors")
    if all_actors and full_cover:
        print(f"  ✓  {len(full_cover)} feature(s) have full actor coverage")
    print(f"{'═' * W}")


def cmd_uc_state_check(args):
    """AI-assisted check that UC postconditions align with preconditions of related UCs.

    Finds UC pairs linked via shared user stories, then asks the AI whether any
    state conflict exists (e.g. postcondition sets authenticated=false while the
    next UC requires authenticated=true).

    Usage:
        uc-state-check --instance manage --ai
        uc-state-check --uc UC-DLG-001 --ai   (check against all UCs sharing a story)
    """
    instance  = getattr(args, "instance", "") or ""
    uc_id     = getattr(args, "uc",       "") or ""
    model     = getattr(args, "model",    "haiku") or "haiku"
    provider  = getattr(args, "provider", "") or ""

    if not instance and not uc_id:
        print("ERROR: provide --instance or --uc", file=sys.stderr); sys.exit(1)

    drv = _driver()
    W = 70
    with drv.session() as s:
        if uc_id:
            pairs = s.run("""
                MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc1:SysUseCase {id:$uid})
                MATCH (us)-[:REALIZED_BY]->(uc2:SysUseCase)
                WHERE uc2.id <> $uid
                  AND (uc1.postconditions IS NOT NULL OR uc2.postconditions IS NOT NULL)
                  AND (uc1.preconditions  IS NOT NULL OR uc2.preconditions  IS NOT NULL)
                RETURN DISTINCT
                    uc1.id AS id1, uc1.title AS t1,
                    uc1.postconditions AS post1, uc1.preconditions AS pre1,
                    uc2.id AS id2, uc2.title AS t2,
                    uc2.postconditions AS post2, uc2.preconditions AS pre2
                LIMIT 30
            """, uid=uc_id).data()
        else:
            pairs = s.run("""
                MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc1:SysUseCase {instance:$inst})
                MATCH (us)-[:REALIZED_BY]->(uc2:SysUseCase {instance:$inst})
                WHERE uc1.id < uc2.id
                  AND (uc1.postconditions IS NOT NULL OR uc2.postconditions IS NOT NULL)
                  AND (uc1.preconditions  IS NOT NULL OR uc2.preconditions  IS NOT NULL)
                RETURN DISTINCT
                    uc1.id AS id1, uc1.title AS t1,
                    uc1.postconditions AS post1, uc1.preconditions AS pre1,
                    uc2.id AS id2, uc2.title AS t2,
                    uc2.postconditions AS post2, uc2.preconditions AS pre2
                LIMIT 40
            """, inst=instance).data()
    drv.close()

    if not pairs:
        scope = f"UC {uc_id}" if uc_id else f"instance {instance}"
        print(f"No UC pairs with pre/postconditions found for {scope}.")
        print("Tip: set preconditions and postconditions on UCs via update-usecase.")
        return

    print(f"\n{'═' * W}")
    print(f"  UC STATE CONSISTENCY — {uc_id or instance}  ({len(pairs)} pair(s) to check)")
    print(f"{'═' * W}")

    conflicts = []
    ok_count  = 0
    for p in pairs:
        id1, id2 = p["id1"], p["id2"]
        # Check both directions: post1 vs pre2, and post2 vs pre1
        checks = []
        if p.get("post1") and p.get("pre2"):
            checks.append((id1, p["t1"], p["post1"], id2, p["t2"], p["pre2"]))
        if p.get("post2") and p.get("pre1"):
            checks.append((id2, p["t2"], p["post2"], id1, p["t1"], p["pre1"]))

        for src_id, src_title, postcond, tgt_id, tgt_title, precond in checks:
            prompt = (
                f"You are checking state consistency between two use cases that share a user story.\n\n"
                f"UC-A ({src_id}): {src_title}\n"
                f"Postconditions: {str(postcond)[:500]}\n\n"
                f"UC-B ({tgt_id}): {tgt_title}\n"
                f"Preconditions: {str(precond)[:500]}\n\n"
                f"Does UC-A's postcondition state conflict with UC-B's preconditions? "
                f"A conflict means UC-A leaves the system in a state that UC-B cannot operate from.\n\n"
                f"Reply with exactly one line: CONFLICT: <one sentence> or OK: <one sentence>"
            )
            try:
                result = _ai_call(prompt, model=model, max_tokens=200, provider=provider).strip()
                first_line = result.split("\n")[0].strip()
                if first_line.upper().startswith("CONFLICT"):
                    conflicts.append((src_id, tgt_id, first_line))
                    print(f"  ⚠  {src_id} → {tgt_id}  {first_line}")
                else:
                    ok_count += 1
                    print(f"  ✓  {src_id} → {tgt_id}  {first_line}")
            except RuntimeError as e:
                print(f"  [AI unavailable]  {src_id} → {tgt_id}  ({e})")
                print(f"     post: {str(postcond)[:120]}")
                print(f"     pre:  {str(precond)[:120]}")

    print(f"\n{'─' * W}")
    print(f"  {ok_count} pair(s) consistent  |  {len(conflicts)} conflict(s) detected")
    if conflicts:
        print(f"\n  Suggested action: align postconditions/preconditions via update-usecase")
    print(f"{'═' * W}")


def cmd_enh_align(args):
    """AI-assisted check that an enhancement's description covers its linked UC behaviours.

    Follows the chain: ENH --EXTENDS--> Feature <--REQUIRES-- UseCase,
    then asks AI whether the ENH description accounts for all UC main flows.

    Usage:
        enh-align --id ENH-024 --ai
        enh-align --instance manage --ai   (checks all in-progress ENHs for instance)
    """
    enh_id   = getattr(args, "id",       "") or ""
    instance = getattr(args, "instance", "") or ""
    model    = getattr(args, "model",    "haiku") or "haiku"
    provider = getattr(args, "provider", "") or ""

    if not enh_id and not instance:
        print("ERROR: provide --id or --instance", file=sys.stderr); sys.exit(1)

    drv = _driver()
    W = 70
    with drv.session() as s:
        if enh_id:
            enh_rows = s.run("""
                MATCH (e:SysEnhancement {id:$id})
                RETURN e.id AS eid, e.title AS etitle, e.description AS edesc, e.status AS estatus
            """, id=enh_id).data()
        else:
            enh_rows = s.run("""
                MATCH (e:SysEnhancement {instance:$inst})
                WHERE e.status IN ['open','in-progress']
                RETURN e.id AS eid, e.title AS etitle, e.description AS edesc, e.status AS estatus
                ORDER BY e.id
            """, inst=instance).data()

        if not enh_rows:
            print(f"No enhancements found.")
            drv.close(); return

        # For each ENH, collect linked UC main flows
        for row in enh_rows:
            eid = row["eid"]
            uc_data = s.run("""
                MATCH (e:SysEnhancement {id:$eid})-[:EXTENDS]->(f:SysFeature)<-[:REQUIRES]-(uc:SysUseCase)
                RETURN DISTINCT uc.id AS uid, uc.title AS utitle,
                       uc.mainFlow AS main, uc.description AS udesc,
                       f.id AS fid, f.name AS fname
            """, eid=eid).data()
            row["ucs"] = uc_data
    drv.close()

    print(f"\n{'═' * W}")
    print(f"  ENH→UC ALIGNMENT CHECK — {enh_id or instance}")
    print(f"{'═' * W}")

    gaps = []
    ok_count = 0
    for row in enh_rows:
        eid   = row["eid"]
        ucs   = row.get("ucs") or []
        edesc = (row.get("edesc") or row.get("etitle") or "").strip()

        print(f"\n  {eid}  [{row['estatus']}]  {(row['etitle'] or '')[:55]}")

        if not ucs:
            print(f"  ⚠  No linked UCs (link via link-enhancement + link-usecase)")
            continue
        if not edesc:
            print(f"  ⚠  No description on ENH — set via update-enhancement --description")
            continue

        uc_block = "\n".join(
            f"  UC {u['uid']} ({u['fname']}): {u['utitle']}\n"
            f"  Main flow: {(u['main'] or u['udesc'] or '(none)')[:400]}"
            for u in ucs
        )
        prompt = (
            f"You are checking whether an enhancement description adequately covers "
            f"all the use case behaviours it is supposed to implement.\n\n"
            f"Enhancement {eid}: {row['etitle']}\n"
            f"Description: {edesc[:600]}\n\n"
            f"Linked use cases:\n{uc_block}\n\n"
            f"Does the enhancement description account for all the main flows described in these use cases? "
            f"Identify any UC behaviour that is NOT mentioned or implied by the enhancement.\n\n"
            f"Reply with exactly one line: ALIGNED: <summary> or GAP: <what is missing>"
        )
        try:
            result = _ai_call(prompt, model=model, max_tokens=300, provider=provider).strip()
            first_line = result.split("\n")[0].strip()
            if first_line.upper().startswith("GAP"):
                gaps.append((eid, first_line))
                print(f"  ⚠  {first_line}")
            else:
                ok_count += 1
                print(f"  ✓  {first_line}")
        except RuntimeError as e:
            print(f"  [AI unavailable]  ({e})")
            print(f"  UCs linked: {', '.join(u['uid'] for u in ucs)}")

    print(f"\n{'─' * W}")
    print(f"  {ok_count} ENH(s) aligned  |  {len(gaps)} gap(s) detected")
    if gaps:
        print(f"\n  Suggested action: update-enhancement --description to cover missing UC behaviours")
    print(f"{'═' * W}")


def cmd_worklog(args):
    """Show open defects and pending enhancements for an instance with feature and code links.

    This is the session 'what to work on' view — complements briefing (coverage) by
    surfacing actionable items with direct pointers to features and code paths.
    """
    instance = args.instance
    system   = getattr(args, "system", "") or ""
    drv      = _driver()
    today    = date.today().isoformat()

    with drv.session() as s:
        _validate_instance(instance, s)
        if system:
            mod_ids = [r["id"] for r in s.run("""
                MATCH (:SysSystem {id:$sys})-[:CONTAINS]->(:SysDomain)-[:CONTAINS]->(m:SysModule {instance:$inst})
                RETURN m.id AS id
            """, inst=instance, sys=system).data()]
        else:
            mod_ids = [r["id"] for r in s.run("""
                MATCH (m:SysModule {instance:$inst}) RETURN m.id AS id
            """, inst=instance).data()]

        # Notes and proposals are shown even with no modules
        no_modules = not mod_ids

        # Open defects — primary filter is d.instance; module links used for display only.
        # Filtering by m.id IN $mids was wrong: coalesce(mf, md) picked the AFFECTS module
        # even when it belonged to another instance, and defects with no graph links were invisible.
        defects = s.run("""
            MATCH (d:SysDefect) WHERE (d.status <> 'closed' AND d.closedAt IS NULL) AND d.instance = $inst
            OPTIONAL MATCH (d)-[:AFFECTS]->(f:SysFeature)<-[:PROVIDES]-(mf:SysModule)
            OPTIONAL MATCH (d)-[:DETECTED_IN]->(md:SysModule)
            WITH d, f,
                 CASE WHEN mf IS NOT NULL AND mf.id IN $mids THEN mf
                      WHEN md IS NOT NULL AND md.id IN $mids THEN md
                      ELSE coalesce(mf, md) END AS m
            RETURN d.id AS did, d.title AS title,
                   coalesce(d.severity,'medium') AS sev,
                   d.source AS src,
                   d.occurrences AS occ,
                   d.lastSeen AS seen,
                   f.id AS fid, f.name AS fname,
                   m.id AS mid, m.name AS mname,
                   coalesce(m.codePaths, m.paths, []) AS paths
            ORDER BY
              CASE coalesce(d.severity,'medium')
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium'   THEN 2 ELSE 3 END,
              d.id
        """, mids=mod_ids, inst=instance).data()

        # Active notes for this instance
        active_notes = s.run("""
            MATCH (n:SysNote {instance:$inst})
            WHERE n.status = 'active'
            RETURN n.id AS nid, n.body AS body, n.createdAt AS created, n.expiresAt AS expires
            ORDER BY n.createdAt DESC
        """, inst=instance).data()

        # Open proposals for this instance
        open_proposals = s.run("""
            MATCH (p:SysProposal {instance:$inst})
            WHERE p.status IN ['draft','accepted']
            RETURN p.id AS pid, p.title AS title, p.status AS status,
                   coalesce(p.priority,'Should') AS priority,
                   p.description AS desc, p.createdAt AS created
            ORDER BY
              CASE p.status WHEN 'accepted' THEN 0 ELSE 1 END,
              CASE coalesce(p.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              p.id
        """, inst=instance).data()

        # Feedback count for the footer prompt
        feedback_count = s.run("""
            MATCH (f:SysFeedback {instance:$inst}) RETURN count(f) AS n
        """, inst=instance).single()["n"]

        # Enhancements — proposed/approved/in-progress for this instance
        since_filter = "AND e.createdAt >= $since" if getattr(args, "since", None) else ""
        enhancements = s.run(f"""
            MATCH (e:SysEnhancement)
            WHERE e.instance = $inst AND e.status IN ['proposed','approved','in-progress']
            {since_filter}
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            OPTIONAL MATCH (e)-[:BLOCKED_BY]->(bl:SysEnhancement)
            OPTIONAL MATCH (bl)-[:BLOCKED_BY]->(bl2:SysEnhancement)
            WITH e, collect(DISTINCT {{fid:f.id, fname:f.name, mid:m.id, mname:m.name, paths:coalesce(m.codePaths, m.paths, [])}}) AS features,
                 collect(DISTINCT {{bid:bl.id, bst:bl.status, bl2id:bl2.id, bl2st:bl2.status}}) AS blockers
            RETURN e.id AS eid, e.title AS title,
                   e.status AS status,
                   coalesce(e.priority,'Should') AS priority,
                   e.description AS desc,
                   e.deployChecklist AS deployChecklist,
                   features, blockers
            ORDER BY
              CASE e.status
                WHEN 'in-progress' THEN 0 WHEN 'approved' THEN 1
                ELSE 2 END,
              CASE coalesce(e.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              e.id
        """, inst=instance, since=getattr(args, "since", "") or "").data()

        # Default: hide ENHs whose only feature links point outside this instance's modules
        # --all opts out of this scoping
        # FB-131: strict only applies when modules are known — skip if mod_ids is empty
        # (empty mod_ids means modules not yet restored; hiding ENHs would mislead)
        use_strict = not getattr(args, "all", False) and len(mod_ids) > 0
        if use_strict:
            def _enh_in_scope(e):
                # Proposed ENHs haven't been scoped yet — always show them
                if e.get("status") == "proposed":
                    return True
                feats = [f for f in (e.get("features") or []) if f.get("fid")]
                if not feats:
                    return True   # unlinked ENHs always shown (instance match is enough)
                return any(f.get("mid") in mod_ids for f in feats)
            enhancements = [e for e in enhancements if _enh_in_scope(e)]

        # ENH-556: recently closed enhancements for this instance (last 3 days)
        from datetime import timedelta as _td
        _three_days_ago = (date.today() - _td(days=3)).isoformat()
        closed_today = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.instance = $inst AND e.status = 'done'
              AND e.completedAt IS NOT NULL AND e.completedAt >= $since
            RETURN e.id AS eid, e.title AS title
            ORDER BY e.completedAt DESC LIMIT 10
        """, inst=instance, since=_three_days_ago).data()

        # ENH-1423: open ENHs with linked features that have no verifying tests
        untested_enhs = s.run("""
            MATCH (e:SysEnhancement {instance:$inst})
            WHERE e.status IN ['proposed','approved','in-progress']
            MATCH (e)-[:EXTENDS]->(f:SysFeature)
            WHERE NOT (:SysTest)-[:VERIFIES]->(f)
              AND coalesce(f.status,'') <> 'Superseded'
            RETURN e.id AS eid, e.title AS title, e.status AS status,
                   collect(DISTINCT f.id) AS untested_fids
            ORDER BY e.id
        """, inst=instance).data()

        # Doc artifact staleness — queried inside the session to avoid driver-after-close crash
        _stale_docs = []
        if instance in ("content", "deploy"):
            from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
            _doc_cutoff = (_dt2.now(_tz2.utc) - _td2(days=30)).isoformat()
            _stale_docs = s.run("""
                MATCH (d:SysDocArtifact {instance:$inst})
                WHERE d.lastReviewedAt IS NULL OR d.lastReviewedAt < $cutoff
                RETURN d.id AS id, d.path AS path, d.docType AS dtype,
                       substring(coalesce(d.lastReviewedAt, 'never'), 0, 10) AS reviewed
                ORDER BY d.docType, d.id
            """, inst=instance, cutoff=_doc_cutoff).data()

        # Cross-instance proposed enhancements — always shown (proposed = unclaimed candidates)
        # --cross-instance flag kept for backward compat; no longer changes behaviour
        # When --system is set, restrict to instances whose modules belong to the same system.
        if system:
            _sys_instances = [r["inst"] for r in s.run("""
                MATCH (:SysSystem {id:$sys})-[:CONTAINS]->(:SysDomain)-[:CONTAINS]->(m:SysModule)
                WHERE m.instance IS NOT NULL AND m.instance <> ''
                RETURN DISTINCT m.instance AS inst
            """, sys=system).data()]
        else:
            _sys_instances = None  # no restriction

        cross_inst_proposed = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.instance <> $inst AND e.status = 'proposed'
              AND ($sys_instances IS NULL OR e.instance IN $sys_instances)
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)
            WITH e, collect(DISTINCT f.id) AS feature_ids
            RETURN e.id AS eid, e.title AS title,
                   coalesce(e.instance,'?') AS instance,
                   coalesce(e.priority,'Should') AS priority,
                   e.description AS desc,
                   feature_ids
            ORDER BY
              CASE coalesce(e.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              e.instance, e.id
        """, inst=instance, sys_instances=_sys_instances).data()

    drv.close()

    W = 62
    since_tag = f"  (since {args.since})" if getattr(args, "since", None) else ""
    use_strict = not getattr(args, "all", False)
    strict_tag = "  [all]" if getattr(args, "all", False) else ""
    cross_tag  = ""  # cross-instance proposed always shown; flag kept for backward compat
    print("═" * W)
    sys_tag2 = f"  [{system}]" if system else ""
    print(f"  WORK LOG — {instance}{sys_tag2}  ({today}){since_tag}{strict_tag}{cross_tag}")
    print(f"  What to work on this session")
    print("═" * W)

    # ── Active notes ─────────────────────────────────────────────────────────
    if active_notes:
        print(f"\nSESSION NOTES ({len(active_notes)})  — expire with: expire-note --id NOTE-xxx")
        today_str = date.today().isoformat()
        for n in active_notes:
            age_days = ""
            try:
                from datetime import datetime as _dt
                created = _dt.fromisoformat(n["created"][:10])
                days = (date.today() - created.date()).days
                age_days = f"  [{days}d old]"
            except Exception:
                pass
            expires_tag = f"  expires {n['expires'][:10]}" if n.get("expires") else ""
            print(f"\n  📝 {n['nid']}{age_days}{expires_tag}")
            body = (n["body"] or "").strip()
            for line in body.splitlines()[:4]:
                print(f"     {line}")
            if len(body.splitlines()) > 4:
                print(f"     … ({len(body.splitlines())-4} more lines)")

    # ── Open proposals ────────────────────────────────────────────────────────
    if open_proposals:
        print(f"\nOPEN PROPOSALS ({len(open_proposals)})  — design work awaiting decision or filing")
        for p in open_proposals:
            status_mark = "✦" if p["status"] == "accepted" else "◇"
            print(f"\n  {status_mark} {p['pid']}  [{p['status']}]  [{p['priority']}]")
            print(f"     {p['title'][:70]}")
            if p.get("desc"):
                desc = (p["desc"] or "").strip()
                if len(desc) > 10:
                    print(f"     {desc[:150]}{'…' if len(desc) > 150 else ''}")

    # ── Defects ───────────────────────────────────────────────────────────────
    open_d = list(defects)
    if open_d:
        print(f"\nOPEN DEFECTS ({len(open_d)})")
        seen_defects: set = set()
        for d in open_d:
            if d["did"] in seen_defects:
                continue
            seen_defects.add(d["did"])
            sev      = d.get("sev","medium")
            sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}.get(sev,"●●○○")
            occ_tag  = f"  ×{d['occ']}" if d.get("occ") and int(d["occ"]) > 1 else ""
            src_tag  = " [log]" if d.get("src") == "log" else ""
            print(f"\n  {sev_mark} {d['did']}  [{sev}]{src_tag}{occ_tag}")
            print(f"     {d['title'][:70]}")
            if d.get("fname"):
                print(f"     Feature : {d['fid']}  {d['fname']}")
            else:
                print(f"     Feature : (not linked — use link-defect to associate)")
            if d.get("mname"):
                print(f"     Module  : {d['mid']}  {d['mname']}")
                if d.get("paths"):
                    for p in (d["paths"] or [])[:3]:
                        print(f"     Code    : {p}")
            if d.get("seen"):
                print(f"     Last seen: {str(d['seen'])[:19]}")
    else:
        print(f"\nOPEN DEFECTS — none ✓")

    # ── Enhancements — grouped by status then priority then module ────────────
    def _print_enh_group(group_label, group):
        if not group:
            return
        # Group by module within the list (preserving priority order)
        by_mod = {}
        for e in group:
            feats = [f for f in (e.get("features") or []) if f.get("mid")]
            mod_key = feats[0]["mid"] if feats else "(unlinked)"
            by_mod.setdefault(mod_key, []).append(e)
        mod_order = list(dict.fromkeys(
            next((f["mid"] for f in (e.get("features") or []) if f.get("mid")), "(unlinked)")
            for e in group
        ))
        print(f"\nENHANCEMENTS — {group_label} ({len(group)})")
        for mod_key in mod_order:
            items = by_mod[mod_key]
            if mod_key != "(unlinked)" and len(mod_order) > 1:
                print(f"\n  ── {mod_key} ──")
            for e in items:
                prio_mark = {"Must":"★★★","Should":"★★☆","Could":"★☆☆"}.get(e["priority"],"★★☆")
                print(f"\n  {prio_mark} {e['eid']}  [{e['priority']}]")
                print(f"     {e['title'][:70]}")
                if e.get("desc"):
                    desc = e["desc"].strip()
                    if len(desc) > 10:
                        print(f"     {desc[:150]}{'…' if len(desc) > 150 else ''}")
                feats = [f for f in (e.get("features") or []) if f.get("fid")]
                if feats:
                    for f in feats[:3]:
                        print(f"     Feature : {f['fid']}  {f.get('fname','')}")
                    shown_mods = set()
                    for f in feats:
                        if f.get("mid") and f["mid"] not in shown_mods:
                            shown_mods.add(f["mid"])
                            print(f"     Module  : {f['mid']}  {f.get('mname','')}")
                            for p in (f.get("paths") or [])[:2]:
                                print(f"     Code    : {p}")
                else:
                    print(f"     Feature : (not yet linked — use link-enhancement)")
                # Deploy checklist — shown for in-progress enhancements
                if e.get("deployChecklist") and e.get("status") == "in-progress":
                    print(f"     Deploy:")
                    for line in e["deployChecklist"].splitlines():
                        print(f"       {line}")
                # ENH-547: blocked-by visibility with transitive chain (DEF-1391)
                active_blockers = [b for b in (e.get("blockers") or [])
                                   if b.get("bid") and b.get("bst") not in ("done", None)]
                if active_blockers:
                    for b in active_blockers:
                        chain = f"⛔ BLOCKED BY: {b['bid']}  [{b['bst']}]"
                        if b.get("bl2id"):
                            chain += f"  ← {b['bl2id']}  [{b.get('bl2st','?')}]  (depth 2)"
                        print(f"     {chain}")

    if enhancements:
        in_prog  = [e for e in enhancements if e["status"] == "in-progress"]
        approved = [e for e in enhancements if e["status"] == "approved"]
        proposed = [e for e in enhancements if e["status"] == "proposed"]
        _print_enh_group("IN PROGRESS", in_prog)
        _print_enh_group("APPROVED — ready to implement", approved)
        _print_enh_group("PROPOSED", proposed)
    else:
        print(f"\nENHANCEMENTS — none pending")

    # ENH-1423: untested open enhancements warning
    if untested_enhs:
        print(f"\n{'─' * W}")
        print(f"  ⚠  OPEN ENHANCEMENTS WITH UNTESTED FEATURES ({len(untested_enhs)})")
        print(f"     These ENHs have linked features with 0 verifying tests.")
        print(f"{'─' * W}")
        for ue in untested_enhs:
            fid_str = ", ".join(ue["untested_fids"][:4])
            if len(ue["untested_fids"]) > 4:
                fid_str += f" … +{len(ue['untested_fids'])-4}"
            print(f"  {ue['eid']}  [{ue['status']}]  {ue['title'][:55]}")
            print(f"     No tests: {fid_str}")
        print(f"  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")

    # ── Cross-instance proposed enhancements ─────────────────────────────────
    if cross_inst_proposed:
        # Compact summary: instance counts + top 15 Must/Should items
        by_ci_inst: dict = {}
        for e in cross_inst_proposed:
            by_ci_inst.setdefault(e["instance"], []).append(e)
        inst_summary = "  ".join(f"{k}({len(v)})" for k, v in sorted(by_ci_inst.items()))
        print(f"\nPROPOSED CANDIDATES — OTHER INSTANCES ({len(cross_inst_proposed)})")
        print(f"  {inst_summary}")
        if getattr(args, "cross_instance", False):
            # Full per-instance detail view
            for ci_inst, ci_list in sorted(by_ci_inst.items()):
                print(f"\n  ── {ci_inst} ──")
                for e in ci_list:
                    prio_mark = {"Must":"★★★","Should":"★★☆","Could":"★☆☆"}.get(e["priority"],"★★☆")
                    fid_tag = f"  [{e['feature_ids'][0]}]" if e.get("feature_ids") and e["feature_ids"][0] else ""
                    print(f"  {prio_mark} {e['eid']:<12}{fid_tag}  {e['title'][:58]}")
        else:
            # Compact: top-15 by priority with instance tag
            _SHOW_LIMIT = 15
            shown = cross_inst_proposed[:_SHOW_LIMIT]
            hidden = len(cross_inst_proposed) - len(shown)
            for e in shown:
                prio_mark = {"Must":"★★★","Should":"★★☆","Could":"★☆☆"}.get(e["priority"],"★★☆")
                inst_tag  = f"[{e['instance']:<10}]"
                print(f"  {prio_mark} {e['eid']:<12} {inst_tag}  {e['title'][:48]}")
            if hidden:
                print(f"  … and {hidden} more — run worklog --cross-instance for full per-instance list")

    print(f"\n{'─' * W}")
    total_work = len(open_d) + len(enhancements) + len(open_proposals) + len(active_notes)
    print(f"  {total_work} item(s) · defects {len(open_d)} · enhancements {len(enhancements)}"
          f" · proposals {len(open_proposals)} · notes {len(active_notes)}")
    if cross_inst_proposed:
        print(f"  + {len(cross_inst_proposed)} proposed from other instances")
    elif not enhancements and not open_d and not open_proposals:
        print(f"\n  ── Queue empty ───────────────────────────────────────────────────")
        print(f"  No open ENHs, defects, or proposals for '{instance}'.")
        print(f"  Possible next actions:")
        print(f"    sys_edge.py test-gaps --instance {instance} --tier component")
        print(f"    sys_edge.py briefing  --instance {instance}")
        print(f"    sys_edge.py create-enhancement --instance {instance} --title '...'")
        print(f"    Ask the user for direction.")
    print(f"  sys_edge.py close-defect --id <id>  |  start-enhancement --id <id> --instance {instance}")
    print(f"{'─' * W}")

    # ENH-556: recently closed today for this instance
    if closed_today:
        print(f"\nRECENTLY CLOSED — {today}")
        for r in closed_today:
            print(f"  ✓ {r['eid']}  {r['title'][:60]}")
        print()

    # ── Doc artifact staleness — shown for content and deploy instances
    if _stale_docs:
            print(f"\n{'─'*W}")
            print(f"  DOC ARTIFACTS STALE >30 DAYS ({len(_stale_docs)})")
            print(f"{'─'*W}")
            for _d in _stale_docs:
                print(f"  ⚠  {_d['id']:<28}  [{_d['dtype']}]  last: {_d['reviewed']}")
                print(f"     {_d['path']}")
            print(f"  Run: python3 scripts/sys_edge.py mark-doc-reviewed --id DOC-xxx")

    # ── Feedback prompt — at end so defects/enhancements are immediately visible
    fb_tag = f"  ({feedback_count} entr{'y' if feedback_count==1 else 'ies'} submitted)" if feedback_count else ""
    print(f"\n{'▶'*2} GRAPH FEEDBACK{fb_tag} — record observations any time this session")
    print(f"   python3 scripts/sys_edge.py feedback --instance {instance} --category <cat> --body \"...\"")
    print(f"   Categories: general | usability | gap | workflow | positive")
    print(f"   1. Is the skill file clear — any rules that stop you without a clear alternative?")
    print(f"   2. Is the worklog/briefing output useful at session start, or do you skip parts?")
    print(f"   3. Are use cases and features a useful frame when designing/testing?")
    print(f"   4. Are there sys_edge.py commands missing that you have had to work around?")
    print(f"   5. Has the graph changed how you approach test coverage decisions?")


def cmd_health_report(args):
    """Single-screen system-wide health snapshot for architect session start (ENH-1452).

    Aggregates: open defects by instance, enhancement counts by instance/priority,
    feature coverage gaps, key traceability warnings, silent instances, backup freshness.
    """
    drv = _driver()
    today = date.today().isoformat()
    W = 62

    with drv.session() as s:
        # Open defects grouped by instance
        defect_rows = s.run("""
            MATCH (d:SysDefect) WHERE (d.status <> 'closed' AND d.closedAt IS NULL)
            OPTIONAL MATCH (d)-[:AFFECTS]->(:SysFeature)<-[:PROVIDES]-(mf:SysModule)
            OPTIONAL MATCH (d)-[:DETECTED_IN]->(md:SysModule)
            WITH d, coalesce(mf.instance, md.instance, 'unknown') AS inst
            RETURN inst,
                   count(d) AS total,
                   sum(CASE WHEN d.severity = 'critical' THEN 1 ELSE 0 END) AS critical,
                   sum(CASE WHEN d.severity = 'high'     THEN 1 ELSE 0 END) AS high,
                   sum(CASE WHEN d.severity = 'medium'   THEN 1 ELSE 0 END) AS medium
            ORDER BY critical DESC, high DESC, total DESC
        """).data()

        # Open enhancements by instance and priority
        enh_rows = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.status IN ['proposed','approved','in-progress']
              AND e.instance IS NOT NULL
            RETURN e.instance AS inst,
                   count(e) AS total,
                   sum(CASE WHEN coalesce(e.priority,'Should') = 'Must'   THEN 1 ELSE 0 END) AS must,
                   sum(CASE WHEN coalesce(e.priority,'Should') = 'Should' THEN 1 ELSE 0 END) AS should,
                   sum(CASE WHEN e.status = 'in-progress'                 THEN 1 ELSE 0 END) AS wip
            ORDER BY must DESC, total DESC
        """).data()

        # Feature coverage by instance — mirrors sys_edge_health.py logic
        cov_rows = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE m.instance IS NOT NULL
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN m.instance AS inst,
                   count(DISTINCT f) AS total_f,
                   count(DISTINCT CASE WHEN t IS NOT NULL THEN f END) AS tested_f
            ORDER BY inst
        """).data()

        # Traceability warnings
        stale_symbols = s.run("""
            MATCH (s:SysSymbol) WHERE s.filePath IS NOT NULL AND NOT s.filePath STARTS WITH 'VIRTUAL'
            RETURN count(s) AS n
        """).single()["n"]
        # (actual file-existence check is done in health.py via os.path — approximate here)

        stale_pkgs = s.run("""
            MATCH (p:SysTestPackage) WHERE p.path IS NOT NULL
            RETURN count(p) AS n
        """).single()["n"]

        orphan_ucs = s.run("""
            MATCH (uc:SysUseCase) WHERE NOT (uc)-[:REQUIRES]->(:SysFeature)
            RETURN count(uc) AS n
        """).single()["n"]

        superseded_enh = s.run("""
            MATCH (e:SysEnhancement)-[:EXTENDS]->(f:SysFeature {status:'Superseded'})
            WHERE e.status IN ['proposed','approved','in-progress']
            RETURN count(e) AS n
        """).single()["n"]

        null_uc_desc = s.run("""
            MATCH (uc:SysUseCase) WHERE uc.description IS NULL OR uc.description = '(null)'
            RETURN count(uc) AS n
        """).single()["n"]

        missing_completed_at = s.run("""
            MATCH (e:SysEnhancement {status:'done'}) WHERE e.completedAt IS NULL
            RETURN count(e) AS n
        """).single()["n"]

        tests_no_pkg = s.run("""
            MATCH (t:SysTest) WHERE NOT (:SysTestPackage)-[:CONTAINS_TEST]->(t)
            RETURN count(t) AS n
        """).single()["n"]

        # Silent instances — based on last SysFeedback entry (mirrors health.py)
        from datetime import timedelta as _tdh
        _7d_ago = (date.today() - _tdh(days=7)).isoformat()
        _all_instances = _project_instances(s)
        fb_rows = s.run("""
            MATCH (f:SysFeedback)
            RETURN f.instance AS inst, max(f.createdAt) AS last
            ORDER BY last ASC
        """).data()
        fb_by_inst = {r["inst"]: str(r["last"] or "")[:10] for r in fb_rows if r["inst"]}
        silent_rows = [
            {"inst": inst, "last": fb_by_inst.get(inst, "(none)")}
            for inst in _all_instances
            if not fb_by_inst.get(inst) or fb_by_inst[inst] < _7d_ago
        ]

        # Backup freshness
        backup_rows = s.run("""
            MATCH (n:SysSystem) RETURN coalesce(n.lastBackup,'') AS lastBackup LIMIT 1
        """).data()

    drv.close()

    # ── Backup freshness from file system ──────────────────────────────────────
    import glob as _glob, os as _os
    backup_files = sorted(_glob.glob("data/sys-backup-*.json"), reverse=True)
    backup_fresh = ""
    backup_age_h = None
    if backup_files:
        latest = _os.path.basename(backup_files[0])
        mtime = _os.path.getmtime(backup_files[0])
        import time as _time
        age_h = (_time.time() - mtime) / 3600
        backup_age_h = age_h
        age_str = f"{int(age_h)}h old" if age_h >= 1 else "just now"
        backup_fresh = f"{latest}  ({age_str})"

    # ── Print report ──────────────────────────────────────────────────────────
    print("═" * W)
    print(f"  SYSTEM HEALTH REPORT  ({today})")
    print("═" * W)

    # Defects
    total_defects = sum(r["total"] for r in defect_rows)
    print(f"\nOPEN DEFECTS  (total: {total_defects})")
    if not defect_rows or total_defects == 0:
        print(f"  ✓ none")
    else:
        for r in defect_rows:
            if r["inst"] == "unknown" and r["total"] == 0:
                continue
            tags = []
            if r["critical"]: tags.append(f"{r['critical']} critical")
            if r["high"]:     tags.append(f"{r['high']} high")
            if r["medium"]:   tags.append(f"{r['medium']} medium")
            tag_str = f"  ({', '.join(tags)})" if tags else ""
            marker = "●" if r["critical"] else ("○" if r["high"] else " ")
            print(f"  {marker} {r['inst']:<14} {r['total']:3d}{tag_str}")

    # Enhancements
    total_enhs = sum(r["total"] for r in enh_rows)
    print(f"\nOPEN ENHANCEMENTS  (total: {total_enhs})")
    for r in enh_rows:
        wip_tag = f"  {r['wip']} in-progress" if r["wip"] else ""
        must_tag = f"  Must:{r['must']}" if r["must"] else ""
        print(f"  {r['inst']:<14} {r['total']:3d}{must_tag}{wip_tag}")

    # Feature coverage
    print(f"\nFEATURE COVERAGE BY INSTANCE")
    for r in cov_rows:
        pct = int(100 * r["tested_f"] / r["total_f"]) if r["total_f"] else 100
        marker = "✓" if pct == 100 else "⚠"
        bar = f"{r['tested_f']}/{r['total_f']}"
        print(f"  {marker} {r['inst']:<14} {pct:3d}%  ({bar})")

    # Traceability warnings
    print(f"\nTRACEABILITY WARNINGS")
    warnings_found = False
    def _warn(msg):
        nonlocal warnings_found
        warnings_found = True
        print(f"  ⚠ {msg}")
    if superseded_enh:   _warn(f"{superseded_enh} open ENH(s) linked to Superseded features")
    if orphan_ucs:       _warn(f"{orphan_ucs} use case(s) with no REQUIRES→Feature edge")
    if null_uc_desc:     _warn(f"{null_uc_desc} use case(s) with null/missing description")
    if missing_completed_at: _warn(f"{missing_completed_at} done ENHs missing completedAt timestamp")
    if tests_no_pkg:     _warn(f"{tests_no_pkg} test(s) not linked to a SysTestPackage")
    if not warnings_found:
        print(f"  ✓ none")

    # Silent instances
    print(f"\nSILENT INSTANCES  (no activity in >7 days)")
    if not silent_rows:
        print(f"  ✓ all active")
    else:
        for r in silent_rows:
            print(f"  ⚠ {r['inst']:<14} last: {r['last']}")

    # Backup
    print(f"\nBACKUP")
    if backup_fresh:
        marker = "⚠" if (backup_age_h and backup_age_h > 48) else "✓"
        print(f"  {marker} {backup_fresh}")
        if len(backup_files) > 1:
            print(f"    {len(backup_files)} backup files in data/")
    else:
        print(f"  ⚠ no backup files found in data/")

    print(f"\n{'═' * W}")
    print(f"{'═' * W}")


def cmd_link_defect(args):
    """Create a SysDefect node and optionally link it to a feature (--feature is optional)."""
    feature = getattr(args, "feature", "") or ""
    drv = _driver()
    with drv.session() as s:
        # Fuzzy duplicate check: warn if an open defect on the same feature has a similar title
        if feature:
            existing = s.run("""
                MATCH (d:SysDefect)-[:AFFECTS]->(f:SysFeature {id:$fid})
                WHERE (d.status <> 'closed' AND d.closedAt IS NULL)
                RETURN d.id AS did, d.title AS title
            """, fid=feature).data()
            new_words = set((args.title or "").lower().split())
            for ex in existing:
                old_words = set((ex["title"] or "").lower().split())
                if new_words and old_words:
                    overlap = len(new_words & old_words) / max(len(new_words | old_words), 1)
                    if overlap >= 0.5:
                        print(f"  ⚠  Possible duplicate: {ex['did']} already open on {feature} "
                              f"with similar title: \"{ex['title']}\"", file=sys.stderr)

        if not args.id:
            args.id = _alloc_id(s, "SysDefect", "DEF-")
            print(f"  auto-assigned id: {args.id}", file=sys.stderr)
        ac = getattr(args, "acceptance_criteria", "") or ""
        if not ac:
            print(f"  ⚠  No --acceptance-criteria set. Name the specific assertion or eval\n"
                  f"     question that currently fails and must pass after the fix.\n"
                  f"     Use: update-defect --id {args.id} --acceptance-criteria '...'",
                  file=sys.stderr)
        s.run("""
            MERGE (d:SysDefect {id:$id})
            SET d.title=$title, d.severity=$sev, d.status='open',
                d.createdAt=$now, d.instance=$inst, d.description=$desc,
                d.acceptanceCriteria=$ac
        """, id=args.id, title=args.title, sev=args.severity or "medium",
             now=_now_iso(), inst=args.instance or "",
             desc=getattr(args, "description", "") or "", ac=ac)
        if feature:
            s.run("""MATCH (d:SysDefect {id:$id}),(f:SysFeature {id:$fid})
                     MERGE (d)-[:AFFECTS]->(f)""",
                  id=args.id, fid=feature)
        else:
            print(f"  ⚠  No --feature linked. This DEF won't appear in UC coverage reports.",
                  file=sys.stderr)
            print(f"     Add later: sys_edge.py link-defect --id {args.id} --feature F-xxx",
                  file=sys.stderr)
        # --blocks: create SysDefect-[:BLOCKS]->SysEnhancement (required for close-enhancement --force gate)
        blocks_enh = getattr(args, "blocks", "") or ""
        if blocks_enh:
            enh_row = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.title AS t",
                            id=blocks_enh).single()
            if not enh_row:
                print(f"  ⚠  enhancement '{blocks_enh}' not found — BLOCKS edge not created",
                      file=sys.stderr)
            else:
                s.run("""MATCH (d:SysDefect {id:$did}),(e:SysEnhancement {id:$eid})
                         MERGE (d)-[:BLOCKS]->(e)""", did=args.id, eid=blocks_enh)
                print(f"  BLOCKS  →  {blocks_enh}  ({enh_row['t'][:55]})")
    drv.close()
    feat_tag = f"  →  AFFECTS  →  {feature}" if feature else "  (no feature linked — use link-defect --feature to associate)"
    print(f"✓ {args.id}  [{args.severity or 'medium'}]  {args.title}{feat_tag}")


def cmd_reconcile_done(args):
    """Post-completion reconciliation after closing an enhancement or defect.

    Surfaces feature/UC/US consistency issues, test gaps with ready-to-run
    commands, and auto-files cross-instance enhancements for out-of-scope gaps.
    Read-only except for auto-filed cross-instance enhancement nodes.
    """
    enh_id    = getattr(args, "id",      None) or ""
    def_id    = getattr(args, "defect",  None) or ""
    caller    = getattr(args, "instance", None) or ""
    work_id   = enh_id or def_id
    is_defect = bool(def_id)

    drv = _driver()
    with drv.session() as s:

        # ── 1. Load the work item ──────────────────────────────────────────
        if is_defect:
            row = s.run("""
                MATCH (d:SysDefect {id:$id})
                RETURN d.title AS title, d.description AS desc,
                       d.instance AS inst, d.status AS status
            """, id=def_id).single()
            rel_type = "AFFECTS"
        else:
            row = s.run("""
                MATCH (e:SysEnhancement {id:$id})
                RETURN e.title AS title, e.description AS desc,
                       e.instance AS inst, e.status AS status,
                       e.completedAt AS completedAt
            """, id=enh_id).single()
            rel_type = "EXTENDS"

        if not row:
            print(f"⚠ {work_id} not found", file=sys.stderr)
            drv.close(); return

        inst        = row["inst"] or caller or "unknown"
        title       = row["title"] or ""
        description = row["desc"]  or ""
        completed   = row.get("completedAt") or ""

        # ── 2. Linked features ────────────────────────────────────────────
        features = s.run(f"""
            MATCH (w {{id:$wid}})-[:{rel_type}]->(f:SysFeature)
            OPTIONAL MATCH (m:SysModule)-[:PROVIDES]->(f)
            RETURN f.id AS fid, f.name AS fname, m.id AS mid, m.instance AS minst
        """, wid=work_id).data()

        # ── 3. Per-feature analysis ───────────────────────────────────────
        feature_analysis = []
        for feat in features:
            fid   = feat["fid"]
            fname = feat["fname"] or fid
            minst = feat["minst"] or inst

            ep_count  = s.run("MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature {id:$fid}) RETURN count(ep) AS n", fid=fid).single()["n"]
            sym_count = s.run("MATCH (m:SysModule)-[:CONTAINS_SYMBOL]->(sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature {id:$fid}) RETURN count(sym) AS n", fid=fid).single()["n"]

            # Tier coverage
            tiers = s.run("""
                MATCH (f:SysFeature {id:$fid})
                OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
                WHERE (tc.testType IN ['component','go-unit'])
                   OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
                OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
                WHERE (ti.testType = 'integration')
                   OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
                OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
                WHERE (tu.testType = 'usecase')
                   OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
                OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
                WHERE (te.testType = 'e2e')
                   OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
                RETURN count(DISTINCT tc) AS cmp, count(DISTINCT ti) AS int_,
                       count(DISTINCT tu) AS uc,  count(DISTINCT te) AS e2e
            """, fid=fid).single()

            # UCs and USs reachable from this feature
            ucs = s.run("""
                MATCH (uc:SysUseCase)-[:REQUIRES]->(f:SysFeature {id:$fid})
                OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
                RETURN uc.id AS ucid, uc.title AS uctitle, uc.description AS ucdesc,
                       uc.instance AS ucinst,
                       collect({id: us.id, title: us.title}) AS stories
            """, fid=fid).data()

            feature_analysis.append({
                "fid": fid, "fname": fname, "minst": minst,
                "ep_count": ep_count, "sym_count": sym_count,
                "tiers": dict(tiers), "ucs": ucs,
            })

        # ── 4. Print report ───────────────────────────────────────────────
        W = 70
        print("═" * W)
        label = "DEFECT" if is_defect else "ENHANCEMENT"
        print(f"  RECONCILIATION — {work_id} [{inst}]  {label}")
        print(f"  {title[:W-4]}")

        # ENH-557: infra-only shortcut — no feature traceability needed for deploy/infra
        _INFRA_INSTANCES = {"deploy", "infra", "sys-edge", "sysops"}
        if not features and inst in _INFRA_INSTANCES:
            print("═" * W)
            print(f"  ℹ  Infra-only {label.lower()} — no feature/test traceability required")
            print("═" * W)
            drv.close(); return
        print("═" * W)

        # Features section
        print(f"\n{'─'*W}")
        print(f"  FEATURES ({len(features)})")
        print(f"{'─'*W}")
        for fa in feature_analysis:
            ep_mark  = "✓" if fa["ep_count"]  > 0 else "⚠"
            sym_mark = "✓" if fa["sym_count"] > 0 else "⚠"
            print(f"  {fa['fid']}  {fa['fname']}  [{fa['minst']}]")
            print(f"    endpoints : {fa['ep_count']:3d}  {ep_mark}{'  ← no IMPLEMENTS edges — run link-endpoint' if fa['ep_count']==0 else ''}")
            print(f"    symbols   : {fa['sym_count']:3d}  {sym_mark}{'  ← no CONTAINS_SYMBOL edges — run scan-code or link-symbol' if fa['sym_count']==0 else ''}")

        # UC/US prose review — ENH-553: filter to caller instance; ENH-551: note when all filtered
        # ENH-901: --skip-uc-check suppresses when defect was env/test-bug not a behaviour change
        skip_uc = getattr(args, "skip_uc_check", False)
        all_ucs_raw = [uc for fa in feature_analysis for uc in fa["ucs"]]
        all_ucs = [uc for uc in all_ucs_raw
                   if not caller or not uc.get("ucinst") or uc["ucinst"] == caller]
        filtered_out = len(all_ucs_raw) - len(all_ucs)

        seen_ucs: set = set()
        if all_ucs and not skip_uc:
            print(f"\n{'─'*W}")
            print(f"  USE CASES & USER STORIES — review prose for accuracy")
            print(f"  Enhancement context: {description[:120].strip()}{'...' if len(description)>120 else ''}")
            print(f"{'─'*W}")
            for uc in all_ucs:
                ucid = uc["ucid"]
                if ucid in seen_ucs:
                    continue
                seen_ucs.add(ucid)
                print(f"\n  [KEEP or UPDATE?]  {ucid}  {uc['uctitle'] or ''}")
                if uc.get("ucdesc"):
                    for line in (uc["ucdesc"] or "")[:300].splitlines():
                        print(f"    {line}")
                print(f"    Context: does this UC still accurately describe behaviour after {work_id}?")
                for us in (uc.get("stories") or []):
                    if us.get("id"):
                        print(f"    → US: {us['id']}  {us.get('title','')}")
            if filtered_out:
                print(f"\n  ℹ  {filtered_out} UC(s) from other instances not shown")
        elif filtered_out:
            # ENH-551: UCs exist but all belong to other instances
            print(f"\n  ℹ  {len(all_ucs_raw)} UC(s) linked — all owned by other instance(s), no review needed here")
        elif features:
            # ENH-555: only show "no UCs" message when there are features to attach UCs to
            print(f"\n  ℹ  No Use Cases linked to these features — consider adding UCs via create-usecase")

        # Test gaps + auto-file cross-instance
        auto_filed = []
        print(f"\n{'─'*W}")
        print(f"  TEST GAPS & REQUIRED ACTIONS")
        print(f"{'─'*W}")
        for fa in feature_analysis:
            t      = fa["tiers"]
            minst  = fa["minst"]
            fid    = fa["fid"]
            gaps   = []
            in_scope_gaps   = []
            cross_inst_gaps = []

            for tier, count in [
                ("cmp",  t.get("cmp",  0)),
                ("int",  t.get("int_", 0)),
                ("uc",   t.get("uc",   0)),
                ("e2e",  t.get("e2e",  0)),
            ][: 2 if _lightweight() else 4]:
                mark = "✓" if count > 0 else "✗"
                gaps.append(f"  {mark} {tier:<4} {count:3d} tests")
                if count == 0:
                    if tier == "e2e":
                        cross_inst_gaps.append(("master", tier, fid, fa["fname"]))
                    elif tier == "uc":
                        # ENH-1176: infer owning instance from UC nodes, not feature module
                        uc_insts = {uc.get("ucinst") for uc in fa["ucs"] if uc.get("ucinst")} or {minst}
                        for uc_inst in sorted(uc_insts):
                            if caller and uc_inst != caller:
                                cross_inst_gaps.append((uc_inst, tier, fid, fa["fname"]))
                            else:
                                in_scope_gaps.append((tier, fid))
                    else:
                        if minst and caller and minst != caller:
                            cross_inst_gaps.append((minst, tier, fid, fa["fname"]))
                        else:
                            in_scope_gaps.append((tier, fid))

            print(f"\n  {fid}  {fa['fname']}")
            for g in gaps:
                print(g)
            for tier, fid_ in in_scope_gaps:
                print(f"    → ADD {tier}: python3 scripts/sys_edge.py link-feature \\")
                print(f"          --feature {fid_} --tests \"<test_file>::<TestClass>::<test_name>\"")
            for (target_inst, tier, fid_, fname) in cross_inst_gaps:
                title_prefix = f"Add {tier} tests for {fid_} ({fname})"
                # ENH-1176: skip if an open ENH for this tier+feature+instance already exists
                existing_n = s.run("""
                    MATCH (e:SysEnhancement)-[:EXTENDS]->(f:SysFeature {id: $fid})
                    WHERE e.status IN ['proposed', 'approved', 'in-progress']
                      AND e.instance = $inst
                      AND e.title STARTS WITH $prefix
                    RETURN count(e) AS n
                """, fid=fid_, inst=target_inst, prefix=title_prefix).single()["n"]
                if existing_n > 0:
                    auto_filed.append((None, target_inst, title_prefix))
                    continue
                # Suppress gap ENH if the feature already has any VERIFIES tests indexed.
                # This prevents phantom cascades when tests were committed but scan-tests
                # hasn't run yet — the gap is stale before the ENH is even created.
                has_verifies = s.run("""
                    MATCH (:SysTest)-[:VERIFIES]->(f:SysFeature {id:$fid})
                    RETURN count(*) AS n
                """, fid=fid_).single()["n"]
                if has_verifies > 0:
                    auto_filed.append((None, target_inst, title_prefix))
                    continue
                enh_title = f"{title_prefix} — triggered by {work_id}"
                enh_id_new = _alloc_id(s, "SysEnhancement", "ENH-",
                                       init_props={"status": "proposed"})
                s.run("""
                    MATCH (e:SysEnhancement {id:$id})
                    SET e.title=$title, e.description=$desc,
                        e.instance=$inst, e.priority='Should',
                        e.status='proposed', e.createdAt=$now, e.source=$src
                """, id=enh_id_new, title=enh_title,
                     desc=f"Post-completion gap from {work_id}: {tier} tier has 0 tests for {fid_}. "
                          f"Enhancement: {title}",
                     inst=target_inst, now=_now_iso(), src=work_id)
                s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                         MERGE (e)-[:EXTENDS]->(f)""", eid=enh_id_new, fid=fid_)
                auto_filed.append((enh_id_new, target_inst, enh_title))

        # Auto-filed summary
        if auto_filed:
            print(f"\n{'─'*W}")
            print(f"  AUTO-FILED CROSS-INSTANCE ENHANCEMENTS")
            print(f"{'─'*W}")
            for (eid, einst, etitle) in auto_filed:
                if eid is None:
                    print(f"  ↷ [skipped — already open]  [{einst}]  {etitle[:55]}")
                else:
                    print(f"  ✓ {eid}  [{einst}]  {etitle[:60]}")

        # Final checklist
        print(f"\n{'─'*W}")
        print(f"  CHECKLIST — complete before ending session")
        print(f"{'─'*W}")
        for fa in feature_analysis:
            if fa["ep_count"] == 0:
                print(f"  ☐ link-endpoint for {fa['fid']}")
            if fa["sym_count"] == 0:
                print(f"  ☐ link-symbol / scan-code for {fa['fid']}")
        if all_ucs:
            print(f"  ☐ Review {len(seen_ucs)} UC prose suggestions above — update or confirm KEEP")
        for fa in feature_analysis:
            t = fa["tiers"]
            for tier, count in [("cmp", t.get("cmp",0)), ("int", t.get("int_",0)), ("uc", t.get("uc",0))]:
                if count == 0 and (not caller or fa["minst"] == caller):
                    print(f"  ☐ Add {tier} tests for {fa['fid']}")
        if auto_filed:
            print(f"  ℹ  {len(auto_filed)} cross-instance gap(s) auto-filed — owning sessions will pick them up")
        print(f"  ☐ run link-endpoint + link-symbol for any new endpoints or symbols")
        print()

    drv.close()


def _print_test_gate(cmd_name: str, id_arg: str, no_code_flag: str = "--no-code") -> None:
    """Print the test-confirmation gate message and exit guidance."""
    print(f"""
⛔  STOP — Confirm tests before closing.

   You must run and pass ALL applicable tests before closing a defect or
   enhancement.  Closing without confirmed tests leaves the graph in a
   misleading state and removes the item from the worklog.

   Required steps:
     1. Integration tests  —  pytest backend/tests/<your-area>/ -v
     2. Go unit tests      —  go test ./...  (from backend/)
     3. UI / flow tests    —  playwright test  (if any UI path was affected)
     4. Regression check   —  run the broader suite to catch side effects
     5. Record results     —  sys_edge.py record-run --package <path>

   Once all tests pass, re-run:
     sys_edge.py {cmd_name} {id_arg} --instance <your-instance> --confirm-tests

   No code was changed (graph / documentation only)?
     sys_edge.py {cmd_name} {id_arg} {no_code_flag}
""", file=sys.stderr)


def cmd_close_defect(args):
    """Mark a SysDefect as closed."""
    if not getattr(args, "confirm_tests", False) and not getattr(args, "no_code", False):
        _print_test_gate("close-defect", f"--id {args.id}", "--no-code")
        sys.exit(1)
    drv = _driver()
    with drv.session() as s:
        # Warn if acceptance criteria were never defined
        if not getattr(args, "no_code", False):
            _row = (s.run(
                "MATCH (d:SysDefect {id:$id}) RETURN coalesce(d.acceptanceCriteria,'') AS ac, "
                "coalesce(d.description,'') AS desc",
                id=args.id).single() or {})
            _ac   = (_row.get("ac", "") or "").strip()
            _desc = (_row.get("desc", "") or "").strip().lower()
            _ac_keywords = ("returns", "commits", "passes", "creates", "saves", "resolves",
                            "displays", "shows", "given", "when ", "then ", "should", "must")
            _desc_has_ac = any(kw in _desc for kw in _ac_keywords)
            if not _ac and not _desc_has_ac:
                print(f"⚠  {args.id}: no acceptance criteria on record.", file=sys.stderr)
                print(f"   The fix cannot be verified against a predetermined assertion.", file=sys.stderr)
                print(f"   Add them with: update-defect --id {args.id} --acceptance-criteria '...'",
                      file=sys.stderr)
        resolution = getattr(args, "resolution", "") or ""
        result = s.run(
            "MATCH (d:SysDefect {id:$id}) SET d.status='closed', d.closedAt=$now, "
            "d.resolution=CASE WHEN $res <> '' THEN $res ELSE d.resolution END RETURN d.id",
            id=args.id, now=_now_iso(), res=resolution).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found — nothing closed", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.id} closed")
    if getattr(args, "no_code", False):
        print(f"  (no-code close — test gate waived)")
    else:
        print(f"  ✓ Tests confirmed by caller")
    resolution = getattr(args, "resolution", "") or ""
    if resolution:
        print(f"  Resolution: {resolution}")
    print(f"  □ Confirm: record-run completed after final test run?")
    # Trigger reconciliation
    class _A: pass
    a = _A(); a.id = ""; a.defect = args.id; a.instance = ""
    a.skip_uc_check = getattr(args, "skip_uc_check", False)
    cmd_reconcile_done(a)


def cmd_claim_defect(args):
    """Mark a SysDefect as in-progress (signals to other sessions it is being worked on)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (d:SysDefect {id:$id})
            SET d.status='in-progress', d.claimedAt=$now, d.claimedBy=$by
            RETURN d.title AS title
        """, id=args.id, now=_now_iso(),
             by=getattr(args, "instance", "") or "").single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); sys.exit(1)
    print(f"✓ {args.id} in-progress  —  {result['title']}")


def cmd_unclaim_defect(args):
    """Return an in-progress SysDefect to 'open' state (release claim without closing)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (d:SysDefect {id:$id})
            WHERE d.status = 'in-progress'
            SET d.status='open'
            REMOVE d.claimedAt, d.claimedBy
            RETURN d.title AS title
        """, id=args.id).single()
    drv.close()
    if not result:
        row_drv = _driver()
        with row_drv.session() as rs:
            row = rs.run("MATCH (d:SysDefect {id:$id}) RETURN d.status AS st", id=args.id).single()
        row_drv.close()
        if not row:
            print(f"⚠ {args.id} not found", file=sys.stderr)
        else:
            print(f"⚠ {args.id} is '{row['st']}', not 'in-progress' — nothing changed", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.id} returned to open  —  {result['title']}")

def cmd_add_standard(args):
    """Add a project-specific SysArchStd node (tier=project always)."""
    drv = _driver()
    with drv.session() as s:
        existing = s.run("MATCH (a:SysArchStd {id:$id}) RETURN a.id", id=args.id).single()
        if existing:
            print(f"⚠ {args.id} already exists — use update-enhancement or raw SET to edit",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        types = [t.strip() for t in (args.applies_to or "").split(",") if t.strip()]
        enforce = getattr(args, "enforcement", "mandatory") or "mandatory"
        s.run("""
            CREATE (a:SysArchStd {
                id: $id, title: $title, description: $desc,
                category: $cat, status: 'adopted',
                appliesToTypes: $types, appliesWhen: $when,
                source: 'project', tier: 'project',
                evaluationMethod: $method, enforcement: $enforce
            })
        """, id=args.id, title=args.title, desc=args.description,
             cat=args.category or "quality",
             types=types or ["SysUseCase"],
             when=args.applies_when or "",
             method=args.evaluation or "manual",
             enforce=enforce)
    drv.close()
    print(f"✓ {args.id} created  [{args.category or 'quality'}]  tier=project  evaluation={args.evaluation or 'manual'}  enforcement={enforce}")


def cmd_quality_review(args):
    """Evaluate all applicable SysArchStd standards for an entity or instance.

    Enforcement model:
      mandatory + automated → graph query; failure exits 1 (hard gate)
      mandatory + manual    → checklist item; printed as [MANUAL]
      guidance + automated  → graph query; failure printed as [SUGGEST] (no gate)
      guidance + ai         → LLM assessment; printed as [AI] with score (no gate)
      guidance + manual     → review prompt; printed as [REVIEW] (no gate)

    Usage:
      quality-review --entity UC-DLG-001
      quality-review --entity US-CTM-010 --ai  (enable AI evaluation of guidance standards)
      quality-review --instance manage --type SysUseCase [--ai]
    """
    if getattr(args, "ai", False):
        _require_licence("quality-review --ai")
    entity_id   = getattr(args, "entity",   "") or ""
    instance    = getattr(args, "instance", "") or ""
    type_filter   = getattr(args, "type",     "") or ""
    ai_mode       = getattr(args, "ai",       False)
    model_name    = getattr(args, "model",    "haiku") or "haiku"
    provider_name = getattr(args, "provider", "") or ""

    if not entity_id and not instance:
        print("ERROR: provide --entity or --instance", file=sys.stderr); sys.exit(1)

    W = 72
    drv = _driver()
    with drv.session() as s:

        # ── Resolve entity type(s) ─────────────────────────────────────────
        label_row = None
        if entity_id:
            label_row = s.run("""
                MATCH (n) WHERE n.id = $id
                RETURN labels(n) AS lbls, n.wizardBased AS wizardBased,
                       n.title AS title, n.goal AS goal, n.description AS desc,
                       n.actor AS actor, n.benefit AS benefit,
                       n.preconditions AS preconditions, n.authorizedRoles AS authorizedRoles,
                       n.failureScenarios AS failureScenarios,
                       n.mainFlow AS mainFlow, n.postconditions AS postconditions
            """, id=entity_id).single()
            if not label_row:
                print(f"ERROR: entity '{entity_id}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
            entity_labels = label_row["lbls"]
            wizard_based  = label_row.get("wizardBased") or False
            entity_text   = " ".join(filter(None, [
                label_row.get("title"), label_row.get("goal"),
                label_row.get("actor"), label_row.get("benefit"),
                label_row.get("preconditions"), label_row.get("authorizedRoles"),
                label_row.get("failureScenarios"), label_row.get("mainFlow"),
                label_row.get("postconditions"), label_row.get("desc"),
            ]))
            entity_fields = {k: label_row.get(k) or "" for k in
                             ("actor","goal","benefit","title","desc","preconditions",
                              "authorizedRoles","failureScenarios","mainFlow","postconditions")}
            targets = [(entity_id, entity_labels, wizard_based, entity_text, entity_fields)]
        else:
            target_types = [type_filter] if type_filter else ["SysUseCase", "SysFeature", "SysUserStory"]
            targets = []
            for t in target_types:
                if t == "SysUseCase":
                    rows = s.run("""
                        MATCH (n:SysUseCase {instance:$inst})
                        RETURN n.id AS id, n.wizardBased AS wb, n.title AS t, n.description AS d,
                               n.actor AS actor, n.goal AS goal, n.benefit AS benefit,
                               n.preconditions AS preconditions, n.authorizedRoles AS authorizedRoles,
                               n.failureScenarios AS failureScenarios,
                               n.mainFlow AS mainFlow, n.postconditions AS postconditions
                    """, inst=instance).data()
                    for r in rows:
                        txt = " ".join(filter(None, [r.get("t"), r.get("d"), r.get("mainFlow"), r.get("postconditions")]))
                        fields = {k: r.get(k) or "" for k in
                                  ("actor","goal","benefit","title","preconditions",
                                   "authorizedRoles","failureScenarios","mainFlow","postconditions","d")}
                        fields["desc"] = fields.pop("d", "")
                        targets.append((r["id"], [t], r.get("wb") or False, txt, fields))
                elif t == "SysFeature":
                    rows = s.run("""
                        MATCH (m:SysModule {instance:$inst})-[:PROVIDES]->(n:SysFeature)
                        RETURN n.id AS id, n.name AS t, n.description AS d
                    """, inst=instance).data()
                    for r in rows:
                        targets.append((r["id"], [t], False, r.get("t","") or "", {}))
                elif t == "SysUserStory":
                    rows = s.run("""
                        MATCH (n:SysUserStory)-[:REALIZED_BY]->(uc:SysUseCase {instance:$inst})
                        RETURN DISTINCT n.id AS id, n.title AS t, n.goal AS g,
                               n.actor AS a, n.benefit AS b
                    """, inst=instance).data()
                    for r in rows:
                        txt = " ".join(filter(None, [r.get("t"), r.get("g"), r.get("a"), r.get("b")]))
                        fields = {"actor": r.get("a") or "", "goal": r.get("g") or "",
                                  "benefit": r.get("b") or "", "title": r.get("t") or ""}
                        targets.append((r["id"], [t], False, txt, fields))

        if not targets:
            print(f"No entities found for {'instance ' + instance if instance else entity_id}")
            drv.close(); return

        # ── Graph query checks (automated) ────────────────────────────────
        GRAPH_CHECKS: dict[str, tuple] = {
            "stories_without_uc":      ("MATCH (n:SysUserStory {id:$id}) WHERE NOT (n)-[:REALIZED_BY]->(:SysUseCase) RETURN count(n) AS n", "Missing REALIZED_BY→UseCase"),
            "stories_without_feature": ("MATCH (n:SysUserStory {id:$id}) WHERE NOT (n)-[:REQUIRES]->(:SysFeature) RETURN count(n) AS n", "Missing REQUIRES→Feature"),
            "uc_without_story":        ("MATCH (n:SysUseCase {id:$id}) WHERE NOT (:SysUserStory)-[:REALIZED_BY]->(n) RETURN count(n) AS n", "Missing UserStory→REALIZED_BY"),
            "uc_without_feature":      ("MATCH (n:SysUseCase {id:$id}) WHERE NOT (n)-[:REQUIRES]->(:SysFeature) RETURN count(n) AS n", "Missing REQUIRES→Feature"),
            "uc_without_test":         ("MATCH (n:SysUseCase {id:$id}) WHERE NOT (n)-[:REQUIRES]->(:SysFeature)<-[:VERIFIES]-(:SysTest) RETURN count(n) AS n", "No tests via UC→Feature←Test"),
            "feature_without_module":  ("MATCH (n:SysFeature {id:$id}) WHERE NOT (:SysModule)-[:PROVIDES]->(n) RETURN count(n) AS n", "Missing Module→PROVIDES"),
            "feature_without_test":    ("MATCH (n:SysFeature {id:$id}) WHERE NOT (:SysTest)-[:VERIFIES]->(n) RETURN count(n) AS n", "No VERIFIES→Feature test"),
            "feature_without_uc":      ("MATCH (n:SysFeature {id:$id}) WHERE NOT (:SysUseCase)-[:REQUIRES]->(n) RETURN count(n) AS n", "No SysUseCase REQUIRES this feature"),
        }

        def _text_check(check_key: str, text: str, eid: str,
                        efields: dict | None = None) -> tuple[bool, str]:
            """Returns (passes, detail) for text-based QUS automated checks."""
            import re as _re
            tl = (text or "").lower()
            fields = efields or {}
            if check_key == "story_wellformed":
                # Accept prose 'As a/I want' OR structured actor+goal fields (ENH-919)
                prose_ok = ("as a" in tl or "as an" in tl) and ("i want" in tl or "i need" in tl or "i can" in tl)
                structured_ok = bool(fields.get("actor")) and bool(fields.get("goal"))
                ok = prose_ok or structured_ok
                return ok, "" if ok else "Missing 'As a [role], I want [means]' structure OR actor+goal fields"
            if check_key == "story_atomic":
                # Prefer goal field alone (ENH-919); fall back to full text
                goal_text = (fields.get("goal") or tl).lower()
                goal_part = goal_text.split("so that")[0] if "so that" in goal_text else goal_text
                ok = not _re.search(r'\band\b.{5,}\band\b', goal_part)
                return ok, "" if ok else "Goal may combine multiple objectives (conjunction detected)"
            if check_key == "story_minimal":
                impl_markers = ["shall", "must use", "using [", "via [", "the system will", "database", "api call", "rest "]
                found = [m for m in impl_markers if m in tl]
                return not found, "" if not found else f"Implementation detail detected: '{found[0]}'"
            if check_key == "story_full_sentence":
                ok = len(text.split()) >= 5
                return ok, "" if ok else "Story text is too short to be a complete sentence"
            if check_key == "story_unique":
                # Uniqueness check requires comparing across stories — skip per-entity
                return True, ""
            if check_key == "story_uniform":
                # Uniform check requires set context — skip per-entity
                return True, ""
            return True, ""

        # Load all standards
        all_stds = s.run("""
            MATCH (a:SysArchStd)
            WHERE a.appliesToTypes IS NOT NULL
            RETURN a.id AS id, a.title AS title, a.tier AS tier,
                   a.appliesToTypes AS types, a.appliesWhen AS when,
                   a.evaluationMethod AS method, a.checkKey AS check,
                   a.category AS cat, a.enforcement AS enforcement,
                   a.description AS desc
            ORDER BY a.enforcement DESC, a.tier DESC, a.category, a.id
        """).data()

        fail_total = suggest_total = pass_total = manual_total = review_total = 0
        all_entity_fails = []

        for eid, elabels, ewizard, etext, efields in targets:
            applicable = [
                std for std in all_stds
                if any(lbl in (std["types"] or []) for lbl in elabels)
                and not (std.get("when") == "wizardBased=true" and not ewizard)
            ]
            if not applicable:
                continue

            print(f"\n{'═' * W}")
            print(f"  quality-review: {eid}  ({', '.join(elabels)})")
            print(f"{'═' * W}")

            deferred_ai = []
            entity_fails = []

            for std in applicable:
                sid      = std["id"]
                method   = std.get("method") or "manual"
                enforce  = std.get("enforcement") or "mandatory"
                tier     = std.get("tier") or "default"
                tbadge   = f"[{tier[:3]}]"
                check    = std.get("check")
                is_gate  = (enforce == "mandatory")

                if method == "automated":
                    # Graph query or text check
                    if check and check in GRAPH_CHECKS:
                        q, gap = GRAPH_CHECKS[check]
                        n = s.run(q, id=eid).single()["n"]
                        passes = (n == 0)
                    elif check and check.startswith("story_"):
                        passes, gap = _text_check(check, etext, eid, efields)
                    elif check == "coverage_ratio":
                        # TRC-006: system-level ratio check, skip per-entity
                        continue
                    else:
                        continue

                    if passes:
                        print(f"  [PASS]    {sid:<16} {tbadge} {std['title'][:42]}")
                        pass_total += 1
                    elif is_gate:
                        print(f"  [FAIL]    {sid:<16} {tbadge} {std['title'][:42]}")
                        print(f"            → {gap}")
                        fail_total += 1
                        entity_fails.append(sid)
                    else:
                        print(f"  [SUGGEST] {sid:<16} {tbadge} {std['title'][:42]}")
                        print(f"            → {gap}")
                        suggest_total += 1

                elif method == "ai":
                    if ai_mode:
                        deferred_ai.append(std)
                    else:
                        label = "[AI]   " if not ai_mode else "[AI]   "
                        print(f"  {label}   {sid:<16} {tbadge} {std['title'][:42]}  (run with --ai to evaluate)")

                elif method == "manual":
                    if is_gate:
                        print(f"  [MANUAL]  {sid:<16} {tbadge} {std['title'][:42]}")
                        manual_total += 1
                    else:
                        print(f"  [REVIEW]  {sid:<16} {tbadge} {std['title'][:42]}")
                        review_total += 1

            # ── AI evaluation for guidance+ai standards ────────────────────
            if deferred_ai:
                for std in deferred_ai:
                    sid   = std["id"]
                    tbadge = f"[{(std.get('tier') or 'def')[:3]}]"
                    # Build structured entity block with labelled fields (ENH-922)
                    entity_block_parts = [f"ID: {eid}"]
                    for lbl, key in [("Title", "title"), ("Actor", "actor"), ("Goal", "goal"),
                                     ("Benefit", "benefit"), ("Preconditions", "preconditions"),
                                     ("Authorized roles", "authorizedRoles"),
                                     ("Failure scenarios", "failureScenarios"),
                                     ("Main flow", "mainFlow"),
                                     ("Postconditions", "postconditions"),
                                     ("Description", "desc")]:
                        val = efields.get(key) or efields.get(key.lower(), "")
                        if val:
                            entity_block_parts.append(f"{lbl}: {str(val)[:300]}")
                    entity_block = "\n".join(entity_block_parts) or etext[:800]
                    prompt = (
                        f"You are evaluating an entity against a quality standard.\n\n"
                        f"## Entity\n{entity_block}\n\n"
                        f"## Standard: {std['title']}\n{std.get('desc','')}\n\n"
                        f"Evaluate the entity against this standard. "
                        f"Return exactly one line: SCORE: GOOD|WEAK|POOR — one sentence reasoning."
                    )
                    try:
                        reply = _ai_call(prompt, model_name, max_tokens=128, provider=provider_name)
                        score = "POOR"
                        for v in ("GOOD","WEAK","POOR"):
                            if v in reply.upper(): score = v; break
                        reason = reply.split("—",1)[-1].strip() if "—" in reply else reply
                        print(f"  [AI-{score:<4}] {sid:<16} {tbadge} {std['title'][:42]}")
                        print(f"            {reason[:90]}")
                    except Exception as e:
                        print(f"  [AI-ERR]  {sid:<16}  ({e})", file=sys.stderr)

            # Deferred non-AI AS-REQ/AS-TEST — note commands
            deferred_cmd = [st for st in applicable
                            if st.get("method") == "automated"
                            and st.get("check") not in GRAPH_CHECKS
                            and not (st.get("check") or "").startswith("story_")
                            and st.get("check") != "coverage_ratio"
                            and st["id"].startswith(("AS-REQ-","AS-TEST-"))]
            if deferred_cmd:
                ef = f"--uc {eid}" if "SysUseCase" in elabels else f"--us {eid}" if "SysUserStory" in elabels else f"--feature {eid}"
                req  = [st for st in deferred_cmd if st["id"].startswith("AS-REQ-")]
                test = [st for st in deferred_cmd if st["id"].startswith("AS-TEST-")]
                if req:  print(f"  [NOTE]    {len(req)} AS-REQ → python3 scripts/sys_edge.py coverage-review {ef}")
                if test: print(f"  [NOTE]    {len(test)} AS-TEST → python3 scripts/sys_edge.py audit-test {ef}")

            if entity_fails:
                all_entity_fails.extend(entity_fails)

    drv.close()
    print(f"\n{'═' * W}")
    print(f"  PASS {pass_total}  FAIL {fail_total}  SUGGEST {suggest_total}  MANUAL {manual_total}  REVIEW {review_total}")
    if all_entity_fails:
        print(f"  Mandatory failures: {', '.join(all_entity_fails[:8])}")
    print(f"{'═' * W}\n")
    if fail_total:
        sys.exit(1)

def cmd_impact(args):
    """Impact analysis: given a file path or symbol name, show which features and tests are affected."""
    path   = getattr(args, "path",   "") or ""
    symbol = getattr(args, "symbol", "") or ""
    if not path and not symbol:
        print("ERROR: specify --path and/or --symbol", file=sys.stderr); sys.exit(1)
    drv = _driver()
    with drv.session() as s:
        results = {}
        if path:
            # Find symbols in this file → features; tests in this file → features
            sym_rows = s.run("""
                MATCH (sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature)
                WHERE sym.file CONTAINS $path
                RETURN DISTINCT f.id AS fid, f.name AS fname, sym.symbol AS sym_name
                ORDER BY fid
            """, path=path).data()
            test_rows = s.run("""
                MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature)
                WHERE t.id CONTAINS $path
                RETURN DISTINCT f.id AS fid, f.name AS fname, count(t) AS tc
                ORDER BY fid
            """, path=path).data()
            ep_rows = s.run("""
                MATCH (e:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature)
                WHERE e.path CONTAINS $path OR e.binary CONTAINS $path
                RETURN DISTINCT f.id AS fid, f.name AS fname, e.method AS method, e.path AS epath
                ORDER BY fid
            """, path=path).data()
            results["path"] = {"symbols": sym_rows, "tests": test_rows, "endpoints": ep_rows}
        if symbol:
            sym_rows2 = s.run("""
                MATCH (sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature)
                WHERE sym.symbol = $sym OR sym.symbol CONTAINS $sym
                RETURN DISTINCT f.id AS fid, f.name AS fname, sym.file AS file
                ORDER BY fid
            """, sym=symbol).data()
            results["symbol"] = sym_rows2
    drv.close()

    W = 66
    print(f"\n{'═'*W}\n  IMPACT ANALYSIS\n{'═'*W}")
    if path:
        r = results.get("path", {})
        print(f"\n  File: {path}")
        if r.get("symbols"):
            print(f"  Via symbols ({len(r['symbols'])} feature links):")
            for row in r["symbols"][:10]:
                print(f"    {row['fid']:<16} {row['fname'][:40]}  [{row['sym_name']}]")
        if r.get("tests"):
            print(f"  Via tests ({len(r['tests'])} feature links):")
            for row in r["tests"][:10]:
                print(f"    {row['fid']:<16} {row['fname'][:40]}  ({row['tc']} tests)")
        if r.get("endpoints"):
            print(f"  Via endpoints ({len(r['endpoints'])} feature links):")
            for row in r["endpoints"][:10]:
                print(f"    {row['fid']:<16} {row['fname'][:40]}  [{row['method']} {row['epath'][:30]}]")
        if not any([r.get("symbols"), r.get("tests"), r.get("endpoints")]):
            print(f"  ✗ No feature links found for path '{path}'")
    if symbol:
        rows2 = results.get("symbol", [])
        print(f"\n  Symbol: {symbol}")
        if rows2:
            print(f"  Feature links ({len(rows2)}):")
            for row in rows2[:10]:
                print(f"    {row['fid']:<16} {row['fname'][:40]}  [{row.get('file','')[:30]}]")
        else:
            print(f"  ✗ No feature links found for symbol '{symbol}'")
    print(f"\n{'═'*W}")


def cmd_show_blockers(args):
    """Show all BLOCKED_BY chains: what is blocked, what is blocking it, and status."""
    instance = getattr(args, "instance", "") or ""
    drv = _driver()
    with drv.session() as s:
        rows = s.run("""
            MATCH (open:SysEnhancement)-[:BLOCKED_BY]->(blocker:SysEnhancement)
            WHERE ($inst = '' OR open.instance = $inst)
            RETURN open.id AS eid, open.title AS etitle, open.status AS estat,
                   blocker.id AS bid, blocker.title AS btitle, blocker.status AS bstat,
                   open.instance AS inst
            ORDER BY bstat, bid, open.instance, eid
        """, inst=instance).data()
    drv.close()
    if not rows:
        print("  ✓ No BLOCKED_BY relationships found" + (f" for instance '{instance}'" if instance else ""))
        return
    W = 70
    print(f"\n{'═'*W}\n  BLOCKED_BY CHAINS{(' — ' + instance) if instance else ''}\n{'═'*W}")
    last_bid = None
    for r in rows:
        if r["bid"] != last_bid:
            bstat_tag = "✓done" if r["bstat"] == "done" else ("⚠" + r["bstat"])
            print(f"\n  BLOCKER: {r['bid']}  [{bstat_tag}]  {(r['btitle'] or '')[:50]}")
            last_bid = r["bid"]
        estat_tag = r["estat"] or "proposed"
        print(f"    ← {r['eid']:<14} [{r['inst']:<12}] [{estat_tag}]  {(r['etitle'] or '')[:40]}")
    print(f"\n  {len(rows)} blocked relationship(s)")


def cmd_link_commit(args):
    """Record a git commit as a SysNote attached to an enhancement or defect."""
    sha     = args.sha.strip()
    message = getattr(args, "message", "") or ""
    eid     = getattr(args, "enhancement", "") or ""
    did     = getattr(args, "defect", "") or ""
    instance = getattr(args, "instance", "") or ""
    if not eid and not did:
        print("ERROR: specify --enhancement or --defect to link the commit to", file=sys.stderr)
        sys.exit(1)
    drv = _driver()
    with drv.session() as s:
        note_id = _alloc_id(s, "SysNote", "NOTE-")
        body = f"git commit {sha}" + (f": {message}" if message else "")
        s.run("""
            MERGE (n:SysNote {id:$nid})
            SET n.body=$body, n.instance=$inst, n.type='commit-link',
                n.commitSha=$sha, n.createdAt=$now, n.status='active'
        """, nid=note_id, body=body, inst=instance, sha=sha, now=_now_iso())
        if eid:
            if not s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=eid).single():
                print(f"⚠ enhancement '{eid}' not found", file=sys.stderr)
            else:
                s.run("""MATCH (n:SysNote {id:$nid}),(e:SysEnhancement {id:$eid})
                         MERGE (n)-[:NOTED_ON]->(e)""", nid=note_id, eid=eid)
                print(f"✓ {sha[:8]}  →  commit-note  →  {eid}")
        if did:
            if not s.run("MATCH (d:SysDefect {id:$id}) RETURN d.id", id=did).single():
                print(f"⚠ defect '{did}' not found", file=sys.stderr)
            else:
                s.run("""MATCH (n:SysNote {id:$nid}),(d:SysDefect {id:$did})
                         MERGE (n)-[:NOTED_ON]->(d)""", nid=note_id, did=did)
                print(f"✓ {sha[:8]}  →  commit-note  →  {did}")
    drv.close()


def _instance_to_feat_prefix(instance: str) -> str:
    """Derive a Feature ID prefix from an instance name, e.g. 'sys-edge' → 'F-SE-'.

    Splits on hyphens/underscores, takes the first letter of each part (uppercased).
    Falls back to 'F-AUTO-' when instance is empty or unrecognisable.
    """
    if not instance:
        return "F-AUTO-"
    parts = [p.upper() for p in instance.replace("_", "-").split("-") if p]
    if len(parts) == 1:
        abbrev = parts[0][:4]
    else:
        abbrev = "".join(p[0] for p in parts)
    return f"F-{abbrev}-"


def cmd_scan_features(args):
    """Auto-detect new exported symbols in a diff and propose/create Feature nodes.

    Workflow (with --enh):
      1. git diff --name-only <since>..HEAD to find changed files
      2. Extract exported symbols that are not yet IMPLEMENTS-linked to any Feature
      3. Propose ONE Feature named from the ENH title, or wire to --feature if given
      4. Create Feature (collision-safe ID), IMPLEMENTS edges, ENH→EXTENDS edge
      5. Confirm before writing (unless --yes)

    Without --enh:
      • If exactly one candidate ENH is found by date, behaves as if --enh was passed.
      • If multiple ENHs found, prints them and asks you to re-run with --enh <id>.
      • If no ENH found, falls back to one Feature per changed module (original behaviour).
    """
    import subprocess as _sp
    from collections import defaultdict as _dd

    since         = (getattr(args, "since",   "") or "").strip()
    instance      = (getattr(args, "instance","") or "").strip()
    explicit_enh  = (getattr(args, "enh",     "") or "").strip()
    explicit_feat = (getattr(args, "feature", "") or "").strip()
    module_filter = (getattr(args, "module",  "") or "").strip()
    yes           = getattr(args, "yes",      False)
    dry_run       = getattr(args, "dry_run",  False)

    # --feature without --enh makes no sense
    if explicit_feat and not explicit_enh:
        print("ERROR: --feature requires --enh (specify which ENH introduced this feature)",
              file=sys.stderr)
        sys.exit(1)

    # ── 1. Resolve git SHA ────────────────────────────────────────────────────
    if not since:
        res = _sp.run(["git", "rev-parse", "HEAD~1"], capture_output=True, text=True,
                      cwd=str(_root))
        if res.returncode != 0:
            print("ERROR: --since is required (could not auto-detect HEAD~1)", file=sys.stderr)
            sys.exit(1)
        since = res.stdout.strip()
        print(f"  (--since not given, defaulting to HEAD~1: {since[:12]})")

    chk = _sp.run(["git", "cat-file", "-t", since], capture_output=True, text=True,
                  cwd=str(_root))
    if chk.returncode != 0 or chk.stdout.strip() not in ("commit", "tag"):
        print(f"ERROR: '{since}' is not a valid git commit ref", file=sys.stderr)
        sys.exit(1)

    # ── 2. Changed files ──────────────────────────────────────────────────────
    diff_res = _sp.run(["git", "diff", "--name-only", f"{since}..HEAD"],
                       capture_output=True, text=True, cwd=str(_root))
    if diff_res.returncode != 0:
        print(f"ERROR: git diff failed: {diff_res.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    code_exts = {".go", ".ts", ".tsx", ".py", ".java", ".cs"}
    changed_code = [p.strip() for p in diff_res.stdout.splitlines()
                    if p.strip() and Path(p.strip()).suffix in code_exts]
    if not changed_code:
        print(f"  No code files changed since {since[:12]}. Nothing to do.")
        return
    print(f"  Changed code files since {since[:12]}: {len(changed_code)}")

    # ── 3. Extract & filter symbols ───────────────────────────────────────────
    drv = _driver()
    with drv.session() as s:

        dir_mappings = _load_dir_categories(s)
        path_map     = _load_module_path_map(s)

        def _mod(file_str: str) -> str:
            return (_derive_module_from_dir(file_str, dir_mappings)
                    or _find_module(file_str, path_map))

        all_syms: list[dict] = []
        for rel_path in changed_code:
            abs_path = _root / rel_path
            if not abs_path.exists() or _should_skip(abs_path):
                continue
            mod_id = _mod(rel_path)
            if module_filter and mod_id != module_filter:
                continue
            ext = abs_path.suffix
            if   ext == ".go":            syms = _extract_symbols_go(abs_path, mod_id)
            elif ext in (".ts", ".tsx"):  syms = _extract_symbols_ts(abs_path, mod_id)
            elif ext == ".py":            syms = _extract_symbols_py(abs_path, mod_id)
            else:                         syms = []
            for sym in syms:
                sym["file"] = rel_path
                sym["id"]   = f"{rel_path}::{sym['symbol']}"
            all_syms.extend(syms)

        if not all_syms:
            print("  No exported symbols found in changed files.")
            drv.close(); return

        print(f"  Total symbols extracted: {len(all_syms)}")

        # Skip symbols already linked to a Feature
        linked_ids = {
            r["sid"] for r in s.run(
                "MATCH (sym:SysSymbol)-[:IMPLEMENTS]->(:SysFeature) RETURN sym.id AS sid"
            ).data()
        }
        unlinked = [sym for sym in all_syms if sym["id"] not in linked_ids]
        if not unlinked:
            print("  All symbols already linked to features. Nothing to do.")
            drv.close(); return
        print(f"  Unlinked symbols (no IMPLEMENTS edge): {len(unlinked)}")

        # ── 4. Resolve ENH(s) ─────────────────────────────────────────────────
        if explicit_enh:
            enh_row = s.run(
                "MATCH (e:SysEnhancement {id:$id}) RETURN e.id AS eid, e.title AS etitle, "
                "e.instance AS einst",
                id=explicit_enh
            ).single()
            if not enh_row:
                print(f"ERROR: enhancement '{explicit_enh}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
            target_enhs = [dict(enh_row)]
        else:
            # Auto-discover by date
            commit_date = _sp.run(
                ["git", "log", "-1", "--format=%cI", since],
                capture_output=True, text=True, cwd=str(_root)
            )
            since_iso = commit_date.stdout.strip() if commit_date.returncode == 0 else "1970-01-01"
            q = (
                "MATCH (e:SysEnhancement) "
                "WHERE e.status IN ['in_progress','done'] "
                "AND (e.startedAt >= $since OR e.createdAt >= $since)"
            )
            params: dict = {"since": since_iso}
            if instance:
                q += " AND e.instance = $inst"
                params["inst"] = instance
            q += " RETURN e.id AS eid, e.title AS etitle, e.instance AS einst ORDER BY e.id"
            target_enhs = s.run(q, **params).data()

            if len(target_enhs) > 1 and not explicit_enh:
                print(f"\n  ⚠  {len(target_enhs)} candidate ENHs found since {since[:12]} —")
                print("     re-run with --enh <id> to scope to one ENH per feature:\n")
                for e in target_enhs:
                    print(f"       {e['eid']}  [{e['einst']}]  {e['etitle']}")
                print()
                print("  Falling back to one Feature per changed module (coarse grouping).")
                target_enhs = []   # handled below in fallback branch

        # ── 5. Resolve existing features on changed modules ───────────────────
        mod_ids = list({sym.get("moduleId","") for sym in unlinked if sym.get("moduleId")})
        existing_by_mod: dict = {}
        if mod_ids:
            for row in s.run(
                "MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature) WHERE m.id IN $mids "
                "RETURN m.id AS mid, f.id AS fid, f.name AS fname",
                mids=mod_ids
            ).data():
                existing_by_mod.setdefault(row["mid"], []).append(
                    {"id": row["fid"], "name": row["fname"]}
                )

        # ── 6. Build proposals ────────────────────────────────────────────────
        # Each proposal = {feature_id, feature_name, module_id, symbols, enh_ids, is_new}
        proposals: list[dict] = []

        if explicit_feat:
            # User said "wire everything to this existing Feature"
            feat_row = s.run(
                "MATCH (f:SysFeature {id:$id}) RETURN f.id AS fid, f.name AS fname",
                id=explicit_feat
            ).single()
            if not feat_row:
                print(f"ERROR: feature '{explicit_feat}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
            primary_mod = (unlinked[0].get("moduleId") or "") if unlinked else ""
            proposals.append({
                "feature_id":    explicit_feat,
                "feature_name":  feat_row["fname"],
                "module_id":     primary_mod,
                "symbols":       unlinked,
                "enh_ids":       [e["eid"] for e in target_enhs],
                "is_new":        False,
                "existing_alts": [],
            })

        elif len(target_enhs) == 1:
            # Exactly one ENH — one Feature for all unlinked symbols
            enh = target_enhs[0]
            enh_inst = enh.get("einst") or instance or ""
            prefix   = _instance_to_feat_prefix(enh_inst)
            feat_id  = _alloc_id(s, "SysFeature", prefix, pad=3,
                                 init_props={"status": "Proposed"})
            feat_name = f"[auto] {enh['etitle']}"[:120]
            primary_mod = (unlinked[0].get("moduleId") or "") if unlinked else ""
            proposals.append({
                "feature_id":    feat_id,
                "feature_name":  feat_name,
                "module_id":     primary_mod,
                "symbols":       unlinked,
                "enh_ids":       [enh["eid"]],
                "is_new":        True,
                "existing_alts": existing_by_mod.get(primary_mod, []),
            })

        else:
            # Fallback: one Feature per changed module (no ENH scoping)
            by_module: dict = _dd(list)
            for sym in unlinked:
                by_module[sym.get("moduleId") or "unknown"].append(sym)

            fallback_inst = instance or ""
            prefix = _instance_to_feat_prefix(fallback_inst)
            for mod_id, syms in by_module.items():
                feat_id   = _alloc_id(s, "SysFeature", prefix, pad=3,
                                      init_props={"status": "Proposed"})
                sym_hint  = ", ".join(sym["symbol"] for sym in syms[:3])
                if len(syms) > 3: sym_hint += " …"
                proposals.append({
                    "feature_id":    feat_id,
                    "feature_name":  f"[auto] {mod_id} — {sym_hint}"[:120],
                    "module_id":     mod_id,
                    "symbols":       syms,
                    "enh_ids":       [e["eid"] for e in target_enhs],
                    "is_new":        True,
                    "existing_alts": existing_by_mod.get(mod_id, []),
                })

        # ── 7. Present proposals ──────────────────────────────────────────────
        print()
        print("  ┌─ scan-features proposals " + "─" * 36)
        for prop in proposals:
            tag = "(new)" if prop["is_new"] else "(existing)"
            print(f"  │  Feature  {prop['feature_id']}  {tag}  module={prop['module_id']}")
            print(f"  │    name: {prop['feature_name']}")
            syms_preview = ", ".join(sym["symbol"] for sym in prop["symbols"][:6])
            if len(prop["symbols"]) > 6: syms_preview += " …"
            print(f"  │    symbols ({len(prop['symbols'])}): {syms_preview}")
            if prop["enh_ids"]:
                enh_preview = ", ".join(prop["enh_ids"][:4])
                if len(prop["enh_ids"]) > 4: enh_preview += " …"
                print(f"  │    ENH→EXTENDS: {enh_preview}")
            if prop["existing_alts"]:
                alts = ", ".join(f"{f['id']} {f['name']}" for f in prop["existing_alts"][:3])
                print(f"  │    existing features on module: {alts}")
            print("  │")
        print("  └" + "─" * 62)
        print()

        if dry_run:
            print("  --dry-run: nothing written.")
            drv.close(); return

        if not yes:
            ans = input("  Write these proposals to the graph? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("  Aborted.")
                drv.close(); return

        # ── 8. Write ──────────────────────────────────────────────────────────
        n_features = n_symbols = n_enh_links = 0

        for prop in proposals:
            fid = prop["feature_id"]
            mid = prop["module_id"]

            if prop["is_new"]:
                # _alloc_id already CREATEd the node; just SET the remaining props
                s.run("""
                    MATCH (f:SysFeature {id:$fid})
                    SET f.name=$name, f.source='scan-features', f.description=''
                """, fid=fid, name=prop["feature_name"])
                n_features += 1

            if mid and mid != "unknown":
                s.run("""MATCH (m:SysModule {id:$mid}),(f:SysFeature {id:$fid})
                         MERGE (m)-[:PROVIDES]->(f)""", mid=mid, fid=fid)

            for sym in prop["symbols"]:
                sid = sym["id"]
                s.run("""
                    MERGE (sym:SysSymbol {id:$sid})
                    SET sym.file=$file, sym.symbol=$symbol,
                        sym.line=$line, sym.symbolType=$stype, sym.language=$lang
                """, sid=sid, file=sym["file"], symbol=sym["symbol"],
                     line=sym.get("line", 0), stype=sym.get("symbolType", "function"),
                     lang=sym.get("language", ""))
                s.run("""MATCH (sym:SysSymbol {id:$sid}),(f:SysFeature {id:$fid})
                         MERGE (sym)-[:IMPLEMENTS]->(f)""", sid=sid, fid=fid)
                if mid and mid != "unknown":
                    s.run("""MATCH (m:SysModule {id:$mid}),(sym:SysSymbol {id:$sid})
                             MERGE (m)-[:CONTAINS_SYMBOL]->(sym)""", mid=mid, sid=sid)
                n_symbols += 1

            for eid in prop["enh_ids"]:
                if s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=eid).single():
                    s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                             MERGE (e)-[:EXTENDS]->(f)""", eid=eid, fid=fid)
                    n_enh_links += 1

        print(f"\n  ✓ scan-features wrote:")
        print(f"      {n_features} Feature node(s) created")
        print(f"      {n_symbols} SysSymbol→IMPLEMENTS→Feature edge(s)")
        print(f"      {n_enh_links} ENH→EXTENDS→Feature edge(s)")
        print()
        print("  Review with:  python3 scripts/sys_edge.py features")
        print("  Rename:       python3 scripts/sys_edge.py update-feature --id <F-xx> --name '...'")

    drv.close()


def cmd_open_viz(args):
    """Print the launch path for sys_edge_viz.py and optionally start it.

    Usage:
        sys_edge.py open-viz               # prints path + env vars needed
        sys_edge.py open-viz --launch      # also starts the server in the background
    """
    viz_candidates = [
        Path(__file__).resolve().parent / "sys_edge_viz.py",
        Path(__file__).resolve().parent.parent / "scripts" / "sys_edge_viz.py",
        Path(__file__).resolve().parent / "cli" / "sys_edge_viz.py",
    ]
    viz_path = next((p for p in viz_candidates if p.exists()), None)
    if not viz_path:
        print("⚠  sys_edge_viz.py not found (visualiser not included in this build)", file=sys.stderr)
        print("   Expected locations:", file=sys.stderr)
        for p in viz_candidates:
            print(f"     {p}", file=sys.stderr)
        sys.exit(1)

    port = getattr(args, "port", None) or 8888
    print(f"\n  SysEdge Visualiser")
    print(f"  ──────────────────────────────────────────────")
    print(f"  Script:  {viz_path}")
    print(f"  Port:    {port}")
    print(f"\n  Launch command:")
    print(f"    python3 {viz_path} --port {port}")
    print(f"\n  The server prints a URL with ?token=… — open that exact URL.")
    print(f"  Env vars needed (same as sys_edge.py):")
    print(f"    SYSEDGE_NEO4J_URI      (default: bolt://localhost:7688)")
    print(f"    SYSEDGE_NEO4J_USER     (default: neo4j)")
    print(f"    SYSEDGE_NEO4J_PASSWORD (required)")

    if getattr(args, "launch", False):
        import subprocess as _sp
        env_copy = os.environ.copy()
        proc = _sp.Popen([sys.executable, str(viz_path), "--port", str(port)],
                         env=env_copy)
        print(f"\n  ✓ Launched (PID {proc.pid}) — open the URL it prints.")
        print(f"  Stop with: kill {proc.pid}")
    print()


def cmd_session_close(args):
    """Composite end-of-session command: runs worklog, health check, and backup.

    Use at the end of a session to ensure the graph is healthy and persisted.
    Prints a session summary without requiring multiple separate commands.
    """
    instance = getattr(args, "instance", "") or ""
    print("\n" + "═"*62)
    print(f"  SESSION CLOSE — {instance or 'all instances'}")
    print("═"*62 + "\n")

    # Step 1: Health check (abbreviated)
    import subprocess as _sp
    health_cmd = ["python3", "scripts/sys_edge_health.py"]
    if instance:
        health_cmd += ["--instance", instance]
    health_result = _sp.run(health_cmd, capture_output=True, text=True, cwd=_root)
    # Print just the WARNINGS line
    for line in health_result.stdout.splitlines():
        if "WARNING" in line or "PASS" in line.upper() or "FAIL" in line.upper():
            print(f"  health: {line.strip()}")
            break
    if health_result.returncode != 0:
        print(f"  ⚠  health check warnings — see above", file=sys.stderr)

    # Step 2: Worklog summary (open counts)
    if instance:
        drv = _driver()
        with drv.session() as s:
            open_enhs = s.run(
                "MATCH (e:SysEnhancement) WHERE e.instance=$inst AND e.status <> 'done' RETURN count(e) AS n",
                inst=instance).single()["n"]
            open_defs = s.run(
                "MATCH (d:SysDefect) WHERE d.instance=$inst AND d.status <> 'closed' RETURN count(d) AS n",
                inst=instance).single()["n"]
        drv.close()
        print(f"  open work: {open_enhs} enhancements, {open_defs} defects")

    # Step 3: Backup
    if not getattr(args, "no_backup", False):
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y-%m-%dT%H-%M-%S")
        out_path = _root / f"data/sys-backup-{ts}.json"
        _do_backup(out_path)

    print("\n  ✓ Session closed\n")


def cmd_close_enhancement(args):
    """Mark one or more SysEnhancements as done.

    Use --id for a single enhancement or --ids for a comma-separated batch.
    Batch mode (--ids) suppresses the post-close checklist; reconcile still runs per item.
    """
    # Batch mode via --ids
    ids_batch = getattr(args, "ids", "") or ""
    if ids_batch:
        ids = [x.strip() for x in ids_batch.split(",") if x.strip()]
        args.skip_checklist = True
        for eid in ids:
            args.id = eid
            args.ids = ""
            cmd_close_enhancement(args)
        return

    # Test-confirmation gate — waived for graph-only, test-only, or --force (documents debt)
    skip_gate = (
        getattr(args, "confirm_tests", False)
        or getattr(args, "graph_only", False)
        or getattr(args, "test_only", False)
        or getattr(args, "force", False)
    )
    if not skip_gate:
        _print_test_gate("close-enhancement", f"--id {args.id}", "--graph-only")
        sys.exit(1)

    drv = _driver()
    with drv.session() as s:
        # AC presence check — warn if acceptance criteria were never defined
        if not getattr(args, "graph_only", False) and not getattr(args, "force", False):
            _ac_row = s.run(
                "MATCH (e:SysEnhancement {id:$id}) RETURN coalesce(e.acceptanceCriteria,'') AS ac",
                id=args.id
            ).single()
            if _ac_row and not (_ac_row["ac"] or "").strip():
                print(f"⚠  {args.id}: no acceptance criteria on record.", file=sys.stderr)
                print(f"   Acceptance criteria should have been defined before work started", file=sys.stderr)
                print(f"   (sys_edge.py start-enhancement now requires them).", file=sys.stderr)
                print(f"   Closing anyway — add them with update-enhancement for traceability.", file=sys.stderr)

        # --force gate: require a linked test-debt defect to document the debt explicitly
        if getattr(args, "force", False) and not getattr(args, "graph_only", False):
            _debt_n = s.run("""
                MATCH (e:SysEnhancement {id:$id})<-[:BLOCKS]-(d:SysDefect)
                WHERE toLower(d.title) CONTAINS 'test debt'
                RETURN count(d) AS n
            """, id=args.id).single()["n"]
            if _debt_n == 0:
                print(f"⛔  --force requires a test-debt defect linked to {args.id}.", file=sys.stderr)
                print(f"   File one first:", file=sys.stderr)
                print(f"   sys_edge.py link-defect --title 'Test debt: {args.id}' \\", file=sys.stderr)
                print(f"     --blocks {args.id} --instance <name> --feature <F-xxx>", file=sys.stderr)
                drv.close(); return

        # ENH-548: --verify-entity check before closing
        verify_types = [t.strip() for t in (getattr(args, "verify_entity", "") or "").split(",") if t.strip()]
        if verify_types:
            for vtype in verify_types:
                if vtype not in _VALID_SYS_LABELS:
                    print(f"⚠  --verify-entity: unknown node type {vtype!r} — "
                          f"valid: {', '.join(sorted(_VALID_SYS_LABELS))}", file=sys.stderr)
                    continue
                count = s.run(
                    f"MATCH (e:SysEnhancement {{id:$id}})-[*1..2]->(n:{vtype}) RETURN count(DISTINCT n) AS n",
                    id=args.id
                ).single()["n"]
                if count == 0:
                    print(f"⚠  --verify-entity: no {vtype} nodes reachable from {args.id} — "
                          f"link them first or omit --verify-entity", file=sys.stderr)
                    drv.close(); return
        # ENH-1423: block close when linked features have 0 verifying tests
        # Skip check for --graph-only (no code changes expected) or --force override
        if not getattr(args, 'graph_only', False) and not getattr(args, 'force', False):
            _pre_lf = s.run(
                "MATCH (e:SysEnhancement {id:$id})-[:EXTENDS]->(f:SysFeature) RETURN count(f) AS n",
                id=args.id
            ).single()["n"]
            if _pre_lf > 0:
                _pre_tc = s.run("""
                    MATCH (e:SysEnhancement {id:$id})-[:EXTENDS]->(f:SysFeature)<-[:VERIFIES]-(t:SysTest)
                    RETURN count(DISTINCT t) AS n
                """, id=args.id).single()["n"]
                if _pre_tc == 0:
                    print(f"⛔  {args.id}: {_pre_lf} linked feature(s) but 0 verifying tests.",
                          file=sys.stderr)
                    print(f"   Link tests:  sys_edge.py link-feature --feature F-xxx --tests 'test_file.py::TestClass'",
                          file=sys.stderr)
                    print(f"   Or pass --force with a test-debt defect linked:", file=sys.stderr)
                    print(f"   sys_edge.py link-defect --title 'Test debt: {args.id}' \\", file=sys.stderr)
                    print(f"     --blocks {args.id} --instance <name> --feature <F-xxx>", file=sys.stderr)
                    drv.close(); return
                # e2e/usecase tier gate — verifying tests must include at least one usecase-tier test
                _e2e_tc = s.run("""
                    MATCH (e:SysEnhancement {id:$id})-[:EXTENDS]->(f:SysFeature)<-[:VERIFIES]-(t:SysTest)
                    WHERE t.testType IN ['e2e','usecase']
                    RETURN count(DISTINCT t) AS n
                """, id=args.id).single()["n"]
                if _e2e_tc == 0:
                    print(f"⛔  {args.id}: tests exist but none at usecase/e2e tier.", file=sys.stderr)
                    print(f"   If tests are already scanned at integration tier, reclassify them:", file=sys.stderr)
                    print(f"   sys_edge.py reclassify-tests --from integration --to usecase --path <dir>",
                          file=sys.stderr)
                    print(f"   Or add a usecase-tier Playwright UC flow test and link it:", file=sys.stderr)
                    print(f"   sys_edge.py link-usecase --id UC-xxx --tests 'test_xxx.py::TestClass'",
                          file=sys.stderr)
                    print(f"   Or pass --force with a test-debt defect linked (see link-defect --blocks).",
                          file=sys.stderr)
                    drv.close(); return

        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='done', e.completedAt=$now
            RETURN e.title AS title, e.instance AS inst
        """, id=args.id, now=_now_iso()).single()
        # ENH-600: warn when owning instance has UCs with no REQUIRES→Feature edges
        orphan_uc_count = 0
        linked_feature_count = 0
        if result:
            inst_to_check = args.instance or (result["inst"] or "")
            if inst_to_check:
                orphan_uc_count = s.run("""
                    MATCH (uc:SysUseCase {instance: $inst})
                    WHERE NOT (uc)-[:REQUIRES]->(:SysFeature)
                    RETURN count(uc) AS n
                """, inst=inst_to_check).single()["n"]
            linked_feature_count = s.run(
                "MATCH (e:SysEnhancement {id:$id})-[:EXTENDS]->(f:SysFeature) RETURN count(f) AS n",
                id=args.id
            ).single()["n"]
            # ENH-864: UC quality enhancements — check test-gaps on linked features
            title_lower = (result["title"] or "").lower() if result else ""
            is_uc_quality = any(kw in title_lower for kw in
                                ("uc quality", "use case quality", "auth boundaries",
                                 "exception paths", "acceptance criteria", "uc design"))
            untested_uc_features = 0
            if is_uc_quality and linked_feature_count > 0:
                untested_uc_features = s.run("""
                    MATCH (e:SysEnhancement {id:$id})-[:EXTENDS]->(f:SysFeature)
                    WHERE NOT (:SysTest)-[:VERIFIES]->(f)
                    RETURN count(f) AS n
                """, id=args.id).single()["n"]
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    if 'untested_uc_features' not in dir():
        untested_uc_features = 0
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    if args.instance and result["inst"] and result["inst"] != args.instance:
        print(f"⚠  Instance mismatch: {args.id} belongs to '{result['inst']}', "
              f"you are closing from '{args.instance}'", file=sys.stderr)
    if orphan_uc_count:
        inst_to_check = args.instance or (result["inst"] or "")
        print(f"⚠  {orphan_uc_count} UC(s) in '{inst_to_check}' have no REQUIRES→Feature edges — "
              f"run link-usecase or link-endpoint to wire them before ending this session", file=sys.stderr)
    if untested_uc_features:
        print(f"⚠  UC quality enhancement closed but {untested_uc_features} linked feature(s) still have 0 tests — "
              f"run audit-test after writing tests to verify coverage", file=sys.stderr)
    print(f"✓ {args.id} done  —  {result['title']}")
    graph_only = getattr(args, "graph_only", False)
    test_only  = getattr(args, "test_only",  False)
    skip_code  = graph_only or test_only or (linked_feature_count == 0)
    if not getattr(args, "skip_checklist", False):
        print(f"\n  Pre-close checklist — confirm before ending your session:")
        if not skip_code:
            print(f"  □ link-endpoint run for any new API endpoints added this session?")
            print(f"  □ link-symbol run for any new code symbols added?")
        print(f"  □ UC REQUIRES→Feature edges wired? (link-usecase)")
        if not graph_only:
            print(f"  □ Tests linked to features? (link-feature)")
            print(f"  □ Integration tests run and passing?")
            print(f"  □ UI flow tests (Playwright) run for all affected use cases?")
            print(f"  □ Regression check done — no unexpected failures in broader suite?")
            print(f"  □ record-run completed after final test run?")
        print(f"  □ User stories still accurate for what was built?")
        note = ""
        if graph_only:
            note = " [--graph-only: code checklist suppressed]"
        elif test_only:
            note = " [--test-only: endpoint/symbol checklist suppressed]"
        elif linked_feature_count == 0:
            note = " [no linked features: endpoint/symbol checklist suppressed]"
        print(f"  (suppress with --skip-checklist{note})\n")
    # Trigger reconciliation
    class _A: pass
    a = _A(); a.id = args.id; a.defect = ""; a.instance = args.instance or ""
    cmd_reconcile_done(a)


def cmd_create_enhancement(args):
    """Create a new SysEnhancement node with auto-incremented ID."""
    drv = _driver()
    with drv.session() as s:
        _validate_instance(args.instance, s)
        # init_props ensures the node is never bare (no status race with reconcile_done)
        enh_id = _alloc_id(s, "SysEnhancement", "ENH-", init_props={"status": "proposed"})
        # Fuzzy duplicate detection: token overlap >= 50% among open enhancements
        open_enhs = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.status <> 'done' AND e.instance = $inst
            RETURN e.id AS id, e.title AS title
        """, inst=args.instance).data()
        new_words = set((args.title or "").lower().split())
        for ex in open_enhs:
            old_words = set((ex["title"] or "").lower().split())
            if new_words and old_words:
                overlap = len(new_words & old_words) / max(len(new_words | old_words), 1)
                if overlap >= 0.5:
                    print(f"  ⚠  Possible duplicate: {ex['id']} has {int(overlap*100)}% title overlap: "
                          f'"{ex["title"]}"', file=sys.stderr)
        ac = getattr(args, "acceptance_criteria", "") or ""
        if not ac:
            print(f"  ⚠  No --acceptance-criteria set. Define what 'done' looks like before starting work.\n"
                  f"     Use: update-enhancement --id {enh_id} --acceptance-criteria '...'",
                  file=sys.stderr)
        s.run("""
            MATCH (e:SysEnhancement {id: $id})
            SET e.title=$title, e.description=$desc,
                e.instance=$inst, e.priority=$prio,
                e.status='proposed', e.createdAt=$now, e.source=$src,
                e.acceptanceCriteria=$ac
        """, id=enh_id, title=args.title, desc=args.description or "",
             inst=args.instance, prio=args.priority or "Should",
             now=_now_iso(), src=args.source or "", ac=ac)
        if args.feature:
            for fid in args.feature.split(","):
                fid = fid.strip()
                if fid:
                    s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                             MERGE (e)-[:EXTENDS]->(f)""", eid=enh_id, fid=fid)
        else:
            print(f"  ⚠  No --feature linked. This ENH won't appear in UC coverage reports.",
                  file=sys.stderr)
            print(f"     Add later: sys_edge.py link-enhancement --id {enh_id} --feature F-xxx",
                  file=sys.stderr)
    drv.close()
    print(f"✓ {enh_id}  [{args.instance}]  [{args.priority or 'Should'}]  {args.title}")


def cmd_update_enhancement(args):
    """Update title, description, priority, or source on an existing SysEnhancement."""
    drv = _driver()
    with drv.session() as s:
        existing = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id AS id",
                         id=args.id).single()
        if not existing:
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if args.title:       updates["title"]       = args.title
        if args.description: updates["description"] = args.description
        if args.priority:    updates["priority"]    = args.priority
        if args.source:      updates["source"]      = args.source
        if getattr(args, "instance",            ""): updates["instance"]           = args.instance
        if getattr(args, "deploy_checklist",    ""): updates["deployChecklist"]    = args.deploy_checklist
        if getattr(args, "acceptance_criteria", ""): updates["acceptanceCriteria"] = args.acceptance_criteria
        # Guard: these fields must never be mutated via update-enhancement
        for _protected in ("status", "completedAt", "startedAt", "closedAt"):
            updates.pop(_protected, None)
        if not updates:
            print("Nothing to update — specify at least one of --title --description --priority "
                  "--source --instance --deploy-checklist --acceptance-criteria",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"e.{k}=${k}" for k in updates)
        s.run(f"MATCH (e:SysEnhancement {{id:$id}}) SET {set_clause}",
              id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


_FEATURE_STATUSES = {"Proposed","InProgress","Implemented","Deprecated","Superseded"}


def cmd_update_feature(args):
    """Update name, description, status, narrative, source, or module link on an existing SysFeature."""
    drv = _driver()
    with drv.session() as s:
        existing = s.run(
            "MATCH (f:SysFeature {id:$id}) RETURN f.id AS id, f.name AS name",
            id=args.id
        ).single()
        if not existing:
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates: dict = {}
        if getattr(args, "name",        ""): updates["name"]        = args.name
        if getattr(args, "description", ""): updates["description"] = args.description
        if getattr(args, "narrative",   ""): updates["narrative"]   = args.narrative
        if getattr(args, "source",      ""): updates["source"]      = args.source
        if getattr(args, "status",      ""):
            status = args.status
            if status not in _FEATURE_STATUSES:
                print(f"ERROR: invalid status '{status}'. "
                      f"Valid: {', '.join(sorted(_FEATURE_STATUSES))}", file=sys.stderr)
                drv.close(); sys.exit(1)
            updates["status"] = status
        module_id = (getattr(args, "module", "") or "").strip()
        if not updates and not module_id:
            print("Nothing to update — specify at least one of "
                  "--name --description --narrative --source --status --module",
                  file=sys.stderr); drv.close(); return
        if updates:
            set_clause = ", ".join(f"f.{k}=${k}" for k in updates)
            s.run(f"MATCH (f:SysFeature {{id:$id}}) SET {set_clause}", id=args.id, **updates)
        if module_id:
            if not s.run("MATCH (m:SysModule {id:$id}) RETURN m.id", id=module_id).single():
                print(f"ERROR: module '{module_id}' not found", file=sys.stderr)
                drv.close(); sys.exit(1)
            s.run("""MATCH (m:SysModule {id:$mid}),(f:SysFeature {id:$fid})
                     MERGE (m)-[:PROVIDES]->(f)""", mid=module_id, fid=args.id)
            print(f"  PROVIDES  ←  {module_id}")
    drv.close()
    changed = list(updates.keys()) + (["module"] if module_id else [])
    print(f"✓ {args.id} updated: {', '.join(changed)}")


def cmd_show_usecase(args):
    """Display full details of a SysUseCase by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (uc:SysUseCase {id:$id})
            OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
            OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN uc { .* } AS ucnode,
                   collect(DISTINCT {sid:us.id, stitle:us.title}) AS stories,
                   collect(DISTINCT f.id) AS feat_ids,
                   count(DISTINCT t) AS test_count
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); return
    uc = result["ucnode"]
    stories = [s for s in result["stories"] if s.get("sid")]
    print(f"\n{'═'*66}")
    print(f"  {uc['id']}  [{uc.get('instance','')}]  [{uc.get('priority','?')}]")
    print(f"  {uc.get('title','')}")
    print(f"{'─'*66}")
    if stories:
        print(f"  Parent stories  : {', '.join(s['sid'] for s in stories)}")
    if result["feat_ids"]:
        print(f"  Features        : {', '.join(result['feat_ids'])}")
    print(f"  Linked tests    : {result['test_count']} (via UC→Feature←Test)")
    for field, label in [
        ("authorizedRoles",  "Auth roles"),
        ("failureScenarios", "Failure scenarios"),
        ("preconditions",    "Preconditions"),
        ("mainFlow",         "Main flow"),
        ("postconditions",   "Postconditions"),
        ("description",      "Description"),
        ("wizardBased",      "Wizard"),
        ("coverageReviewLastRun",  "Coverage review"),
        ("auditTestLastRun",       "Audit test"),
    ]:
        val = uc.get(field)
        if val is not None and val != "":
            print(f"\n  {label}:\n  {val}")
    print(f"{'═'*66}")


def cmd_list_usecases(args):
    """List SysUseCase IDs and titles, optionally filtered by instance or module."""
    instance   = getattr(args, "instance", "") or ""
    module_id  = getattr(args, "module",   "") or ""
    story_id   = getattr(args, "story",    "") or ""
    drv = _driver()
    with drv.session() as s:
        if instance:
            rows = s.run("""
                MATCH (uc:SysUseCase {instance:$inst})
                OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
                RETURN uc.id AS id, uc.title AS title, uc.priority AS prio,
                       collect(DISTINCT us.id) AS stories
                ORDER BY uc.id
            """, inst=instance).data()
        elif module_id:
            rows = s.run("""
                MATCH (m:SysModule {id:$mid})-[:PROVIDES]->(f:SysFeature)<-[:REQUIRES]-(uc:SysUseCase)
                OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
                RETURN DISTINCT uc.id AS id, uc.title AS title, uc.priority AS prio,
                       collect(DISTINCT us.id) AS stories
                ORDER BY uc.id
            """, mid=module_id).data()
        elif story_id:
            rows = s.run("""
                MATCH (us:SysUserStory {id:$sid})-[:REALIZED_BY]->(uc:SysUseCase)
                RETURN uc.id AS id, uc.title AS title, uc.priority AS prio,
                       [$sid] AS stories
                ORDER BY uc.id
            """, sid=story_id).data()
        else:
            rows = s.run("""
                MATCH (uc:SysUseCase)
                RETURN uc.id AS id, uc.title AS title, uc.priority AS prio,
                       uc.instance AS inst
                ORDER BY uc.instance, uc.id
            """).data()
    drv.close()
    if not rows:
        scope = f"instance '{instance}'" if instance else (f"module '{module_id}'" if module_id else "all instances")
        print(f"No use cases found for {scope}")
        return
    scope_label = instance or module_id or story_id or "all"
    W = 70
    print(f"\n{'═'*W}")
    print(f"  USE CASES  [{scope_label}]  ({len(rows)} total)")
    print(f"{'═'*W}")
    for r in rows:
        story_tag = f"  ← {', '.join(r['stories'])}" if r.get("stories") else ""
        inst_tag  = f"  [{r.get('inst','')}]" if not instance and r.get("inst") else ""
        prio      = f"[{r.get('prio','?')}]"
        print(f"  {r['id']:<20} {prio:<5}{inst_tag}  {(r.get('title') or '')[:48]}{story_tag}")
    print(f"{'═'*W}\n")


def cmd_update_usecase(args):
    """Update title, description, preconditions, mainFlow, postconditions, authorizedRoles, failureScenarios, or priority on a SysUseCase."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (uc:SysUseCase {id:$id}) RETURN uc.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if getattr(args, "title",              ""): updates["title"]             = args.title
        if getattr(args, "description",        ""): updates["description"]       = args.description
        if getattr(args, "preconditions",      ""): updates["preconditions"]     = args.preconditions
        if getattr(args, "main_flow",          ""): updates["mainFlow"]          = args.main_flow
        if getattr(args, "postconditions",     ""): updates["postconditions"]    = args.postconditions
        if getattr(args, "authorized_roles",   ""): updates["authorizedRoles"]   = args.authorized_roles
        if getattr(args, "failure_scenarios",  ""): updates["failureScenarios"]  = args.failure_scenarios
        if getattr(args, "priority",           ""): updates["priority"]          = args.priority
        if not updates:
            print("Nothing to update — specify at least one of "
                  "--title --description --preconditions --main-flow --postconditions "
                  "--authorized-roles --failure-scenarios --priority",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"uc.{k}=${k}" for k in updates)
        s.run(f"MATCH (uc:SysUseCase {{id:$id}}) SET {set_clause}", id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_update_story(args):
    """Update title, goal, benefit, narrative, actor, acceptanceCriteria, or outOfScope on a SysUserStory."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (us:SysUserStory {id:$id}) RETURN us.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if getattr(args, "title",               ""): updates["title"]              = args.title
        if getattr(args, "actor",               ""): updates["actor"]              = args.actor
        if getattr(args, "goal",                ""): updates["goal"]               = args.goal
        if getattr(args, "benefit",             ""): updates["benefit"]            = args.benefit
        if getattr(args, "narrative",           ""): updates["narrative"]          = args.narrative
        if getattr(args, "acceptance_criteria", ""): updates["acceptanceCriteria"] = args.acceptance_criteria
        if getattr(args, "out_of_scope",        ""): updates["outOfScope"]         = args.out_of_scope
        if not updates:
            print("Nothing to update — specify at least one of "
                  "--title --actor --goal --benefit --narrative --acceptance-criteria --out-of-scope",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"us.{k}=${k}" for k in updates)
        s.run(f"MATCH (us:SysUserStory {{id:$id}}) SET {set_clause}", id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_link_enhancement(args):
    """Link a SysEnhancement to a SysFeature via EXTENDS."""
    drv = _driver()
    linked = []
    with drv.session() as s:
        if not s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=args.id).single():
            print(f"ERROR: enhancement '{args.id}' not found", file=sys.stderr); drv.close(); sys.exit(1)
        for fid in args.feature.split(","):
            fid = fid.strip()
            if not fid:
                continue
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"⚠ feature '{fid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                     MERGE (e)-[:EXTENDS]->(f)""", eid=args.id, fid=fid)
            linked.append(fid)
    drv.close()
    for fid in linked:
        print(f"✓ {args.id}  →  EXTENDS  →  {fid}")


def cmd_unlink_enhancement(args):
    """Remove stale EXTENDS (Enhancement→Feature) edges."""
    drv = _driver(admin=True)
    with drv.session() as s:
        if not s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=args.id).single():
            print(f"ERROR: enhancement '{args.id}' not found", file=sys.stderr); drv.close(); sys.exit(1)
        removed = []
        for fid in [x.strip() for x in args.feature.split(",") if x.strip()]:
            result = s.run("""
                MATCH (e:SysEnhancement {id:$eid})-[r:EXTENDS]->(f:SysFeature {id:$fid})
                DELETE r RETURN count(r) AS n
            """, eid=args.id, fid=fid).single()
            if result and result["n"] > 0:
                removed.append(fid)
            else:
                print(f"⚠ no EXTENDS edge from {args.id} to {fid}", file=sys.stderr)
    drv.close()
    for fid in removed:
        print(f"✓ removed  {args.id}  →  EXTENDS  →  {fid}")
    if not removed:
        print("Nothing removed", file=sys.stderr)


def cmd_link_usecase(args):
    """Add feature, story, or test links to an existing SysUseCase without touching its content."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (uc:SysUseCase {id:$id}) RETURN uc.id", id=args.id).single():
            print(f"ERROR: use case '{args.id}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        linked_f = []
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"⚠ feature '{fid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                     MERGE (uc)-[:REQUIRES]->(f)""", uid=args.id, fid=fid)
            linked_f.append(fid)
        linked_s = []
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            if not s.run("MATCH (us:SysUserStory {id:$sid}) RETURN us.id", sid=sid).single():
                print(f"⚠ story '{sid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                     MERGE (us)-[:REALIZED_BY]->(uc)""", sid=sid, uid=args.id)
            linked_s.append(sid)
        # --tests: create VERIFIES edges from SysTest nodes to UC's linked features
        linked_t = []
        for tid in [x.strip() for x in (getattr(args, "tests", "") or "").split(",") if x.strip()]:
            # Resolve test node (full path::class::fn or partial match)
            test_row = s.run(
                "MATCH (t:SysTest) WHERE t.id = $tid OR t.id ENDS WITH $tid RETURN t.id AS id LIMIT 1",
                tid=tid
            ).single()
            if not test_row:
                print(f"  ⚠  test '{tid}' not in graph — creating stub; run scan-tests to populate full metadata",
                      file=sys.stderr)
                s.run("""MERGE (t:SysTest {id:$tid})
                         SET t.scannedAt=$now, t.source='manual-stub',
                             t.type='unclassified', t.testType='unclassified'""",
                      tid=tid, now=_now_iso())
                test_row = {"id": tid}
            # Link to all features required by this UC
            s.run("""
                MATCH (uc:SysUseCase {id:$uid})-[:REQUIRES]->(f:SysFeature)
                MATCH (t:SysTest {id:$tid})
                MERGE (t)-[:VERIFIES]->(f)
            """, uid=args.id, tid=test_row["id"])
            linked_t.append(test_row["id"])
    drv.close()
    for fid in linked_f:
        print(f"✓ {args.id}  →  REQUIRES  →  {fid}")
    for sid in linked_s:
        print(f"✓ {sid}  →  REALIZED_BY  →  {args.id}")
    for tid in linked_t:
        print(f"✓ {tid}  →  VERIFIES  →  {args.id} features")
    if not linked_f and not linked_s and not linked_t:
        print("Nothing linked — specify --feature, --story, and/or --tests", file=sys.stderr)


def cmd_unlink_usecase(args):
    """Remove stale REQUIRES (UC→Feature) or REALIZED_BY (Story→UC) edges."""
    drv = _driver(admin=True)
    with drv.session() as s:
        removed = []
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            result = s.run("""
                MATCH (uc:SysUseCase {id:$uid})-[r:REQUIRES]->(f:SysFeature {id:$fid})
                DELETE r RETURN count(r) AS n
            """, uid=args.id, fid=fid).single()
            if result and result["n"] > 0:
                removed.append(f"REQUIRES→{fid}")
            else:
                print(f"⚠ no REQUIRES edge from {args.id} to {fid}", file=sys.stderr)
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            result = s.run("""
                MATCH (us:SysUserStory {id:$sid})-[r:REALIZED_BY]->(uc:SysUseCase {id:$uid})
                DELETE r RETURN count(r) AS n
            """, sid=sid, uid=args.id).single()
            if result and result["n"] > 0:
                removed.append(f"{sid}→REALIZED_BY")
            else:
                print(f"⚠ no REALIZED_BY edge from {sid} to {args.id}", file=sys.stderr)
    drv.close()
    for r in removed:
        print(f"✓ removed  {args.id}  {r}")
    if not removed:
        print("Nothing removed — specify --feature and/or --story", file=sys.stderr)


def cmd_start_enhancement(args):
    """Mark one or more SysEnhancements as in-progress (signals to other sessions it is being built).

    Use --id for a single enhancement or --ids for a comma-separated batch.
    """
    # Batch mode via --ids
    ids_batch = getattr(args, "ids", "") or ""
    if ids_batch:
        ids = [x.strip() for x in ids_batch.split(",") if x.strip()]
        for eid in ids:
            args.id = eid
            args.ids = ""
            cmd_start_enhancement(args)
        return

    drv = _driver()
    with drv.session() as s:
        existing = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            RETURN e.status AS st, e.title AS title, e.instance AS inst,
                   e.startedAt AS startedAt,
                   coalesce(e.acceptanceCriteria, '') AS ac
        """, id=args.id).single()
        if not existing:
            print(f"⚠ {args.id} not found", file=sys.stderr)
            drv.close(); return
        if existing["st"] == "in-progress":
            started = str(existing.get("startedAt") or "")[:16]
            print(f"⚠  {args.id} is already in-progress (started {started}) — "
                  f"run abandon-enhancement first if you want to claim it", file=sys.stderr)
            drv.close(); return
        if existing["st"] == "done":
            print(f"⚠  {args.id} is already done — cannot restart", file=sys.stderr)
            drv.close(); return

        # Acceptance-criteria gate — "done" must be defined before code is touched
        skip_ac = getattr(args, "skip_ac_check", False) or getattr(args, "graph_only", False)
        if not skip_ac and not existing["ac"].strip():
            print(f"""
⛔  STOP — Acceptance criteria required before starting work on {args.id}.

   "Done" must be defined NOW, before any code is written, so the
   evidence required at close time is predetermined and cannot be
   cherry-picked after the fact.

   For a defect fix:
     Name the specific assertion or eval question that currently FAILS
     and must pass after the fix. If no such test exists, create it first.

   For a feature enhancement:
     Define the end-to-end scenario — inputs, expected outputs, and the
     specific integration test or eval question that validates the path.

   Set acceptance criteria, then re-run start-enhancement:
     sys_edge.py update-enhancement --id {args.id} \\
       --acceptance-criteria "test_delegation_create passes; \\
       assert response.status == 201 and delegate receives notification"

     sys_edge.py start-enhancement --id {args.id} --instance {getattr(args, 'instance', '<name>')}

   Graph-only / documentation enhancement with no test evidence needed?
     sys_edge.py start-enhancement --id {args.id} --skip-ac-check
""", file=sys.stderr)
            drv.close(); sys.exit(1)

        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='in-progress', e.startedAt=$now
            RETURN e.title AS title, e.instance AS inst
        """, id=args.id, now=_now_iso()).single()
    drv.close()
    if args.instance and result["inst"] and result["inst"] != args.instance:
        print(f"⚠  Instance mismatch: {args.id} belongs to '{result['inst']}'", file=sys.stderr)
    print(f"✓ {args.id} in-progress  —  {result['title']}")
    ac_display = existing["ac"].strip()
    if ac_display:
        print(f"\n  Acceptance criteria (defined before code was written):")
        for line in ac_display.splitlines()[:5]:
            print(f"    {line}")
        if len(ac_display.splitlines()) > 5:
            print(f"    … ({len(ac_display.splitlines())-5} more lines)")
    print(f"""
  ── Test discipline (required before close-enhancement) ──────────────
  1. Implement the change
  2. Verify the acceptance criteria above pass — not just any test
  3. Integration tests   — run for every changed feature
  4. UI flow tests       — run Playwright for all affected use cases
  5. Regression          — run the broader suite to catch side effects
  6. record-run          — after every test run (graph tracks history)
  7. audit-test --uc <id> — validate test quality against AS-TEST dimensions
  ─────────────────────────────────────────────────────────────────────""")


def cmd_wontfix_enhancement(args):
    """Mark an enhancement as wont-fix (closed, will not be implemented)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='wont-fix', e.closedAt=$now, e.wontFixReason=$reason
            RETURN e.title AS title
        """, id=args.id, now=_now_iso(),
             reason=getattr(args, "reason", "") or "").single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); sys.exit(1)
    print(f"✓ {args.id} wont-fix  —  {result['title']}")


def cmd_defer_enhancement(args):
    """Mark an enhancement as deferred (acknowledged, not in current scope)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='deferred', e.deferredAt=$now, e.deferReason=$reason
            RETURN e.title AS title
        """, id=args.id, now=_now_iso(),
             reason=getattr(args, "reason", "") or "").single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); sys.exit(1)
    print(f"✓ {args.id} deferred  —  {result['title']}")


def cmd_approve_enhancement(args):
    """Promote a proposed SysEnhancement to 'approved' state (ready to implement)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='approved', e.approvedAt=$now
            RETURN e.title AS title, e.instance AS inst, e.status AS prev_status
        """, id=args.id, now=_now_iso()).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.id} approved  —  {result['title']}")


def cmd_abandon_enhancement(args):
    """Return an in-progress SysEnhancement to 'proposed' state (release claim without closing)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            WHERE e.status = 'in-progress'
            SET e.status='proposed'
            REMOVE e.startedAt
            RETURN e.title AS title
        """, id=args.id).single()
    drv.close()
    if not result:
        drv = _driver()
        with drv.session() as s:
            row = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.status AS st",
                        id=args.id).single()
        drv.close()
        if not row:
            print(f"⚠ {args.id} not found", file=sys.stderr)
        else:
            print(f"⚠ {args.id} is '{row['st']}', not 'in-progress' — nothing changed", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.id} returned to proposed  —  {result['title']}")


def cmd_reopen_enhancement(args):
    """Return a done/approved SysEnhancement to 'proposed' state (recovery from accidental close)."""
    drv = _driver()
    with drv.session() as s:
        existing = s.run(
            "MATCH (e:SysEnhancement {id:$id}) RETURN e.status AS st, e.title AS title",
            id=args.id).single()
        if not existing:
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        if existing["st"] not in ("done", "approved", "in-progress"):
            print(f"⚠ {args.id} is '{existing['st']}' — only done/approved/in-progress can be reopened",
                  file=sys.stderr); drv.close(); sys.exit(1)
        s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='proposed'
            REMOVE e.startedAt, e.completedAt
        """, id=args.id)
    drv.close()
    print(f"✓ {args.id} reopened → proposed  —  {existing['title']}")
    print(f"  (run start-enhancement when ready to work on it)")


def cmd_show_enhancement(args):
    """Display full details of a SysEnhancement by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            RETURN e { .* } AS enode,
                   collect(DISTINCT {fid:f.id, fname:f.name, mid:m.id}) AS features
        """, id=args.id).single()
    drv.close()
    if not result:
        # Check if the ID belongs to a different node type (e.g. a UC ID passed by mistake)
        drv2 = _driver()
        with drv2.session() as s2:
            node_row = s2.run("MATCH (n {id:$id}) RETURN labels(n) AS lbls", id=args.id).single()
        drv2.close()
        if node_row:
            lbls = [l for l in (node_row["lbls"] or []) if l != "SysEnhancement"]
            suggestion = ""
            if "SysUseCase" in (node_row["lbls"] or []):
                suggestion = f"  Did you mean: show-usecase --id {args.id}"
            elif "SysDefect" in (node_row["lbls"] or []):
                suggestion = f"  Did you mean: show-defect --id {args.id}"
            elif "SysProposal" in (node_row["lbls"] or []):
                suggestion = f"  Did you mean: show-proposal --id {args.id}"
            print(f"⚠ {args.id} is not a SysEnhancement (found: {', '.join(lbls)})", file=sys.stderr)
            if suggestion:
                print(suggestion, file=sys.stderr)
        else:
            print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    e = result["enode"]
    feats = [f for f in result["features"] if f.get("fid")]
    print(f"\n{'═'*62}")
    print(f"  {e['id']}  [{e.get('instance','')}]  [{e.get('priority','?')}]  status: {e.get('status','?')}")
    print(f"  {e.get('title','')}")
    print(f"{'─'*62}")
    if e.get("description"):
        for line in e["description"].splitlines():
            print(f"  {line}")
    ac = (e.get("acceptanceCriteria") or "").strip()
    if ac:
        print(f"\n  Acceptance criteria:")
        for line in ac.splitlines():
            print(f"    {line}")
    else:
        print(f"\n  ⚠  Acceptance criteria: (none — set before starting work)")
        print(f"     update-enhancement --id {e['id']} --acceptance-criteria '...'")
    if e.get("source"):
        print(f"\n  Source   : {e['source']}")
    if feats:
        print(f"\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f.get('fname','')}  [{f.get('mid','')}]")
    if e.get("deployChecklist"):
        print(f"\n  Deploy checklist:")
        for line in e["deployChecklist"].splitlines():
            print(f"    {line}")
    for ts_field in ("createdAt","startedAt","completedAt"):
        if e.get(ts_field):
            print(f"  {ts_field:<12}: {str(e[ts_field])[:19]}")
    print(f"{'═'*62}")


def cmd_list_defects(args):
    """List defects for an instance, filtered by status."""
    instance = getattr(args, "instance", "") or ""
    status_filter = getattr(args, "status", "open") or "open"
    drv = _driver()
    with drv.session() as s:
        cypher = """
            MATCH (d:SysDefect)
            WHERE ($inst = '' OR d.instance = $inst)
              AND ($status = 'all' OR
                   ($status = 'open' AND d.status <> 'closed') OR
                   (d.status = $status))
            RETURN d.id AS id, d.title AS title, d.priority AS prio,
                   d.status AS status, d.instance AS inst
            ORDER BY
              CASE d.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                              WHEN 'medium' THEN 2 ELSE 3 END,
              d.id
        """
        rows = s.run(cypher, inst=instance, status=status_filter).data()
    drv.close()
    if not rows:
        print(f"  No defects found{' for ' + instance if instance else ''} "
              f"(status={status_filter})")
        return
    print(f"  {'ID':<12} {'Pri':<10} {'Status':<14} {'Instance':<16} Title")
    print(f"  {'-'*12} {'-'*10} {'-'*14} {'-'*16} {'-'*40}")
    for r in rows:
        print(f"  {r['id']:<12} {(r['prio'] or ''):<10} {(r['status'] or ''):<14} "
              f"{(r['inst'] or ''):<16} {r['title'] or ''}")
    print(f"\n  Total: {len(rows)}")


def cmd_list_enhancements(args):
    """List enhancements for an instance, filtered by status and priority."""
    instance = getattr(args, "instance", "") or ""
    status_filter = getattr(args, "status", "open") or "open"
    priority_filter = getattr(args, "priority", "") or ""
    drv = _driver()
    with drv.session() as s:
        cypher = """
            MATCH (e:SysEnhancement)
            WHERE ($inst = '' OR e.instance = $inst)
              AND ($status = 'all' OR
                   ($status = 'open' AND NOT (e.status IN ['done','wont-fix'])) OR
                   (e.status = $status))
              AND ($prio = '' OR e.priority = $prio)
            RETURN e.id AS id, e.title AS title, e.priority AS prio,
                   e.status AS status, e.instance AS inst
            ORDER BY
              CASE e.priority WHEN 'Must' THEN 0 WHEN 'Should' THEN 1
                              WHEN 'Could' THEN 2 ELSE 3 END,
              e.id
        """
        rows = s.run(cypher, inst=instance, status=status_filter,
                     prio=priority_filter).data()
    drv.close()
    if not rows:
        print(f"  No enhancements found{' for ' + instance if instance else ''} "
              f"(status={status_filter})")
        return
    print(f"  {'ID':<12} {'Pri':<10} {'Status':<14} {'Instance':<16} Title")
    print(f"  {'-'*12} {'-'*10} {'-'*14} {'-'*16} {'-'*40}")
    for r in rows:
        print(f"  {r['id']:<12} {(r['prio'] or ''):<10} {(r['status'] or ''):<14} "
              f"{(r['inst'] or ''):<16} {r['title'] or ''}")
    print(f"\n  Total: {len(rows)}")


def cmd_add_note(args):
    """Add a SysNote — instance memory for design context, decisions, and in-session reminders."""
    drv = _driver()
    with drv.session() as s:
        note_id = _alloc_id(s, "SysNote", "NOTE-")
        s.run("""
            MATCH (n:SysNote {id:$id})
            SET n.instance=$inst, n.body=$body,
                n.status='active', n.createdAt=$now,
                n.expiresAt=$expires
        """, id=note_id, inst=args.instance, body=args.body,
             now=_now_iso(), expires=getattr(args, "expires", "") or "")
    drv.close()
    print(f"✓ {note_id}  [{args.instance}]")
    print(f"  {args.body[:100]}{'…' if len(args.body) > 100 else ''}")


def cmd_show_notes(args):
    """Show active SysNote entries for an instance."""
    drv = _driver()
    with drv.session() as s:
        notes = s.run("""
            MATCH (n:SysNote {instance:$inst})
            WHERE n.status IN ['active', $status_filter]
            RETURN n.id AS nid, n.body AS body, n.status AS status,
                   n.createdAt AS created, n.expiresAt AS expires
            ORDER BY n.createdAt DESC
        """, inst=args.instance,
             status_filter="archived" if getattr(args, "all", False) else "active").data()
    drv.close()
    if not notes:
        print(f"No active notes for '{args.instance}'.")
        return
    print(f"\n{'═'*62}")
    print(f"  NOTES — {args.instance}  ({len(notes)} entries)")
    print(f"{'═'*62}")
    for n in notes:
        status_tag = f"  [{n['status']}]" if n["status"] != "active" else ""
        expires_tag = f"  expires {n['expires'][:10]}" if n.get("expires") else ""
        print(f"\n  {n['nid']}{status_tag}{expires_tag}")
        print(f"  Created: {str(n['created'])[:19]}")
        for line in (n["body"] or "").splitlines():
            print(f"  {line}")
    print(f"{'═'*62}")


def cmd_expire_note(args):
    """Mark a SysNote as expired (removes it from worklog without deleting)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (n:SysNote {id:$id})
            SET n.status='expired', n.expiredAt=$now
            RETURN n.instance AS inst, n.body AS body
        """, id=args.id, now=_now_iso()).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); return
    print(f"✓ {args.id} expired  [{result['inst']}]")


def cmd_show_defect(args):
    """Display full details of a SysDefect by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (d:SysDefect {id:$id})
            OPTIONAL MATCH (d)-[:AFFECTS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            RETURN d { .* } AS dnode,
                   collect(DISTINCT {fid:f.id, fname:f.name, mid:m.id}) AS features
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    d = result["dnode"]
    feats = [f for f in result["features"] if f.get("fid")]
    sev = d.get("severity", "medium")
    sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}.get(sev,"●●○○")
    print(f"\n{'═'*62}")
    print(f"  {d['id']}  [{sev_mark} {sev}]  status: {d.get('status','?')}")
    print(f"  {d.get('title','')}")
    print(f"{'─'*62}")
    if d.get("instance"):
        print(f"  Instance : {d['instance']}")
    if d.get("source"):
        print(f"  Source   : {d['source']}")
    if d.get("description"):
        print(f"\n  {d['description']}")
    ac = (d.get("acceptanceCriteria") or "").strip()
    if ac:
        print(f"\n  Acceptance criteria:")
        for line in ac.splitlines():
            print(f"    {line}")
    else:
        print(f"\n  ⚠  Acceptance criteria: (none — name the assertion that currently fails)")
        print(f"     update-defect --id {d['id']} --acceptance-criteria '...'")
    if feats:
        print(f"\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f.get('fname','')}  [{f.get('mid','')}]")
    for ts_field in ("createdAt","closedAt","lastSeen"):
        if d.get(ts_field):
            print(f"  {ts_field:<12}: {str(d[ts_field])[:19]}")
    if d.get("occurrences") and int(d["occurrences"]) > 1:
        print(f"  occurrences: {d['occurrences']}")
    print(f"{'═'*62}")


def cmd_show_feature(args):
    """Display full details of a SysFeature by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (f:SysFeature {id:$id})
            OPTIONAL MATCH (m:SysModule)-[:PROVIDES]->(f)
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            OPTIONAL MATCH (uc:SysUseCase)-[:REQUIRES]->(f)
            OPTIONAL MATCH (d:SysDefect)-[:AFFECTS]->(f) WHERE (d.status <> 'closed' AND d.closedAt IS NULL)
            OPTIONAL MATCH (e:SysEnhancement)-[:EXTENDS]->(f) WHERE e.status <> 'done'
            RETURN f { .* } AS fnode, m.id AS mid, m.name AS mname,
                   collect(DISTINCT {tid:t.id, ttype:t.testType}) AS tests,
                   collect(DISTINCT {uid:uc.id, utitle:uc.title}) AS usecases,
                   collect(DISTINCT {did:d.id, dtitle:d.title, dsev:d.severity}) AS defects,
                   collect(DISTINCT {eid:e.id, etitle:e.title}) AS enhs
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    f = result["fnode"]
    print(f"\n{'═'*66}")
    print(f"  {f['id']}  [{f.get('status','?')}]  [{f.get('layer','')}]")
    print(f"  {f.get('name','')}")
    print(f"{'─'*66}")
    if result["mid"]:
        print(f"  Module  : {result['mid']}  {result.get('mname','')}")
    if f.get("description"):
        print(f"\n  {f['description']}")
    tests = [t for t in result["tests"] if t.get("tid")]
    if tests:
        print(f"\n  Tests ({len(tests)}):")
        for t in tests[:8]:
            print(f"    {t['tid']}  [{t.get('ttype','')}]")
        if len(tests) > 8:
            print(f"    … and {len(tests)-8} more")
    ucs = [u for u in result["usecases"] if u.get("uid")]
    if ucs:
        print(f"\n  Use cases ({len(ucs)}):")
        for u in ucs:
            print(f"    {u['uid']}  {(u.get('utitle') or '')[:50]}")
    defs = [d for d in result["defects"] if d.get("did")]
    if defs:
        print(f"\n  Open defects ({len(defs)}):")
        for d in defs:
            sev = d.get("dsev","?")
            print(f"    {d['did']}  [{sev}]  {(d.get('dtitle') or '')[:50]}")
    enhs = [e for e in result["enhs"] if e.get("eid")]
    if enhs:
        print(f"\n  Open enhancements ({len(enhs)}):")
        for e in enhs:
            print(f"    {e['eid']}  {(e.get('etitle') or '')[:50]}")
    print(f"{'═'*66}")


def cmd_show_story(args):
    """Display full details of a SysUserStory by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (us:SysUserStory {id:$id})
            OPTIONAL MATCH (us)-[:REALIZED_BY]->(uc:SysUseCase)
            OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
            RETURN us { .* } AS usnode,
                   collect(DISTINCT {uid:uc.id, utitle:uc.title}) AS usecases,
                   collect(DISTINCT f.id) AS feat_ids
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    us = result["usnode"]
    print(f"\n{'═'*66}")
    print(f"  {us['id']}  [{us.get('instance','')}]")
    print(f"  {us.get('title','')}")
    print(f"{'─'*66}")
    if us.get("goal"):
        print(f"\n  Goal    : {us['goal']}")
    if us.get("benefit"):
        print(f"  Benefit : {us['benefit']}")
    if us.get("narrative"):
        print(f"\n  Narrative:\n  {us['narrative']}")
    if us.get("acceptanceCriteria"):
        print(f"\n  Acceptance criteria:\n  {us['acceptanceCriteria']}")
    if us.get("outOfScope"):
        print(f"\n  Out of scope:\n  {us['outOfScope']}")
    ucs = [u for u in result["usecases"] if u.get("uid")]
    if ucs:
        print(f"\n  Use cases:")
        for u in ucs:
            print(f"    {u['uid']}  {(u.get('utitle') or '')[:50]}")
    if result["feat_ids"]:
        print(f"\n  Features (via UCs): {', '.join(result['feat_ids'])}")
    print(f"{'═'*66}")


def cmd_update_defect(args):
    """Patch title, description, severity, or acceptance-criteria on a SysDefect node."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("MATCH (d:SysDefect {id:$id}) RETURN d.id AS id", id=args.id).single()
        if not result:
            print(f"⚠ {args.id} not found", file=sys.stderr)
            drv.close(); return
        if args.title:
            s.run("MATCH (d:SysDefect {id:$id}) SET d.title=$v", id=args.id, v=args.title)
        if args.description:
            s.run("MATCH (d:SysDefect {id:$id}) SET d.description=$v", id=args.id, v=args.description)
        if args.severity:
            s.run("MATCH (d:SysDefect {id:$id}) SET d.severity=$v", id=args.id, v=args.severity)
        if getattr(args, "acceptance_criteria", ""):
            s.run("MATCH (d:SysDefect {id:$id}) SET d.acceptanceCriteria=$v",
                  id=args.id, v=args.acceptance_criteria)
    drv.close()
    parts = [p for p in [
        args.title and "title",
        args.description and "description",
        args.severity and "severity",
        getattr(args, "acceptance_criteria", "") and "acceptance-criteria",
    ] if p]
    print(f"✓ {args.id} updated  ({', '.join(parts) or 'no changes'})")


def cmd_show_feature_tests(args):
    """Show all tests already linked to a feature, and tests in the same module area."""
    drv = _driver()
    with drv.session() as s:
        # Tests already VERIFIES this feature
        linked = s.run("""
            MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature {id:$fid})
            OPTIONAL MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t)
            RETURN t.id AS id, t.testType AS tier,
                   pkg.id AS pkg, pkg.testCategory AS cat
            ORDER BY t.id
        """, fid=args.feature).data()

        # Module area for this feature — find related packages
        mod_row = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature {id:$fid})
            RETURN m.id AS mid, coalesce(m.testPaths, m.paths, []) AS paths
        """, fid=args.feature).single()

        W = 62
        print(f"\n{'─'*W}")
        print(f"  Tests linked to {args.feature}  ({len(linked)} total)")
        print(f"{'─'*W}")
        if linked:
            for t in linked:
                tier = f"[{t['cat'] or t['tier'] or '?'}]"
                print(f"  {tier:<16} {t['id']}")
        else:
            print(f"  (none linked yet — use link-feature to add)")

        if mod_row and mod_row["paths"]:
            print(f"\n  Module: {mod_row['mid']}  paths: {', '.join(mod_row['paths'][:2])}")
            # Suggest unlinked tests in same package area
            area_tests = s.run("""
                MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t:SysTest)
                WHERE any(p IN $paths WHERE pkg.path CONTAINS p OR p CONTAINS pkg.area)
                  AND NOT (t)-[:VERIFIES]->(:SysFeature {id:$fid})
                RETURN t.id AS id, pkg.testCategory AS cat
                ORDER BY t.id LIMIT 15
            """, paths=mod_row["paths"], fid=args.feature).data()
            if area_tests:
                print(f"\n  Tests in same module area (not yet linked):")
                for t in area_tests:
                    print(f"  [{t['cat'] or '?':12}] {t['id']}")
    drv.close()


def cmd_link_story(args):
    """Add REQUIRES edges from a SysUserStory to one or more SysFeature nodes."""
    drv = _driver()
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    linked = []
    with drv.session() as s:
        if not s.run("MATCH (us:SysUserStory {id:$id}) RETURN us.id", id=args.story).single():
            print(f"ERROR: story '{args.story}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        for fid in features:
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"⚠ feature '{fid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (us:SysUserStory {id:$sid}),(f:SysFeature {id:$fid})
                     MERGE (us)-[:REQUIRES]->(f)""", sid=args.story, fid=fid)
            linked.append(fid)
    drv.close()
    for fid in linked:
        print(f"✓ {args.story}  →  REQUIRES  →  {fid}")
    if not linked:
        print("No features linked.", file=sys.stderr)


def cmd_link_blocks(args):
    """Mark an enhancement as BLOCKED_BY another enhancement, or a defect as BLOCKS an enhancement."""
    if args.id == args.blocked_by:
        print(f"⚠ self-block rejected — {args.id} cannot be BLOCKED_BY itself", file=sys.stderr)
        return
    drv = _driver()
    with drv.session() as s:
        if args.id.startswith("DEF-"):
            # DEF-[:BLOCKS]->ENH (required for close-enhancement --force gate)
            src = s.run("MATCH (d:SysDefect {id:$id}) RETURN d.title AS t", id=args.id).single()
            if not src:
                print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); return
            tgt = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.title AS t, e.status AS st",
                        id=args.blocked_by).single()
            if not tgt:
                print(f"⚠ {args.blocked_by} not found", file=sys.stderr); drv.close(); return
            s.run("""
                MATCH (d:SysDefect {id:$did}), (e:SysEnhancement {id:$eid})
                MERGE (d)-[:BLOCKS]->(e)
            """, did=args.id, eid=args.blocked_by)
            print(f"✓ {args.id}  BLOCKS  {args.blocked_by}  ({tgt['t'][:50]}  [{tgt['st']}])")
        else:
            blocker = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.title AS t, e.status AS st",
                            id=args.blocked_by).single()
            if not blocker:
                print(f"⚠ blocker {args.blocked_by} not found", file=sys.stderr)
                drv.close(); return
            if not s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=args.id).single():
                print(f"⚠ {args.id} not found", file=sys.stderr)
                drv.close(); return
            s.run("""
                MATCH (e:SysEnhancement {id:$id}), (b:SysEnhancement {id:$bid})
                MERGE (e)-[:BLOCKED_BY]->(b)
            """, id=args.id, bid=args.blocked_by)
            print(f"✓ {args.id}  BLOCKED_BY  {args.blocked_by}  ({blocker['t'][:50]}  [{blocker['st']}])")
    drv.close()


def cmd_unlink_blocks(args):
    """Remove BLOCKED_BY edge(s) from an enhancement.

    Removes ENH --id BLOCKED_BY each id in --unblock (comma-separated).
    Use when a dependency was filed in error.
    """
    unblock_ids = [x.strip() for x in (args.unblock or "").split(",") if x.strip()]
    if not unblock_ids:
        print("ERROR: --unblock requires at least one ENH id", file=sys.stderr); sys.exit(1)
    drv = _driver()
    with drv.session() as s:
        is_def = args.id.startswith("DEF-")
        label = "SysDefect" if is_def else "SysEnhancement"
        if not s.run(f"MATCH (n:{label} {{id:$id}}) RETURN n.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        for bid in unblock_ids:
            if is_def:
                result = s.run("""
                    MATCH (d:SysDefect {id:$id})-[r:BLOCKS]->(e:SysEnhancement {id:$bid})
                    DELETE r RETURN count(r) AS n
                """, id=args.id, bid=bid).single()
                rel_desc = "BLOCKS"
            else:
                result = s.run("""
                    MATCH (e:SysEnhancement {id:$id})-[r:BLOCKED_BY]->(b:SysEnhancement {id:$bid})
                    DELETE r RETURN count(r) AS n
                """, id=args.id, bid=bid).single()
                rel_desc = "BLOCKED_BY"
            if result and result["n"] > 0:
                print(f"✓ removed  {args.id}  {rel_desc}  {bid}")
            else:
                print(f"⚠ no {rel_desc} edge found between {args.id} and {bid}", file=sys.stderr)
    drv.close()


def cmd_retire_feature(args):
    """Mark a SysFeature as Superseded so it is excluded from coverage gap reports."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (f:SysFeature {id:$id})
            SET f.status = 'Superseded', f.retiredAt = $now, f.retiredReason = $reason
            RETURN f.name AS name
        """, id=args.id, now=_now_iso(), reason=args.reason or "").single()
    drv.close()
    if result:
        print(f"✓ {args.id} retired (Superseded) — {result['name']}")
        print(f"  Feature will no longer appear in coverage gap reports.")
    else:
        print(f"⚠ {args.id} not found", file=sys.stderr)


def cmd_coverage_review(args):
    """AI-powered semantic sufficiency check on US→UC and UC→Feature chains.

    Prompts Claude to review whether traceability chains are complete, not just
    structurally present. Outputs PASS/PARTIAL/GAP per item with one-line reasoning.
    Use --file-proposals to create SysProposal nodes for every GAP found.

    Flags:
      --scope us|uc|both       which chain to review (default: both)
      --instance <name>        scope to one instance's modules/UCs
      --story US-xxx           single story (implies --scope us)
      --module MOD-xxx         single module
      --model haiku|sonnet     model to use (default: haiku)
      --file-proposals         create SysProposal nodes for GAP findings
    """
    _require_licence("coverage-review")
    scope          = getattr(args, "scope",          "both")   or "both"
    instance       = getattr(args, "instance",       "")       or ""
    story_filter   = getattr(args, "story",          "")       or ""
    module_filter  = getattr(args, "module",         "")       or ""
    model_name     = getattr(args, "model",          "haiku")  or "haiku"
    provider_name  = getattr(args, "provider",       "")       or ""
    file_proposals = getattr(args, "file_proposals", False)
    us_single      = getattr(args, "us",             "")       or ""
    uc_single      = getattr(args, "uc",             "")       or ""
    analyse        = getattr(args, "analyse",        False)
    output_request = getattr(args, "output_request", "")       or ""
    apply_results  = getattr(args, "apply_results",  "")       or ""
    batch_size     = getattr(args, "batch_size",     1)        or 1

    # ── Apply assistant-produced results ──────────────────────────────────────
    if apply_results:
        _apply_coverage_review_results(apply_results, file_proposals=file_proposals)
        return

    W = 66
    verdict_mark = {"PASS": "✓", "PARTIAL": "~", "GAP": "✗", "INSUFFICIENT": "?"}
    gap_count = partial_count = pass_count = insufficient_count = 0
    proposals_filed: list[str] = []

    drv = _driver()
    with drv.session() as s:

        # ── AS-REQ batch mode: --instance runs all stories owned by that instance ──
        if instance and not (us_single or uc_single) and not story_filter:
            story_ids = [r["sid"] for r in s.run("""
                MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc:SysUseCase {instance:$inst})
                RETURN DISTINCT us.id AS sid ORDER BY us.id
            """, inst=instance).data()]
            if not story_ids:
                print(f"No user stories found for instance '{instance}'.")
                drv.close()
                return

            if analyse:
                subjects = [_build_coverage_review_subject(s, sid, "") for sid in story_ids]
                subjects = [sub for sub in subjects if sub]
                _emit_coverage_review_request(args, subjects, _fetch_as_req_standards(s))
                drv.close()
                return

            print(f"\n{'═' * 66}")
            print(f"  COVERAGE REVIEW — {instance}  ({len(story_ids)} stories)  [AS-REQ batch]")
            print(f"{'═' * 66}\n")
            fail_count = 0
            for sid in story_ids:
                try:
                    _coverage_review_as_req(s, model_name, sid, "", provider_name)
                except SystemExit as e:
                    if e.code != 0:
                        fail_count += 1
            print(f"\n{'═' * 66}")
            print(f"  Batch complete: {len(story_ids)} stories  ·  {fail_count} with FAILs")
            print(f"{'═' * 66}\n")
            drv.close()
            if fail_count:
                sys.exit(1)
            return

        # ── AS-REQ single-item mode (--us / --uc) ─────────────────────────────
        if us_single or uc_single:
            if analyse:
                subject = _build_coverage_review_subject(s, us_single, uc_single)
                if subject:
                    _emit_coverage_review_request(args, [subject], _fetch_as_req_standards(s))
                drv.close()
                return
            _coverage_review_as_req(s, model_name, us_single, uc_single, provider_name)
            drv.close()
            return

        # ── Resolve scope → module IDs ────────────────────────────────────────
        mod_ids: list[str] = []
        if instance:
            mod_ids = [r["id"] for r in s.run(
                "MATCH (m:SysModule {instance:$inst}) RETURN m.id AS id", inst=instance
            ).data()]
        elif module_filter:
            mod_ids = [module_filter]

        # ── Level 1: US → UC sufficiency ─────────────────────────────────────
        if scope in ("us", "both"):
            print(f"\n{'═' * W}")
            print(f"  LEVEL 1 — User Story → Use Case sufficiency")
            print(f"  Model: {model_name}  |  Scope: {instance or module_filter or 'all'}")
            print(f"{'═' * W}")

            # Fetch stories in scope
            if story_filter:
                story_q = "MATCH (us:SysUserStory {id:$sid})"
                story_params: dict = {"sid": story_filter}
            elif mod_ids:
                story_q = """
                    MATCH (us:SysUserStory)-[:REQUIRES]->(:SysFeature)<-[:PROVIDES]-(m:SysModule)
                    WHERE m.id IN $mids
                """
                story_params = {"mids": mod_ids}
            else:
                story_q = "MATCH (us:SysUserStory)"
                story_params = {}

            stories = s.run(f"""
                {story_q}
                OPTIONAL MATCH (us)-[:REALIZED_BY]->(uc:SysUseCase)
                RETURN us.id AS sid, us.title AS stitle,
                       us.actor AS actor, us.goal AS goal,
                       collect(DISTINCT {{id: uc.id, title: uc.title, desc: uc.description}}) AS ucs
                ORDER BY us.id
            """, **story_params).data()

            if not stories:
                print("  No user stories found in scope.")
            else:
                print(f"  Reviewing {len(stories)} user stor{'y' if len(stories)==1 else 'ies'}…\n")

            for row in stories:
                sid   = row["sid"]
                actor = row.get("actor") or "User"
                goal  = row.get("goal")  or row.get("stitle") or ""
                ucs   = [uc for uc in (row.get("ucs") or []) if uc.get("id")]

                if not ucs:
                    print(f"  ✗ {sid}  (no use cases linked — skip AI review)")
                    gap_count += 1
                    continue

                uc_lines = "\n".join(
                    f"  - [{uc['id']}] {uc['title']}"
                    + (f"\n    {uc['desc']}" if uc.get("desc") else "")
                    for uc in ucs
                )
                prompt = (
                    f"You are reviewing requirements traceability. "
                    f"Actor: '{actor}'. Goal: '{goal}'.\n\n"
                    f"These use cases are linked to cover this goal:\n{uc_lines}\n\n"
                    f"Do these use cases together fully cover all meaningful user paths for this actor/goal? "
                    f"Respond with exactly one line in this format:\n"
                    f"VERDICT: PASS|PARTIAL|GAP|INSUFFICIENT — one sentence reason. "
                    f"Use INSUFFICIENT if the actor/goal description is too vague to assess coverage meaningfully. "
                    f"If PARTIAL or GAP, add: MISSING: brief description of what is absent."
                )

                reply = _ai_call(prompt, model_name, max_tokens=256, provider=provider_name)

                verdict = "GAP"
                for v in ("PASS", "PARTIAL", "INSUFFICIENT", "GAP"):
                    if v in reply.upper():
                        verdict = v; break

                mark = verdict_mark.get(verdict, "?")
                print(f"  {mark} {sid}  [{verdict}]  {row.get('stitle','')[:55]}")
                reason_line = reply.split("—", 1)[-1].strip() if "—" in reply else reply
                print(f"    {reason_line[:110]}")

                if verdict == "PASS":
                    pass_count += 1
                elif verdict == "PARTIAL":
                    partial_count += 1
                elif verdict == "INSUFFICIENT":
                    insufficient_count += 1
                else:
                    gap_count += 1
                    if file_proposals:
                        missing = ""
                        if "MISSING:" in reply:
                            missing = reply.split("MISSING:", 1)[1].strip()
                        prop_title = f"Coverage gap: {row.get('stitle','')[:50]}"
                        prop_desc  = (
                            f"AI review of {sid} ({actor} / {goal}) found missing coverage.\n"
                            f"Existing UCs: {', '.join(uc['id'] for uc in ucs)}\n"
                            f"Missing: {missing or reason_line}\n"
                            f"Review model: {model_name}"
                        )
                        prop_id = _alloc_id(s, "SysProposal", "PROP-")
                        s.run("""
                            MATCH (p:SysProposal {id:$id})
                            SET p.title=$title, p.description=$desc,
                                p.instance=$inst, p.priority='Should',
                                p.status='draft', p.createdAt=$now,
                                p.source=$src
                        """, id=prop_id, title=prop_title, desc=prop_desc,
                             inst=instance or "architect", now=_now_iso(), src=sid)
                        proposals_filed.append(f"{prop_id} ← {sid}")
                        print(f"    → filed {prop_id}")

        # ── Level 2: UC → Feature sufficiency ────────────────────────────────
        if scope in ("uc", "both"):
            print(f"\n{'═' * W}")
            print(f"  LEVEL 2 — Use Case → Feature sufficiency")
            print(f"  Model: {model_name}  |  Scope: {instance or module_filter or 'all'}")
            print(f"{'═' * W}")

            if mod_ids:
                uc_q = """
                    MATCH (uc:SysUseCase)-[:REQUIRES]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
                    WHERE m.id IN $mids
                    WITH uc, collect(DISTINCT {id:f.id, name:f.name, desc:f.description}) AS feats
                    RETURN uc.id AS uid, uc.title AS utitle,
                           uc.description AS udesc,
                           uc.mainFlow AS mainflow,
                           uc.preconditions AS precond,
                           feats
                    ORDER BY uc.id
                """
                uc_params: dict = {"mids": mod_ids}
            elif instance:
                uc_q = """
                    MATCH (uc:SysUseCase {instance:$inst})
                    OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
                    WITH uc, collect(DISTINCT {id:f.id, name:f.name, desc:f.description}) AS feats
                    RETURN uc.id AS uid, uc.title AS utitle,
                           uc.description AS udesc,
                           uc.mainFlow AS mainflow,
                           uc.preconditions AS precond,
                           feats
                    ORDER BY uc.id
                """
                uc_params = {"inst": instance}
            else:
                uc_q = """
                    MATCH (uc:SysUseCase)
                    OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
                    WITH uc, collect(DISTINCT {id:f.id, name:f.name, desc:f.description}) AS feats
                    RETURN uc.id AS uid, uc.title AS utitle,
                           uc.description AS udesc,
                           uc.mainFlow AS mainflow,
                           uc.preconditions AS precond,
                           feats
                    ORDER BY uc.id
                """
                uc_params = {}

            uc_rows = s.run(uc_q, **uc_params).data()

            if not uc_rows:
                print("  No use cases found in scope.")
            else:
                print(f"  Reviewing {len(uc_rows)} use case{'s' if len(uc_rows)!=1 else ''}…\n")

            for row in uc_rows:
                uid      = row["uid"]
                utitle   = row.get("utitle") or uid
                udesc    = (row.get("udesc") or row.get("mainflow") or "")
                precond  = row.get("precond") or ""
                features = [f for f in (row.get("feats") or []) if f.get("id")]

                if not features:
                    print(f"  ✗ {uid}  (no features linked — skip AI review)")
                    gap_count += 1
                    continue

                feat_lines = "\n".join(
                    f"  - [{f['id']}] {f['name']}"
                    + (f": {f['desc'][:100]}" if f.get("desc") else "")
                    for f in features
                )
                prompt = (
                    f"You are reviewing requirements traceability. "
                    f"Use case: '{utitle}'.\n"
                    + (f"Preconditions: {precond}\n" if precond else "")
                    + (f"Description: {udesc}\n\n" if udesc else "\n")
                    + f"These features are linked to implement this use case:\n{feat_lines}\n\n"
                    f"Do these features together fully implement the use case? "
                    f"Respond with exactly one line in this format:\n"
                    f"VERDICT: PASS|PARTIAL|GAP|INSUFFICIENT — one sentence reason. "
                    f"Use INSUFFICIENT if the use case description is too sparse to assess whether the features cover it. "
                    f"If PARTIAL or GAP, add: MISSING: brief description of what functionality is absent."
                )

                reply = _ai_call(prompt, model_name, max_tokens=256, provider=provider_name)

                verdict = "GAP"
                for v in ("PASS", "PARTIAL", "INSUFFICIENT", "GAP"):
                    if v in reply.upper():
                        verdict = v; break

                mark = verdict_mark.get(verdict, "?")
                print(f"  {mark} {uid}  [{verdict}]  {utitle[:55]}")
                reason_line = reply.split("—", 1)[-1].strip() if "—" in reply else reply
                print(f"    {reason_line[:110]}")

                if verdict == "PASS":
                    pass_count += 1
                elif verdict == "PARTIAL":
                    partial_count += 1
                elif verdict == "INSUFFICIENT":
                    insufficient_count += 1
                else:
                    gap_count += 1
                    if file_proposals:
                        missing = ""
                        if "MISSING:" in reply:
                            missing = reply.split("MISSING:", 1)[1].strip()
                        prop_title = f"Coverage gap: {utitle[:50]}"
                        prop_desc  = (
                            f"AI review of {uid} ('{utitle}') found missing feature coverage.\n"
                            f"Existing features: {', '.join(f['id'] for f in features)}\n"
                            f"Missing: {missing or reason_line}\n"
                            f"Review model: {model_name}"
                        )
                        prop_id = _alloc_id(s, "SysProposal", "PROP-")
                        s.run("""
                            MATCH (p:SysProposal {id:$id})
                            SET p.title=$title, p.description=$desc,
                                p.instance=$inst, p.priority='Should',
                                p.status='draft', p.createdAt=$now,
                                p.source=$src
                        """, id=prop_id, title=prop_title, desc=prop_desc,
                             inst=instance or "architect", now=_now_iso(), src=uid)
                        proposals_filed.append(f"{prop_id} ← {uid}")
                        print(f"    → filed {prop_id}")

    drv.close()

    total = pass_count + partial_count + gap_count
    print(f"\n{'═' * W}")
    print(f"  SUMMARY  —  {total} reviewed")
    print(f"  ✓ PASS         {pass_count:4d}")
    print(f"  ~ PARTIAL      {partial_count:4d}")
    print(f"  ✗ GAP          {gap_count:4d}")
    print(f"  ? INSUFFICIENT {insufficient_count:4d}")
    if proposals_filed:
        print(f"\n  Proposals filed ({len(proposals_filed)}):")
        for p in proposals_filed:
            print(f"    {p}")
    print(f"{'═' * W}\n")


def _fetch_as_req_standards(session):
    """Return all AS-REQ architecture standards from the graph."""
    return session.run(
        "MATCH (a:SysArchStd) WHERE a.id STARTS WITH 'AS-REQ-' "
        "RETURN a.id AS id, a.title AS title, a.description AS desc ORDER BY a.id"
    ).data()


def _build_coverage_review_subject(session, us_id: str, uc_id: str) -> dict | None:
    """Gather context for a single user story or use case AS-REQ review."""
    if us_id:
        row = session.run(
            "MATCH (us:SysUserStory {id:$id}) "
            "OPTIONAL MATCH (us)-[:REALIZED_BY]->(uc:SysUseCase) "
            "RETURN us.id AS id, us.title AS title, us.actor AS actor, "
            "       us.goal AS goal, us.benefit AS benefit, "
            "       us.acceptanceCriteria AS ac, us.outOfScope AS oos, "
            "       collect(DISTINCT {id: uc.id, title: uc.title, desc: uc.description, "
            "                         precond: uc.preconditions, mainflow: uc.mainFlow, "
            "                         authRoles: uc.authorizedRoles, "
            "                         failScenarios: uc.failureScenarios}) AS ucs",
            id=us_id
        ).single()
        if not row:
            return None
        uc_rows = [u for u in (row["ucs"] or []) if u.get("id")]
        if uc_rows:
            uc_ids_for_count = [u["id"] for u in uc_rows]
            test_counts = {
                r["uid"]: {"count": r["tc"], "last_run": str(r.get("lr") or "")[:10]}
                for r in session.run("""
                    UNWIND $uids AS uid
                    MATCH (uc:SysUseCase {id: uid})
                    OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)<-[:VERIFIES]-(t:SysTest)
                    OPTIONAL MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t)
                    RETURN uid, count(DISTINCT t) AS tc, max(pkg.lastRun) AS lr
                """, uids=uc_ids_for_count).data()
            }
            for u in uc_rows:
                tc = test_counts.get(u["id"], {})
                u["testCount"] = tc.get("count", 0)
                u["lastRun"] = tc.get("last_run", "")

        context_lines = [
            f"User Story: {row['title']} ({us_id})",
            f"Actor: {row.get('actor') or 'User'}",
            f"Goal: {row.get('goal') or ''}",
        ]
        if row.get("benefit"):
            context_lines.append(f"So that: {row['benefit']}")
        if row.get("ac"):
            context_lines.append(f"Acceptance criteria: {row['ac']}")
        if row.get("oos"):
            context_lines.append(f"Out of scope: {row['oos']}")
        if uc_rows:
            uc_summary = "\n".join(
                f"  [{u['id']}] {u['title']}"
                + (f"\n    Tests via UC→Feature→Test: {u.get('testCount', 0)}"
                   + (f" (last run: {u['lastRun']})" if u.get("lastRun") else " (none run)"))
                + (f"\n    Authorized roles: {u['authRoles']}" if u.get("authRoles") else "")
                + (f"\n    Failure scenarios: {u['failScenarios']}" if u.get("failScenarios") else "")
                + (f"\n    Preconditions: {u['precond']}" if u.get("precond") else "")
                + (f"\n    Main flow: {u['mainflow']}" if u.get("mainflow") else "")
                + (f"\n    {u['desc']}" if u.get("desc") else "")
                for u in uc_rows
            )
            context_lines.append(f"\nRealising use cases:\n{uc_summary}")

        linked_ucs = []
        for u in uc_rows:
            linked_ucs.append({
                "id": u["id"],
                "title": u.get("title", ""),
                "description": u.get("desc", ""),
                "testCount": u.get("testCount", 0),
                "lastRun": u.get("lastRun", ""),
            })
        return {
            "subjectId": us_id,
            "subjectType": "SysUserStory",
            "title": row.get("title", ""),
            "context": "\n".join(context_lines),
            "linkedUseCases": linked_ucs,
        }

    if uc_id:
        row = session.run(
            "MATCH (uc:SysUseCase {id:$id}) "
            "OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc) "
            "OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature) "
            "OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f) "
            "OPTIONAL MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t) "
            "RETURN uc.id AS id, uc.title AS title, uc.description AS desc, "
            "       uc.preconditions AS precond, uc.mainFlow AS mainflow, "
            "       uc.authorizedRoles AS auth_roles, "
            "       uc.failureScenarios AS fail_scenarios, "
            "       uc.instance AS instance, "
            "       collect(DISTINCT us.id) AS story_ids, "
            "       collect(DISTINCT f.id) AS feat_ids, "
            "       count(DISTINCT t) AS test_count, "
            "       max(pkg.lastRun) AS last_run",
            id=uc_id
        ).single()
        if not row:
            return None
        context_lines = [
            f"Use Case: {row['title']} ({uc_id})",
            f"Instance: {row.get('instance') or 'unknown'}",
            f"Parent stories: {', '.join(row['story_ids'] or []) or '(none)'}",
            f"Features: {', '.join(row['feat_ids'] or []) or '(none)'}",
            f"Tests linked via UC→Feature→Test path: {row.get('test_count', 0)}"
            + (f" (last run: {str(row['last_run'])[:10]})" if row.get('last_run') else " (none run)"),
        ]
        if row.get("precond"):
            context_lines.append(f"Preconditions: {row['precond']}")
        if row.get("mainflow"):
            context_lines.append(f"Main flow: {row['mainflow']}")
        if row.get("auth_roles"):
            context_lines.append(f"Authorized roles: {row['auth_roles']}")
        if row.get("fail_scenarios"):
            context_lines.append(f"Failure scenarios: {row['fail_scenarios']}")
        if row.get("desc"):
            context_lines.append(f"\nDescription:\n{row['desc']}")
        return {
            "subjectId": uc_id,
            "subjectType": "SysUseCase",
            "title": row.get("title", ""),
            "context": "\n".join(context_lines),
            "linkedFeatures": [f for f in (row["feat_ids"] or []) if f],
            "testCount": row.get("test_count", 0),
            "lastRun": str(row.get("last_run") or "")[:10],
        }

    return None


def _emit_coverage_review_request(args, subjects: list[dict], standards: list[dict]):
    """Emit a JSON request for the assistant to run an AS-REQ coverage review.

    Supports batching multiple subjects into one request via --batch-size.
    """
    if not subjects:
        print("  No subjects found — nothing to review.")
        return

    batch_size = getattr(args, "batch_size", 1) or 1
    output_path = getattr(args, "output_request", "") or ""
    groups = [subjects[i:i + batch_size] for i in range(0, len(subjects), batch_size)]

    response_format = {
        "subjects": [
            {
                "subjectId": "<US-xxx or UC-xxx>",
                "subjectType": "<SysUserStory|SysUseCase>",
                "overall": "PASS|WARN|FAIL",
                "dimensions": [
                    {
                        "id": "<AS-REQ-NNN>",
                        "status": "PASS|WARN|FAIL",
                        "finding": "<specific sentence>",
                        "recommendation": "<concrete action if WARN/FAIL, else empty>"
                    }
                ]
            }
        ]
    }

    std_block = "\n\n".join(
        f"### {std['id']} — {std['title']}\n{std['desc']}"
        for std in standards
    )

    for idx, group in enumerate(groups):
        request = {
            "schemaVersion": 2,
            "command": "coverage-review",
            "instruction": (
                "Review each subject against the AS-REQ architecture standards below. "
                "Return one overall PASS/WARN/FAIL per subject and one dimension row per standard."
            ),
            "responseFormat": response_format,
            "rules": [
                "PASS: fully satisfied.",
                "WARN: partially satisfied — present but incomplete or ambiguous.",
                "FAIL: clearly absent or clearly wrong.",
                "finding must be specific (name actual UCs, missing scenarios, ambiguous fields).",
                "recommendation must be concrete when status is WARN or FAIL.",
                "Return raw JSON only, no markdown fences."
            ],
            "standards": [
                {"id": s["id"], "title": s["title"], "description": s["desc"]}
                for s in standards
            ],
            "subjects": group,
        }

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
            "- finding must be specific (name actual UCs, missing scenarios, ambiguous fields)",
            "- recommendation must be concrete when status is WARN or FAIL",
            "- Return raw JSON only, no markdown fences",
            "",
            "## Architecture standards",
            std_block,
            "",
            "## Subjects to review",
        ]
        for sub in group:
            prompt_lines.append(f"\n### {sub['subjectId']} [{sub['subjectType']}] {sub.get('title','')}")
            prompt_lines.append(sub["context"])
            if sub.get("linkedUseCases"):
                prompt_lines.append("\nLinked use cases:")
                for uc in sub["linkedUseCases"]:
                    prompt_lines.append(f"- {uc['id']}: {uc.get('title','')} "
                                        f"(tests: {uc.get('testCount',0)})".rstrip())
            if sub.get("linkedFeatures"):
                prompt_lines.append(f"\nLinked features: {', '.join(sub['linkedFeatures'])}")
            if sub.get("testCount") is not None:
                prompt_lines.append(f"Tests linked via UC→Feature→Test path: {sub['testCount']}"
                                    + (f" (last run: {sub['lastRun']})" if sub.get("lastRun") else ""))
        request["prompt"] = "\n".join(prompt_lines)

        if output_path:
            p = Path(output_path)
            if len(groups) > 1:
                p = p.with_name(f"{p.stem}.{idx + 1}{p.suffix}")
            p.write_text(json.dumps(request, indent=2), encoding="utf-8")
            print(f"  Coverage review request written to: {p}")
        else:
            print("\n" + "=" * 62)
            print("  COVERAGE REVIEW AI ANALYSIS REQUEST")
            print("=" * 62)
            print(request["prompt"])
            print("\n" + "=" * 62)
            print("  MACHINE-READABLE REQUEST JSON")
            print("=" * 62)
            print(json.dumps(request, indent=2))

    print("\n  Next step: run the analysis in your AI session, save the result JSON,")
    print("  then apply it with:")
    print(f"    python3 scripts/sys_edge.py coverage-review --apply-results <result.json>")


def _apply_coverage_review_results(path: str, file_proposals: bool = False):
    """Read assistant-produced JSON and write coverage-review results to the graph."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: result file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not parse {path}: {e}", file=sys.stderr)
        sys.exit(1)

    subjects = data.get("subjects", []) if isinstance(data, dict) else data
    if not subjects:
        print("  No subjects found in result file.")
        return

    W = 72
    any_fail = False
    proposals_filed: list[str] = []

    drv = _driver()
    with drv.session() as s:
        for sub in subjects:
            subject_id = sub.get("subjectId")
            subject_type = sub.get("subjectType", "")
            title = sub.get("title", "")
            dims = sub.get("dimensions", [])
            if not subject_id:
                continue

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
                std_title = next((st["title"] for st in _fetch_as_req_standards(s) if st["id"] == sid), sid)

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
                print(f"  Recommendations")
                print(f"{'─' * W}")
                for i, r in enumerate(recs, 1):
                    print(f"  {i}. {r}")
                print()

            overall = "ALL PASS" if fail_count == 0 and warn_count == 0 else (
                "NEEDS WORK" if fail_count > 0 else "REVIEW WARNINGS")
            print(f"{'═' * W}")
            print(f"  {fail_count} FAIL  ·  {warn_count} WARN  ·  {pass_count} PASS   (overall: {overall})")
            print(f"{'═' * W}\n")

            result_label = "PASS" if fail_count == 0 and warn_count == 0 else (
                "FAIL" if fail_count > 0 else "WARN")
            uc_id = subject_id if subject_type == "SysUseCase" else ""
            us_id = subject_id if subject_type == "SysUserStory" else ""
            _stamp_audit(subject_id, "coverageReviewLastRun", "coverageReviewResult", result_label,
                         uc_id=uc_id, us_id=us_id)

            if fail_count > 0:
                any_fail = True

            if file_proposals and (fail_count > 0 or warn_count > 0):
                for d in dims:
                    if d.get("status", "").upper() in ("WARN", "FAIL") and d.get("recommendation"):
                        prop_title = f"Coverage gap: {subject_id} / {d['id']}"
                        prop_desc = (
                            f"AI review of {subject_id} found a {d['status'].upper()} on {d['id']}.\n"
                            f"Finding: {d.get('finding','')}\n"
                            f"Recommendation: {d['recommendation']}\n"
                            f"Source: coverage-review --apply-results {path}"
                        )
                        try:
                            prop_id = _alloc_id(s, "SysProposal", "PROP-")
                            s.run("""
                                MATCH (p:SysProposal {id:$id})
                                SET p.title=$title, p.description=$desc,
                                    p.instance=$inst, p.priority='Should',
                                    p.status='draft', p.createdAt=$now,
                                    p.source=$src
                            """, id=prop_id, title=prop_title, desc=prop_desc,
                                 inst="architect", now=_now_iso(), src=subject_id)
                            proposals_filed.append(f"{prop_id} ← {subject_id} / {d['id']}")
                            print(f"    → filed {prop_id}")
                        except Exception as e:
                            print(f"    ⚠ could not file proposal for {subject_id} / {d['id']}: {e}",
                                  file=sys.stderr)

    drv.close()

    if proposals_filed:
        print(f"\n  Proposals filed ({len(proposals_filed)}):")
        for p in proposals_filed:
            print(f"    {p}")

    if any_fail:
        sys.exit(1)


def _coverage_review_as_req(s, model_name, us_id, uc_id, provider_name=""):
    """AS-REQ dimensional review for a single US or UC."""
    W = 72

    # Fetch AS-REQ standards
    stds = s.run(
        "MATCH (a:SysArchStd) WHERE a.id STARTS WITH 'AS-REQ-' "
        "RETURN a.id AS id, a.title AS title, a.description AS desc ORDER BY a.id"
    ).data()
    if not stds:
        print("ERROR: No AS-REQ SysArchStd nodes found in graph.", file=sys.stderr)
        sys.exit(1)

    subject_id = us_id or uc_id
    context_lines = []
    uc_rows = []

    if us_id:
        row = s.run(
            "MATCH (us:SysUserStory {id:$id}) "
            "OPTIONAL MATCH (us)-[:REALIZED_BY]->(uc:SysUseCase) "
            "RETURN us.id AS id, us.title AS title, us.actor AS actor, "
            "       us.goal AS goal, us.benefit AS benefit, "
            "       us.acceptanceCriteria AS ac, us.outOfScope AS oos, "
            "       collect(DISTINCT {id: uc.id, title: uc.title, desc: uc.description, precond: uc.preconditions, mainflow: uc.mainFlow, authRoles: uc.authorizedRoles, failScenarios: uc.failureScenarios}) AS ucs",
            id=us_id
        ).single()
        if not row:
            print(f"ERROR: User story {us_id} not found", file=sys.stderr); sys.exit(1)
        uc_rows = [u for u in (row["ucs"] or []) if u.get("id")]

        # ENH-876: fetch test counts per UC via indirect path (UC→Feature←Test)
        # collect() in the main query cannot do this aggregation per-UC cleanly
        if uc_rows:
            uc_ids_for_count = [u["id"] for u in uc_rows]
            test_counts = {
                r["uid"]: {"count": r["tc"], "last_run": str(r.get("lr") or "")[:10]}
                for r in s.run("""
                    UNWIND $uids AS uid
                    MATCH (uc:SysUseCase {id: uid})
                    OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)<-[:VERIFIES]-(t:SysTest)
                    OPTIONAL MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t)
                    RETURN uid, count(DISTINCT t) AS tc, max(pkg.lastRun) AS lr
                """, uids=uc_ids_for_count).data()
            }
            for u in uc_rows:
                tc = test_counts.get(u["id"], {})
                u["testCount"] = tc.get("count", 0)
                u["lastRun"]   = tc.get("last_run", "")

        # Preflight: flag UCs with null/short descriptions before wasting AI calls
        thin_ucs = [u["id"] for u in uc_rows if not u.get("desc") or len(u["desc"]) < 50]
        if thin_ucs:
            print(f"  ⚠  {subject_id}: {len(thin_ucs)} UC(s) have null/short descriptions "
                  f"(<50 chars) — AS-REQ results may be unreliable: {', '.join(thin_ucs)}")
        context_lines = [
            f"User Story: {row['title']} ({us_id})",
            f"Actor: {row.get('actor') or 'User'}",
            f"Goal: {row.get('goal') or ''}",
        ]
        if row.get("benefit"):
            context_lines.append(f"So that: {row['benefit']}")
        if row.get("ac"):
            context_lines.append(f"Acceptance criteria: {row['ac']}")
        if row.get("oos"):
            context_lines.append(f"Out of scope: {row['oos']}")
        if uc_rows:
            uc_summary = "\n".join(
                f"  [{u['id']}] {u['title']}"
                + (f"\n    Tests via UC→Feature→Test: {u.get('testCount', 0)}"
                   + (f" (last run: {u['lastRun']})" if u.get("lastRun") else " (none run)"))
                + (f"\n    Authorized roles: {u['authRoles']}" if u.get("authRoles") else "")
                + (f"\n    Failure scenarios: {u['failScenarios']}" if u.get("failScenarios") else "")
                + (f"\n    Preconditions: {u['precond']}" if u.get("precond") else "")
                + (f"\n    Main flow: {u['mainflow']}" if u.get("mainflow") else "")
                + (f"\n    {u['desc']}" if u.get("desc") else "")
                for u in uc_rows
            )
            context_lines.append(f"\nRealising use cases:\n{uc_summary}")
        header = f"coverage-review: {us_id}"
        subtitle = row.get("title", "")
        uc_ids_str = ", ".join(u["id"] for u in uc_rows) if uc_rows else "(none)"

    elif uc_id:
        row = s.run(
            "MATCH (uc:SysUseCase {id:$id}) "
            "OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc) "
            "OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature) "
            # ENH-876: count tests via indirect UC→Feature←Test path (no direct VERIFIES edge on UC)
            "OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f) "
            "OPTIONAL MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t) "
            "RETURN uc.id AS id, uc.title AS title, uc.description AS desc, "
            "       uc.preconditions AS precond, uc.mainFlow AS mainflow, "
            "       uc.authorizedRoles AS auth_roles, "
            "       uc.failureScenarios AS fail_scenarios, "
            "       uc.instance AS instance, "
            "       collect(DISTINCT us.id) AS story_ids, "
            "       collect(DISTINCT f.id) AS feat_ids, "
            "       count(DISTINCT t) AS test_count, "
            "       max(pkg.lastRun) AS last_run",
            id=uc_id
        ).single()
        if not row:
            print(f"ERROR: Use case {uc_id} not found", file=sys.stderr); sys.exit(1)
        context_lines = [
            f"Use Case: {row['title']} ({uc_id})",
            f"Instance: {row.get('instance') or 'unknown'}",
            f"Parent stories: {', '.join(row['story_ids'] or []) or '(none)'}",
            f"Features: {', '.join(row['feat_ids'] or []) or '(none)'}",
            f"Tests linked via UC→Feature→Test path: {row.get('test_count', 0)}"
            + (f" (last run: {str(row['last_run'])[:10]})" if row.get('last_run') else " (none run)"),
        ]
        if row.get("precond"):
            context_lines.append(f"Preconditions: {row['precond']}")
        if row.get("mainflow"):
            context_lines.append(f"Main flow: {row['mainflow']}")
        if row.get("auth_roles"):
            context_lines.append(f"Authorized roles: {row['auth_roles']}")
        if row.get("fail_scenarios"):
            context_lines.append(f"Failure scenarios: {row['fail_scenarios']}")
        if row.get("desc"):
            context_lines.append(f"\nDescription:\n{row['desc']}")
        header = f"coverage-review: {uc_id}"
        subtitle = row.get("title", "")
        uc_ids_str = uc_id

    std_block = "\n\n".join(
        f"### {std['id']} — {std['title']}\n{std['desc']}"
        for std in stds
    )
    context_block = "\n".join(context_lines)
    mode_desc = "user story's use case set" if us_id else "single use case internal adequacy"

    prompt = f"""You are reviewing requirements adequacy for a {mode_desc}.

## Subject
{context_block}

## Evaluation Dimensions
For each AS-REQ dimension below, evaluate the subject and return structured JSON.

{std_block}

Return ONLY a JSON object with this exact structure:
{{
  "dimensions": [
    {{
      "id": "<standard id e.g. AS-REQ-001>",
      "status": "PASS" | "WARN" | "FAIL",
      "finding": "<one specific sentence about what was found or what is missing>",
      "recommendation": "<if WARN or FAIL: one concrete action; if PASS: empty string>"
    }}
  ]
}}

Rules:
- PASS: fully satisfied
- WARN: partially satisfied — something present but incomplete or ambiguous
- FAIL: clearly absent or clearly wrong
- finding must be specific (name actual UCs, missing scenarios, ambiguous fields)
- recommendation must be concrete when status is WARN or FAIL
- Return raw JSON only, no markdown fences"""

    import re as _re
    import hashlib as _hashlib

    # ENH-900: cache result keyed on hash of prompt content (entity + standards)
    # Stored on the UC/US node as coverageReviewCacheKey + coverageReviewCacheData
    cache_key = _hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def _load_cache(session, entity_id, node_label):
        row = session.run(
            f"MATCH (n:{node_label} {{id:$id}}) "
            "RETURN n.coverageReviewCacheKey AS k, n.coverageReviewCacheData AS d",
            id=entity_id
        ).single()
        if row and row["k"] == cache_key and row["d"]:
            try:
                return json.loads(row["d"])
            except Exception:
                pass
        return None

    def _store_cache(session, entity_id, node_label, result_obj):
        try:
            session.run(
                f"MATCH (n:{node_label} {{id:$id}}) "
                "SET n.coverageReviewCacheKey=$k, n.coverageReviewCacheData=$d",
                id=entity_id, k=cache_key, d=json.dumps(result_obj)
            )
        except Exception:
            pass

    # Check cache first
    cache_label = "SysUseCase" if uc_id else "SysUserStory"
    cache_entity = uc_id or us_id
    cached = _load_cache(s, cache_entity, cache_label)
    if cached:
        result = cached
        print(f"  (using cached result — run with updated UC/US content to refresh)")
    else:

        def _call_and_parse_req(prompt_text):
            raw = _ai_call(prompt_text, model_name, max_tokens=2048, provider=provider_name)
            m = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if not m:
                return None, raw
            try:
                return json.loads(m.group()), raw
            except json.JSONDecodeError:
                return None, raw

        result, raw = _call_and_parse_req(prompt)
        if result is None:
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY the JSON object with no text outside it."
            result, raw = _call_and_parse_req(retry_prompt)

        if result is not None:
            _store_cache(s, cache_entity, cache_label, result)

    if result is None:
        print(f"  ⚠  {subject_id}: could not parse AI response after retry — skipping",
              file=sys.stderr)
        return

    dims = result.get("dimensions", [])
    pass_count = warn_count = fail_count = 0
    recs = []

    print(f"\n{'═' * W}")
    print(f"  {header}")
    print(f"  {subtitle[:68]}")
    if us_id and uc_rows:
        print(f"  UCs: {uc_ids_str}")
    print(f"  Model: {model_name}")
    print(f"{'═' * W}\n")

    for d in dims:
        sid   = d.get("id", "")
        status = d.get("status", "FAIL").upper()
        finding = d.get("finding", "")
        rec     = d.get("recommendation", "")
        std_title = next((st["title"] for st in stds if st["id"] == sid), sid)

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
        print(f"  Recommendations")
        print(f"{'─' * W}")
        for i, r in enumerate(recs, 1):
            print(f"  {i}. {r}")
        print()

    overall = "ALL PASS" if fail_count == 0 and warn_count == 0 else ("NEEDS WORK" if fail_count > 0 else "REVIEW WARNINGS")
    print(f"{'═' * W}")
    print(f"  {fail_count} FAIL  ·  {warn_count} WARN  ·  {pass_count} PASS   (overall: {overall})")
    print(f"{'═' * W}\n")

    # Stamp coverageReviewLastRun + coverageReviewResult on the node
    result_label = "PASS" if fail_count == 0 and warn_count == 0 else ("FAIL" if fail_count > 0 else "WARN")
    _stamp_audit(subject_id, "coverageReviewLastRun", "coverageReviewResult", result_label,
                 uc_id=uc_id, us_id=us_id)

    if fail_count > 0:
        sys.exit(1)


def _fetch_audit_test_standards(session, tier_prefix: str) -> list[dict]:
    """Return AS-TEST standards for the given tier prefix."""
    return session.run(
        "MATCH (a:SysArchStd) WHERE a.id STARTS WITH $prefix "
        "RETURN a.id AS id, a.title AS title, a.description AS desc ORDER BY a.id",
        prefix=tier_prefix
    ).data()


def _build_audit_test_subject(session, uc_id: str, feat_id: str, us_id: str,
                              tier: str, file_arg: str) -> tuple[dict | None, str]:
    """Gather context, discover the test file, and build a source excerpt.

    Returns (subject_dict, error_message).  subject_dict is None on error.
    """
    tier_label = {
        "usecase":     "UC / UI Flow",
        "integration": "Component / Integration",
        "e2e":         "E2E / User Story",
    }.get(tier, tier)

    subject_id = uc_id or feat_id or us_id
    context_lines = []
    feature_ids: list[str] = []
    subject_type = ""

    if uc_id:
        subject_type = "SysUseCase"
        row = session.run(
            "MATCH (uc:SysUseCase {id:$id}) "
            "OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc) "
            "OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature) "
            "RETURN uc.id AS id, uc.title AS title, uc.description AS desc, "
            "       uc.instance AS instance, uc.preconditions AS precond, "
            "       uc.authorizedRoles AS auth_roles, "
            "       uc.failureScenarios AS fail_scenarios, "
            "       collect(DISTINCT us.title) AS stories, "
            "       collect(DISTINCT f.id) AS feat_ids",
            id=uc_id
        ).single()
        if not row:
            return None, f"ERROR: UC {uc_id} not found"
        feature_ids = [fid for fid in (row["feat_ids"] or []) if fid]
        context_lines = [
            f"Use Case: {row['title']} ({uc_id})",
            f"Instance: {row.get('instance') or 'unknown'}",
        ]
        if row.get("stories"):
            context_lines.append(f"Parent stories: {', '.join(row['stories'])}")
        if row.get("precond"):
            context_lines.append(f"Preconditions: {row['precond']}")
        if row.get("auth_roles"):
            context_lines.append(f"Authorized roles: {row['auth_roles']}")
        if row.get("fail_scenarios"):
            context_lines.append(f"Failure scenarios: {row['fail_scenarios']}")
        if row.get("desc"):
            context_lines.append(f"\nDescription:\n{row['desc']}")

    elif feat_id:
        subject_type = "SysFeature"
        row = session.run(
            "MATCH (f:SysFeature {id:$id}) "
            "OPTIONAL MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f) "
            "RETURN f.id AS id, f.name AS name, f.description AS desc, "
            "       collect(DISTINCT ep.method + ' ' + ep.path) AS endpoints",
            id=feat_id
        ).single()
        if not row:
            return None, f"ERROR: Feature {feat_id} not found"
        feature_ids = [feat_id]
        context_lines = [f"Feature: {row['name']} ({feat_id})"]
        if row.get("desc"):
            context_lines.append(f"Description: {row['desc']}")
        eps = [e for e in (row.get("endpoints") or []) if e and e.strip() != " "]
        if eps:
            context_lines.append(f"Endpoints: {', '.join(eps)}")

    elif us_id:
        subject_type = "SysUserStory"
        row = session.run(
            "MATCH (us:SysUserStory {id:$id}) "
            "OPTIONAL MATCH (us)-[:REALIZED_BY]->(uc:SysUseCase) "
            "OPTIONAL MATCH (us)-[:REQUIRES]->(f:SysFeature) "
            "RETURN us.id AS id, us.title AS title, us.actor AS actor, "
            "       us.goal AS goal, us.benefit AS benefit, "
            "       us.acceptanceCriteria AS ac, us.outOfScope AS oos, "
            "       collect(DISTINCT uc.id) AS uc_ids, "
            "       collect(DISTINCT f.id) AS feat_ids",
            id=us_id
        ).single()
        if not row:
            return None, f"ERROR: User story {us_id} not found"
        feature_ids = [fid for fid in (row["feat_ids"] or []) if fid]
        context_lines = [
            f"User Story: {row['title']} ({us_id})",
            f"Actor: {row.get('actor') or 'User'}",
            f"Goal: {row.get('goal') or ''}",
        ]
        if row.get("benefit"):
            context_lines.append(f"So that: {row['benefit']}")
        if row.get("ac"):
            context_lines.append(f"Acceptance criteria: {row['ac']}")
        if row.get("oos"):
            context_lines.append(f"Out of scope: {row['oos']}")
        if row.get("uc_ids"):
            context_lines.append(f"Realising use cases: {', '.join(row['uc_ids'])}")

    # Discover test file
    test_file_path = file_arg
    if not test_file_path:
        if feature_ids:
            _TIER_CATS = {
                "usecase":     ["usecase", "ui", "ui_flow"],
                "integration": ["integration", "api", "functional"],
                "e2e":         ["e2e", "end_to_end"],
                "component":   ["component", "unit"],
            }
            preferred_cats = _TIER_CATS.get(tier, [])

            if preferred_cats:
                tier_rows = session.run(
                    "MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature) "
                    "WHERE f.id IN $fids AND t.id CONTAINS '::' "
                    "MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t) "
                    "WHERE pkg.testCategory IN $cats "
                    "RETURN t.id AS tid, t.lastTestedAt AS lr, t.scannedAt AS sa "
                    "ORDER BY t.lastTestedAt DESC, t.scannedAt DESC",
                    fids=feature_ids, cats=preferred_cats
                ).data()
            else:
                tier_rows = []

            test_rows = tier_rows or session.run(
                "MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature) "
                "WHERE f.id IN $fids AND t.id CONTAINS '::' "
                "RETURN t.id AS tid, t.lastTestedAt AS lr, t.scannedAt AS sa "
                "ORDER BY t.lastTestedAt DESC, t.scannedAt DESC",
                fids=feature_ids
            ).data()

            if not tier_rows and test_rows:
                print(f"  ⚠  No {tier}-tier test file found for {subject_id}; "
                      f"using best available. Use --file to override.", file=sys.stderr)
        else:
            test_rows = []

        if test_rows:
            seen: dict[str, None] = {}
            for tr in test_rows:
                tid = tr["tid"]
                fp = tid.split("::")[0] if "::" in tid else tid
                seen[fp] = None
            # Prefer the file whose path contains a normalised form of the UC ID
            # e.g. "UC-LIC-002" → "uclic002" matches "test_uclic002.py" or "TestUCLic002"
            uc_norm = subject_id.lower().replace("-", "").replace("_", "")
            preferred = [fp for fp in seen if uc_norm in fp.lower().replace("_", "").replace("-", "")]
            test_file_path = preferred[0] if preferred else next(iter(seen))
        else:
            return None, f"No tests registered for {subject_id} — run scan-tests first."

    p = _root / test_file_path if not test_file_path.startswith("/") else Path(test_file_path)
    if not p.exists():
        return None, (
            f"ERROR: Test file not found: {p}\n"
            f"  (resolved from graph node: {test_file_path})\n"
            f"  Use --file <path> to specify the file directly."
        )

    test_content = p.read_text(encoding="utf-8")

    TRUNCATE_THRESHOLD = 8000
    if len(test_content) > TRUNCATE_THRESHOLD and p.suffix == ".py":
        try:
            tree = ast.parse(test_content)
            lines = []
            lines.append(f"# Structured summary of {p.name} ({len(test_content):,} chars, {test_content.count(chr(10))} lines)")
            lines.append("# Full class/method inventory — all test methods listed\n")
            if ast.get_docstring(tree):
                lines.append(f'"""Module: {ast.get_docstring(tree)[:200]}"""\n')
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    cls_doc = ast.get_docstring(node) or ""
                    lines.append(f"\nclass {node.name}:")
                    if cls_doc:
                        lines.append(f'    """{cls_doc[:150]}"""')
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                            fn_doc = ast.get_docstring(item) or ""
                            decorators = [ast.unparse(d) for d in item.decorator_list] if item.decorator_list else []
                            for dec in decorators:
                                lines.append(f"    @{dec}")
                            lines.append(f"    def {item.name}(self, ...):  # line {item.lineno}")
                            if fn_doc:
                                lines.append(f'        """{fn_doc[:200]}"""')
                            body_lines = []
                            for stmt in item.body:
                                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                                    continue
                                try:
                                    body_lines.append(f"        {ast.unparse(stmt)[:120]}")
                                except Exception:
                                    pass
                                if len(body_lines) >= 6:
                                    body_lines.append("        ...")
                                    break
                            lines.extend(body_lines)
            test_excerpt = "\n".join(lines)
            test_excerpt += f"\n\n# END SUMMARY — full source is {len(test_content):,} chars"
        except Exception:
            test_excerpt = test_content[:TRUNCATE_THRESHOLD] + f"\n\n... [truncated at {TRUNCATE_THRESHOLD} chars of {len(test_content):,}]"
    elif len(test_content) > TRUNCATE_THRESHOLD:
        test_excerpt = test_content[:TRUNCATE_THRESHOLD] + f"\n\n... [truncated — {len(test_content):,} chars total]"
    else:
        test_excerpt = test_content

    return {
        "subjectId": subject_id,
        "subjectType": subject_type,
        "tier": tier,
        "tierLabel": tier_label,
        "context": "\n".join(context_lines),
        "testFilePath": str(p),
        "testFileName": p.name,
        "testExcerpt": test_excerpt,
        "featureIds": feature_ids,
    }, ""


def _emit_audit_test_request(args, subjects: list[dict], standards: list[dict]):
    """Emit a JSON request for the assistant to run an AS-TEST audit."""
    if not subjects:
        print("  No subjects found — nothing to audit.")
        return

    output_path = getattr(args, "output_request", "") or ""
    response_format = {
        "subjects": [
            {
                "subjectId": "<UC-xxx|F-xxx|US-xxx>",
                "subjectType": "<SysUseCase|SysFeature|SysUserStory>",
                "tier": "<usecase|integration|e2e>",
                "overall": "PASS|WARN|FAIL",
                "dimensions": [
                    {
                        "id": "<AS-TEST-...>",
                        "status": "PASS|WARN|FAIL",
                        "finding": "<specific sentence>",
                        "action": "<concrete action if WARN/FAIL, else empty>"
                    }
                ]
            }
        ]
    }

    std_block = "\n\n".join(
        f"### {std['id']} — {std['title']}\n{std['desc']}"
        for std in standards
    )

    prompt_lines = [
        "You are evaluating test files against architecture standards.",
        "",
        "Return STRICT JSON in this format:",
        json.dumps(response_format, indent=2),
        "",
        "Rules:",
        "- PASS: the test file adequately satisfies this dimension",
        "- WARN: partially satisfied — something present but incomplete",
        "- FAIL: clearly absent or clearly incorrect",
        "- finding must reference actual content in the file (test names, lines, patterns seen or absent)",
        "- action must be a concrete implementation instruction when status is WARN or FAIL",
        "- Return raw JSON only, no markdown fences",
        "",
        "## Architecture standards",
        std_block,
        "",
        "## Subjects to audit",
    ]

    for sub in subjects:
        prompt_lines.append(
            f"\n### {sub['subjectId']} [{sub['subjectType']}] — {sub['tierLabel']}")
        prompt_lines.append(sub["context"])
        prompt_lines.append(f"\nTest file: {sub['testFilePath']}")
        prompt_lines.append("\n```python")
        prompt_lines.append(sub["testExcerpt"])
        prompt_lines.append("```")

    request = {
        "schemaVersion": 2,
        "command": "audit-test",
        "instruction": (
            "Audit each test file against the AS-TEST architecture standards below. "
            "Return one overall PASS/WARN/FAIL per subject and one dimension row per standard."
        ),
        "responseFormat": response_format,
        "rules": [
            "PASS: the test file adequately satisfies this dimension",
            "WARN: partially satisfied — something present but incomplete",
            "FAIL: clearly absent or clearly incorrect",
            "finding must reference actual content in the file",
            "action must be concrete when status is WARN or FAIL",
            "Return raw JSON only, no markdown fences"
        ],
        "standards": [
            {"id": s["id"], "title": s["title"], "description": s["desc"]}
            for s in standards
        ],
        "subjects": subjects,
        "prompt": "\n".join(prompt_lines),
    }

    if output_path:
        Path(output_path).write_text(json.dumps(request, indent=2), encoding="utf-8")
        print(f"  Audit request written to: {output_path}")
    else:
        print("\n" + "=" * 62)
        print("  AUDIT TEST AI ANALYSIS REQUEST")
        print("=" * 62)
        print(request["prompt"])
        print("\n" + "=" * 62)
        print("  MACHINE-READABLE REQUEST JSON")
        print("=" * 62)
        print(json.dumps(request, indent=2))

    print("\n  Next step: run the analysis in your AI session, save the result JSON,")
    print("  then apply it with:")
    print(f"    python3 scripts/sys_edge.py audit-test --apply-results <result.json>")


def _apply_audit_test_results(path: str):
    """Read assistant-produced JSON and write audit-test results to the graph."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: result file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not parse {path}: {e}", file=sys.stderr)
        sys.exit(1)

    subjects = data.get("subjects", []) if isinstance(data, dict) else data
    if not subjects:
        print("  No subjects found in result file.")
        return

    W = 72
    any_fail = False

    drv = _driver()
    with drv.session() as s:
        for sub in subjects:
            subject_id = sub.get("subjectId")
            subject_type = sub.get("subjectType", "")
            tier = sub.get("tier", "")
            dims = sub.get("dimensions", [])
            if not subject_id:
                continue

            pass_count = warn_count = fail_count = 0
            actions = []

            print(f"\n{'═' * W}")
            print(f"  audit-test: {subject_id}  [tier: {tier}]")
            print(f"{'═' * W}\n")

            for d in dims:
                sid = d.get("id", "")
                status = d.get("status", "FAIL").upper()
                finding = d.get("finding", "")
                action = d.get("action", "")
                _TIER_PREFIX_MAP = {
                    "usecase": "AS-TEST-UC-",
                    "integration": "AS-TEST-COMP-",
                    "e2e": "AS-TEST-US-",
                }
                std_title = next((st["title"] for st in _fetch_audit_test_standards(s, _TIER_PREFIX_MAP.get(tier, "AS-TEST-")) if st["id"] == sid), sid)

                print(f"  [{status:<4}]  {sid}  {std_title}")
                if finding:
                    print(f"          {finding[:110]}")
                if action:
                    print(f"          → {action[:108]}")
                print()

                if status == "PASS":
                    pass_count += 1
                elif status == "WARN":
                    warn_count += 1
                    if action:
                        actions.append(f"[{sid}] {action}")
                else:
                    fail_count += 1
                    if action:
                        actions.append(f"[{sid}] {action}")

            if actions:
                print(f"{'─' * W}")
                print(f"  Action items")
                print(f"{'─' * W}")
                for i, a in enumerate(actions, 1):
                    print(f"  {i}. {a}")
                print()

            overall = ("ALL PASS" if fail_count == 0 and warn_count == 0
                       else "NEEDS WORK" if fail_count > 0 else "REVIEW WARNINGS")
            print(f"{'═' * W}")
            print(f"  {fail_count} FAIL  ·  {warn_count} WARN  ·  {pass_count} PASS   (overall: {overall})")
            print(f"{'═' * W}\n")

            result_label = "PASS" if fail_count == 0 and warn_count == 0 else (
                "FAIL" if fail_count > 0 else "WARN")
            uc_id = subject_id if subject_type == "SysUseCase" else ""
            us_id = subject_id if subject_type == "SysUserStory" else ""
            streak = _stamp_audit(subject_id, "auditTestLastRun", "auditTestResult", result_label,
                                  uc_id=uc_id, us_id=us_id)
            # ENH-2477: hint after 3+ consecutive failures
            if result_label == "FAIL" and streak >= 3 and recs:
                failing_dims = [r.split("]")[0].lstrip("[") for r in recs if "]" in r]
                print(f"  ⚑  REPEATED FAILURE ({streak}x) — diagnostic hints:")
                print(f"     Failing dimensions: {', '.join(failing_dims)}")
                print(f"     1. Check the test file imports the classes under test directly")
                print(f"     2. Update UC prose for each failing dimension")
                print(f"     3. Run audit-test --uc {subject_id} --preflight to verify file path")
                print(f"     4. Override file: audit-test --uc {subject_id} --file <path>")
                print()

            if fail_count > 0:
                any_fail = True

    drv.close()
    if any_fail:
        sys.exit(1)


def cmd_audit_test(args):
    """AI-powered test adequacy audit against AS-TEST architecture standards.

    Evaluates a test file against the applicable tier's AS-TEST-* dimensions.
    Reads SysArchStd nodes from the graph at runtime — never hardcodes dimension text.

    Usage:
      audit-test --uc UC-DLG-001 [--file path/to/test.py] [--tier usecase]
      audit-test --feature F-DLG-001 [--tier integration]
      audit-test --us US-CTM-010 [--tier e2e]
    """
    _require_licence("audit-test")
    uc_id      = getattr(args, "uc",       "") or ""
    ucs_batch  = getattr(args, "ucs",      "") or ""
    feat_id    = getattr(args, "feature",  "") or ""
    us_id      = getattr(args, "us",       "") or ""
    file_arg   = getattr(args, "file",     "") or ""
    tier_arg   = getattr(args, "tier",     "") or ""
    model_name    = getattr(args, "model",    "sonnet") or "sonnet"
    provider_name = getattr(args, "provider", "") or ""
    preflight     = getattr(args, "preflight", False)
    apply_results = getattr(args, "apply_results", "") or ""

    if apply_results:
        _apply_audit_test_results(apply_results)
        return

    # Batch mode: --ucs UC-A,UC-B,UC-C
    if ucs_batch:
        uc_ids = [u.strip() for u in ucs_batch.split(",") if u.strip()]
        any_fail = False
        for uid in uc_ids:
            args.uc = uid
            args.ucs = ""
            try:
                cmd_audit_test(args)
            except SystemExit as e:
                if e.code != 0:
                    any_fail = True
        if any_fail:
            sys.exit(1)
        return

    if not (uc_id or feat_id or us_id):
        print("ERROR: provide --uc, --feature, --us, or --ucs (batch)", file=sys.stderr)
        sys.exit(1)

    if tier_arg:
        tier = tier_arg
    elif uc_id:
        tier = "usecase"
    elif feat_id:
        tier = "integration"
    else:
        tier = "e2e"

    tier_prefix = {
        "usecase":     "AS-TEST-UC-",
        "integration": "AS-TEST-COMP-",
        "e2e":         "AS-TEST-US-",
    }.get(tier, "AS-TEST-UC-")

    tier_label = {
        "usecase":     "UC / UI Flow",
        "integration": "Component / Integration",
        "e2e":         "E2E / User Story",
    }.get(tier, tier)

    drv = _driver()
    with drv.session() as s:
        stds = _fetch_audit_test_standards(s, tier_prefix)
        if not stds:
            print(f"ERROR: No SysArchStd nodes found with prefix {tier_prefix}", file=sys.stderr)
            drv.close(); sys.exit(1)

        subject, err = _build_audit_test_subject(s, uc_id, feat_id, us_id, tier, file_arg)
        if err:
            print(err, file=sys.stderr)
            drv.close(); sys.exit(1)

        if getattr(args, "analyse", False):
            _emit_audit_test_request(args, [subject], stds)
            drv.close()
            return

    drv.close()

    p = Path(subject["testFilePath"])
    test_file_path = subject["testFilePath"]
    subject_id = subject["subjectId"]

    # Preflight: show resolved file and exit without running LLM
    if preflight:
        print(f"preflight: {subject_id} → {p}  ({'exists' if p.exists() else 'NOT FOUND'})")
        return

    context_block = subject["context"]
    test_excerpt = subject["testExcerpt"]

    # Build AI prompt
    std_block = "\n\n".join(
        f"### {std['id']} — {std['title']}\n{std['desc']}"
        for std in stds
    )

    prompt = f"""You are evaluating a test file against architecture standards for {tier_label} tests.

## Subject Under Test
{context_block}

## Test File: {p.name}
```python
{test_excerpt}
```

## Evaluation Dimensions
For each dimension below, evaluate the test file content against that dimension's standard.

{std_block}

Return ONLY a JSON object with this exact structure:
{{
  "dimensions": [
    {{
      "id": "<standard id e.g. AS-TEST-UC-001>",
      "status": "PASS" | "WARN" | "FAIL",
      "finding": "<one specific sentence: name actual test functions, classes, or specific gaps found>",
      "action": "<if WARN or FAIL: one concrete action naming a specific test class, method, or assertion; if PASS: empty string>"
    }}
  ]
}}

Rules:
- PASS: the test file adequately satisfies this dimension
- WARN: partially satisfied — something present but incomplete
- FAIL: clearly absent or clearly incorrect
- finding must reference actual content in the file (test names, lines, patterns seen or absent)
- action must be a concrete implementation instruction when status is WARN or FAIL
- Return raw JSON only, no markdown fences"""

    try:
        raw = _ai_call(prompt, model_name, max_tokens=2048, provider=provider_name)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)

    import re as _re
    json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if not json_match:
        # Retry with stricter instruction
        try:
            raw = _ai_call(prompt + "\n\nReturn ONLY the JSON object, no markdown fences.", model_name,
                           provider=provider_name)
            json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        except Exception:
            pass
    if not json_match:
        print("ERROR: Could not parse AI response as JSON", file=sys.stderr)
        print(raw, file=sys.stderr)
        sys.exit(1)
    try:
        result = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(f"ERROR: JSON parse error: {e}", file=sys.stderr)
        print(raw, file=sys.stderr)
        sys.exit(1)

    dims = result.get("dimensions", [])
    W = 72
    pass_count = warn_count = fail_count = 0
    actions = []

    print(f"\n{'═' * W}")
    print(f"  audit-test: {subject_id}  [tier: {tier}]")
    print(f"  Standards: {tier_prefix}* ({len(stds)} dimensions)  |  Model: {model_name}")
    print(f"  Test file: {test_file_path}")
    print(f"{'═' * W}\n")

    for d in dims:
        sid     = d.get("id", "")
        status  = d.get("status", "FAIL").upper()
        finding = d.get("finding", "")
        action  = d.get("action", "")
        std_title = next((st["title"] for st in stds if st["id"] == sid), sid)

        print(f"  [{status:<4}]  {sid}  {std_title}")
        if finding:
            print(f"          {finding[:110]}")
        if action:
            print(f"          → {action[:108]}")
        print()

        if status == "PASS":
            pass_count += 1
        elif status == "WARN":
            warn_count += 1
            if action:
                actions.append(f"[{sid}] {action}")
        else:
            fail_count += 1
            if action:
                actions.append(f"[{sid}] {action}")

    if actions:
        print(f"{'─' * W}")
        print(f"  Action items")
        print(f"{'─' * W}")
        for i, a in enumerate(actions, 1):
            print(f"  {i}. {a}")
        print()

    overall = ("ALL PASS" if fail_count == 0 and warn_count == 0
               else "NEEDS WORK" if fail_count > 0 else "REVIEW WARNINGS")
    print(f"{'═' * W}")
    print(f"  {fail_count} FAIL  ·  {warn_count} WARN  ·  {pass_count} PASS   (overall: {overall})")
    print(f"{'═' * W}\n")

    # Stamp auditTestLastRun + auditTestResult on the UC/Feature/US node
    if uc_id or feat_id or us_id:
        result_label = "PASS" if fail_count == 0 and warn_count == 0 else ("FAIL" if fail_count > 0 else "WARN")
        streak = _stamp_audit(subject_id, "auditTestLastRun", "auditTestResult", result_label, uc_id=uc_id, us_id=us_id)
        # ENH-2477: hint after 3+ consecutive failures
        if result_label == "FAIL" and streak >= 3 and actions:
            print(f"  ⚑  REPEATED FAILURE ({streak}x) — diagnostic hints:")
            failing_dims = [a.split("]")[0].lstrip("[") for a in actions if "]" in a]
            print(f"     Failing dimensions: {', '.join(failing_dims)}")
            print(f"     1. Check that the test file imports the classes under test directly")
            print(f"        (grep for the class name in the test file — missing import = silent gap)")
            print(f"     2. Update UC prose for each failing dimension — if the UC doesn't describe")
            print(f"        the scenario, the LLM can't verify it")
            print(f"     3. Run with --preflight to confirm the resolved test file path is correct")
            print(f"     4. If the test file is wrong: audit-test --uc {subject_id} --file <path>")
            print()

    if fail_count > 0:
        sys.exit(1)


def _stamp_audit(subject_id: str, ts_field: str, result_field: str, result: str,
                 uc_id: str = "", us_id: str = "") -> int:
    """Write last-run timestamp and result to the subject node (fire-and-forget).

    Returns the updated auditTestFailStreak for UC nodes (0 for non-UC or on error).
    """
    try:
        drv = _driver()
        now = _now_iso()
        streak = 0
        with drv.session() as s:
            if uc_id:
                # Track consecutive fail streak for audit-test hints (ENH-2477)
                if ts_field == "auditTestLastRun":
                    row = s.run(
                        "MATCH (n:SysUseCase {id:$id}) RETURN coalesce(n.auditTestFailStreak, 0) AS st",
                        id=subject_id).single()
                    prev_streak = row["st"] if row else 0
                    streak = (prev_streak + 1) if result == "FAIL" else 0
                    s.run(
                        f"MATCH (n:SysUseCase {{id:$id}}) "
                        f"SET n.{ts_field}=$now, n.{result_field}=$r, n.auditTestFailStreak=$st",
                        id=subject_id, now=now, r=result, st=streak)
                else:
                    s.run(f"MATCH (n:SysUseCase {{id:$id}}) SET n.{ts_field}=$now, n.{result_field}=$r",
                          id=subject_id, now=now, r=result)
            elif us_id:
                s.run(f"MATCH (n:SysUserStory {{id:$id}}) SET n.{ts_field}=$now, n.{result_field}=$r",
                      id=subject_id, now=now, r=result)
            else:
                s.run(f"MATCH (n:SysFeature {{id:$id}}) SET n.{ts_field}=$now, n.{result_field}=$r",
                      id=subject_id, now=now, r=result)
        drv.close()
        return streak
    except Exception:
        return 0  # stamping is non-critical — never block on it


def cmd_audit_status(args):
    """Show last audit-test and coverage-review timestamps per UC/US for an instance.

    Flags items not reviewed in --days (default: 14).
    """
    from datetime import datetime, timezone, timedelta
    instance = getattr(args, "instance", "") or ""
    days     = getattr(args, "days",     14)  or 14
    scope    = getattr(args, "scope",    "both") or "both"

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    W = 72

    drv = _driver()
    with drv.session() as s:

        if scope in ("uc", "both"):
            if instance:
                uc_rows = s.run("""
                    MATCH (uc:SysUseCase {instance:$inst})
                    RETURN uc.id AS id, uc.title AS title,
                           uc.auditTestLastRun AS atRun, uc.auditTestResult AS atRes,
                           uc.coverageReviewLastRun AS crRun, uc.coverageReviewResult AS crRes
                    ORDER BY uc.id
                """, inst=instance).data()
            else:
                uc_rows = s.run("""
                    MATCH (uc:SysUseCase)
                    RETURN uc.id AS id, uc.title AS title,
                           uc.auditTestLastRun AS atRun, uc.auditTestResult AS atRes,
                           uc.coverageReviewLastRun AS crRun, uc.coverageReviewResult AS crRes
                    ORDER BY uc.id
                """).data()

            stale = [r for r in uc_rows if
                     not r.get("atRun") or str(r["atRun"]) < cutoff or
                     not r.get("crRun") or str(r["crRun"]) < cutoff]

            print(f"\n{'═' * W}")
            print(f"  AUDIT STATUS — Use Cases  ({len(uc_rows)} total · {len(stale)} stale >{days}d)")
            print(f"{'═' * W}")
            print(f"  {'UC ID':<20} {'audit-test':>12}  {'result':>6}  {'cov-review':>12}  {'result':>6}")
            print(f"  {'─'*20} {'─'*12}  {'─'*6}  {'─'*12}  {'─'*6}")
            for r in uc_rows:
                at_date = str(r.get("atRun") or "")[:10] or "never"
                cr_date = str(r.get("crRun") or "")[:10] or "never"
                at_res  = r.get("atRes") or "—"
                cr_res  = r.get("crRes") or "—"
                at_stale = not r.get("atRun") or str(r["atRun"]) < cutoff
                cr_stale = not r.get("crRun") or str(r["crRun"]) < cutoff
                at_flag = " ⚠" if at_stale else ""
                cr_flag = " ⚠" if cr_stale else ""
                print(f"  {r['id']:<20} {at_date+at_flag:>14}  {at_res:>6}  {cr_date+cr_flag:>14}  {cr_res:>6}")

        if scope in ("us", "both"):
            if instance:
                us_rows = s.run("""
                    MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc:SysUseCase {instance:$inst})
                    WITH DISTINCT us
                    RETURN us.id AS id, us.title AS title,
                           us.coverageReviewLastRun AS crRun, us.coverageReviewResult AS crRes
                    ORDER BY us.id
                """, inst=instance).data()
            else:
                us_rows = s.run("""
                    MATCH (us:SysUserStory)
                    RETURN us.id AS id, us.title AS title,
                           us.coverageReviewLastRun AS crRun, us.coverageReviewResult AS crRes
                    ORDER BY us.id
                """).data()

            stale_us = [r for r in us_rows if
                        not r.get("crRun") or str(r["crRun"]) < cutoff]

            print(f"\n{'═' * W}")
            print(f"  AUDIT STATUS — User Stories  ({len(us_rows)} total · {len(stale_us)} stale >{days}d)")
            print(f"{'═' * W}")
            print(f"  {'US ID':<20} {'cov-review':>14}  {'result':>6}  Title")
            print(f"  {'─'*20} {'─'*14}  {'─'*6}  {'─'*40}")
            for r in us_rows:
                cr_date = str(r.get("crRun") or "")[:10] or "never"
                cr_res  = r.get("crRes") or "—"
                cr_flag = " ⚠" if (not r.get("crRun") or str(r["crRun"]) < cutoff) else ""
                print(f"  {r['id']:<20} {cr_date+cr_flag:>16}  {cr_res:>6}  {(r['title'] or '')[:40]}")

    drv.close()
    print()


def cmd_coverage_report(args):
    """4-tier V-model gap analysis: US→UC→Feature→Test traceability.

    surfaceType on SysFeature drives which test tiers are required.
    Values: ui | api | middleware | protocol | config | script
    Default (unset) is treated as 'api'.
    """
    instance = getattr(args, "instance", "") or ""
    tier_filter = getattr(args, "tier", "all") or "all"
    fmt = getattr(args, "format", "md") or "md"

    # Tier applicability: which test tiers are required per surfaceType
    _SURFACE_TIERS = {
        "ui":         {"cmp": True,  "int": True,  "uc": True,  "e2e": True},
        "api":        {"cmp": True,  "int": True,  "uc": True,  "e2e": False},
        "middleware": {"cmp": True,  "int": True,  "uc": False, "e2e": False},
        "protocol":   {"cmp": True,  "int": True,  "uc": False, "e2e": False},
        "config":     {"cmp": False, "int": True,  "uc": False, "e2e": False},
        "script":     {"cmp": True,  "int": True,  "uc": False, "e2e": False},
    }
    _DEFAULT_TIERS = {"cmp": True, "int": True, "uc": True, "e2e": False}  # api
    if _lightweight():
        _SURFACE_TIERS = {k: dict(v, uc=False, e2e=False) for k, v in _SURFACE_TIERS.items()}
        _DEFAULT_TIERS = dict(_DEFAULT_TIERS, uc=False, e2e=False)

    drv = _driver()
    with drv.session() as s:

        # ── T1: User Stories without Use Cases ───────────────────────────
        t1_rows = s.run("""
            MATCH (us:SysUserStory)
            WHERE NOT (us)-[:REALIZED_BY]->(:SysUseCase)
            RETURN us.id AS id, us.title AS title
            ORDER BY us.id
        """).data()

        # ── T2: Use Cases without REQUIRES→Feature edges ─────────────────
        t2_q = "MATCH (uc:SysUseCase) WHERE NOT (uc)-[:REQUIRES]->(:SysFeature)"
        t2_params: dict = {}
        if instance:
            t2_q += " AND uc.instance = $inst"
            t2_params["inst"] = instance
        t2_q += " RETURN uc.id AS id, uc.instance AS inst, uc.title AS title ORDER BY uc.instance, uc.id"
        t2_rows = s.run(t2_q, **t2_params).data()

        # ── T3: Feature test gaps (surfaceType-aware) ─────────────────────
        t3_q = """
            MATCH (f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            WHERE coalesce(f.status,'') <> 'Superseded'
        """
        t3_params: dict = {}
        if instance:
            t3_q += " AND m.instance = $inst"
            t3_params["inst"] = instance
        t3_q += """
            OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
              WHERE tc.testType IN ['component','go-unit']
                 OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
            OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
              WHERE ti.testType = 'integration'
                 OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
            OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
              WHERE tu.testType = 'usecase'
                 OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
            OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
              WHERE te.testType = 'e2e'
                 OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
            RETURN f.id AS fid, f.name AS fname,
                   coalesce(f.surfaceType,'api') AS surface,
                   m.id AS mid, m.instance AS minst,
                   count(DISTINCT tc) AS cmp, count(DISTINCT ti) AS int_,
                   count(DISTINCT tu) AS uc,  count(DISTINCT te) AS e2e
            ORDER BY m.instance, m.id, f.id
        """
        t3_all = s.run(t3_q, **t3_params).data()

        # Filter to only features with at least one required tier missing
        t3_gaps = []
        for row in t3_all:
            surf = row["surface"] or "api"
            tiers = _SURFACE_TIERS.get(surf, _DEFAULT_TIERS)
            counts = {"cmp": row["cmp"], "int": row["int_"], "uc": row["uc"], "e2e": row["e2e"]}
            gaps = {t: counts[t] == 0 for t, req in tiers.items() if req}
            if any(gaps.values()):
                t3_gaps.append({**row, "required": tiers, "counts": counts, "gaps": gaps})

        # ── T4: User Stories without e2e tests ────────────────────────────
        t4_rows = s.run("""
            MATCH (us:SysUserStory)
            WHERE NOT exists(
              (us)-[:REALIZED_BY]->(:SysUseCase)-[:REQUIRES]->(:SysFeature)<-[:VERIFIES]-(:SysTest {testType:'e2e'})
            )
            RETURN us.id AS id, us.title AS title
            ORDER BY us.id
        """).data()

    drv.close()

    today = date.today().isoformat()
    scope_tag = f" — {instance}" if instance else " — all instances"

    if fmt == "md":
        print(f"# SysEdge Coverage Report{scope_tag}  ({today})\n")

        if tier_filter in ("all", "1"):
            print(f"## T1 — User Stories without Use Cases ({len(t1_rows)})\n")
            if t1_rows:
                print("| US | Title |")
                print("|---|---|")
                for r in t1_rows:
                    print(f"| {r['id']} | {r['title']} |")
            else:
                print("_No gaps — all user stories have at least one UC._")
            print()

        if tier_filter in ("all", "2"):
            print(f"## T2 — Use Cases without Feature Links ({len(t2_rows)})\n")
            if t2_rows:
                print("| UC | Instance | Title |")
                print("|---|---|---|")
                for r in t2_rows:
                    print(f"| {r['id']} | {r['inst']} | {r['title'] or ''} |")
            else:
                print("_No gaps — all UCs have REQUIRES→Feature edges._")
            print()

        if tier_filter in ("all", "3"):
            print(f"## T3 — Feature Test Gaps ({len(t3_gaps)} features with missing tiers)\n")
            if t3_gaps:
                print("surfaceType controls which tiers are required "
                      "(ui/api/middleware/protocol/config/script).\n")
                print("| Feature | Module | Surface | cmp | int | uc | e2e |")
                print("|---|---|---|---|---|---|---|")
                for r in t3_gaps:
                    def _cell(tier):
                        req = r["required"].get(tier, False)
                        cnt = r["counts"].get(tier, 0)
                        if not req:
                            return "—"
                        return str(cnt) if cnt > 0 else "**0**"
                    print(f"| {r['fid']} | {r['mid']} | {r['surface']} "
                          f"| {_cell('cmp')} | {_cell('int')} | {_cell('uc')} | {_cell('e2e')} |")
            else:
                print("_No gaps — all features meet their required tier coverage._")
            print()

        if tier_filter in ("all", "4"):
            print(f"## T4 — User Stories without E2E Tests ({len(t4_rows)})\n")
            if t4_rows:
                print("| US | Title |")
                print("|---|---|")
                for r in t4_rows:
                    print(f"| {r['id']} | {r['title']} |")
            else:
                print("_No gaps — all user stories have e2e test coverage._")
            print()

        if tier_filter == "all":
            print("## Summary\n")
            print("| Tier | Gap count |")
            print("|---|---|")
            print(f"| T1 US→UC wiring | {len(t1_rows)} |")
            print(f"| T2 UC→Feature wiring | {len(t2_rows)} |")
            print(f"| T3 Feature test coverage | {len(t3_gaps)} |")
            print(f"| T4 US e2e coverage | {len(t4_rows)} |")
    else:
        # Plain text fallback
        print(f"Coverage Report{scope_tag}  {today}")
        print(f"  T1 US without UCs       : {len(t1_rows)}")
        print(f"  T2 UCs without features : {len(t2_rows)}")
        print(f"  T3 Feature test gaps    : {len(t3_gaps)}")
        print(f"  T4 US without e2e       : {len(t4_rows)}")


def cmd_migrate_binary(args):
    """Update the binary property on SysEndpoint and SysSymbol nodes from one binary to another."""
    drv = _driver()
    from_b = args.from_binary
    to_b   = args.to_binary
    fids   = [f.strip() for f in (getattr(args, "features", "") or "").split(",") if f.strip()]

    with drv.session() as s:
        if fids:
            ep_count = s.run("""
                MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature)
                WHERE f.id IN $fids AND ep.binary = $frm
                SET ep.binary = $to
                RETURN count(ep) AS n
            """, fids=fids, frm=from_b, to=to_b).single()["n"]
            sym_count = s.run("""
                MATCH (sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature)
                WHERE f.id IN $fids AND sym.binary = $frm
                SET sym.binary = $to
                RETURN count(sym) AS n
            """, fids=fids, frm=from_b, to=to_b).single()["n"]
        else:
            ep_count = s.run("""
                MATCH (ep:SysEndpoint) WHERE ep.binary = $frm
                SET ep.binary = $to
                RETURN count(ep) AS n
            """, frm=from_b, to=to_b).single()["n"]
            sym_count = s.run("""
                MATCH (sym:SysSymbol) WHERE sym.binary = $frm
                SET sym.binary = $to
                RETURN count(sym) AS n
            """, frm=from_b, to=to_b).single()["n"]
    drv.close()
    scope = f"  scope: {len(fids)} feature(s)" if fids else "  scope: all"
    print(f"✓ migrate-binary  {from_b} → {to_b}")
    print(f"  endpoints updated : {ep_count}")
    print(f"  symbols updated   : {sym_count}")
    print(scope)


def cmd_create_story(args):
    """Create a SysUserStory and optionally link to features and a user type."""
    drv = _driver()
    with drv.session() as s:
        s.run("""
            MERGE (us:SysUserStory {id:$id})
            SET us.title=$title, us.actor=$actor, us.goal=$goal,
                us.priority=$prio, us.benefit=$benefit,
                us.acceptanceCriteria=$ac
        """, id=args.id, title=args.title, actor=args.actor or "",
             goal=args.goal or "", prio=args.priority or "Should",
             benefit=args.benefit or "", ac=getattr(args, "acceptance_criteria", "") or "")
        for fid in (args.features or "").split(","):
            fid = fid.strip()
            if fid:
                s.run("""MATCH (us:SysUserStory {id:$uid}),(f:SysFeature {id:$fid})
                         MERGE (us)-[:REQUIRES]->(f)""", uid=args.id, fid=fid)
        if args.user:
            s.run("""MATCH (u:SysUser {id:$uid}),(us:SysUserStory {id:$sid})
                     MERGE (u)-[:INITIATES]->(us)""", uid=args.user, sid=args.id)
    drv.close()
    print(f"✓ {args.id}  {args.title}")


def cmd_preview_import(args):
    """Classify entities in an import JSON file as new/duplicate/near-duplicate.

    Reads the intermediate JSON produced by an import skill and compares each
    entity against the live graph. Prints a review table and writes a
    decisions JSON template the user can edit before commit.
    """
    _require_licence("preview-import")
    import json as _json
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr); sys.exit(1)
    data = _json.loads(path.read_text(encoding="utf-8"))
    kind = data.get("kind", "unknown")
    entities = data.get("entities", [])
    if not entities:
        print(f"No entities found in {path} (kind={kind})"); return

    drv = _driver()
    decisions = []
    W = 70
    print(f"\n{'═'*W}")
    print(f"  IMPORT PREVIEW  kind={kind}  {len(entities)} entities")
    print(f"  {'Ref':<22} {'Title/Name':<35} {'Status':<12} Action")
    print(f"{'─'*W}")

    with drv.session() as s:
        for ent in entities:
            ref   = ent.get("id_hint") or ent.get("name") or "?"
            title = (ent.get("title") or ent.get("name") or "")[:34]
            conf  = ent.get("_confidence", 1.0)
            action = "create"
            status = "new"
            match_id = None

            if conf < 0.5:
                action, status = "skip", "low-conf"

            elif kind == "stories":
                # Exact title match
                exact = s.run("""
                    MATCH (us:SysUserStory)
                    WHERE toLower(us.title) = toLower($t)
                    RETURN us.id AS id LIMIT 1
                """, t=ent.get("title","")).single()
                if exact:
                    action, status, match_id = "skip", "duplicate", exact["id"]
                else:
                    # Near-duplicate: shared actor + title word overlap ≥ 70%
                    actor = ent.get("actor","").lower()
                    words = set(ent.get("title","").lower().split())
                    near = s.run("""
                        MATCH (us:SysUserStory)
                        WHERE toLower(us.actor) = $actor
                        RETURN us.id AS id, us.title AS t
                        LIMIT 20
                    """, actor=actor).data()
                    for row in near:
                        row_words = set(row["t"].lower().split())
                        if row_words and words:
                            overlap = len(words & row_words) / len(words | row_words)
                            if overlap >= 0.70:
                                action, status, match_id = "review", "near-dup", row["id"]
                                break

            elif kind == "use-cases":
                slug = ent.get("name","")
                inst = ent.get("instance") or data.get("instance","")
                exact = s.run("""
                    MATCH (uc:SysUseCase)
                    WHERE uc.name = $name AND uc.instance = $inst
                    RETURN uc.id AS id LIMIT 1
                """, name=slug, inst=inst).single()
                if exact:
                    action, status, match_id = "skip", "duplicate", exact["id"]

            elif kind == "requirements":
                mod = ent.get("moduleId") or ent.get("_moduleHint","")
                exact = s.run("""
                    MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
                    WHERE (m.id=$mod OR m.name=$mod)
                      AND toLower(f.name) = toLower($name)
                    RETURN f.id AS id LIMIT 1
                """, mod=mod, name=ent.get("name","")).single()
                if exact:
                    action, status, match_id = "skip", "duplicate", exact["id"]

            elif kind == "architecture":
                if ent.get("_type") == "decision":
                    hint = ent.get("id_hint","")
                    if hint:
                        ex = s.run("MATCH (a:SysArchDecision {id:$id}) RETURN a.id AS id",
                                   id=hint).single()
                        if ex:
                            action, status, match_id = "update", "exists", ex["id"]
                else:
                    ex = s.run("""
                        MATCH (s:SysArchStd)
                        WHERE toLower(s.name) = toLower($n)
                        RETURN s.id AS id LIMIT 1
                    """, n=ent.get("name","")).single()
                    if ex:
                        action, status, match_id = "skip", "duplicate", ex["id"]

            warn = ""
            if ent.get("_ambiguous"):   warn = " [ambiguous]"
            if not ent.get("actor","") and kind == "stories" and action == "create": warn += " [no actor]"

            match_str = f"← {match_id}" if match_id else ""
            print(f"  {ref:<22} {title:<35} {status:<12} {action}{warn} {match_str}")
            decisions.append({"ref": ref, "action": action, "status": status,
                               "match_id": match_id, "entity": ent})

    drv.close()
    print(f"{'═'*W}")

    # Write decisions template
    dec_path = path.with_name(path.stem + "-decisions.json")
    dec_path.write_text(_json.dumps(decisions, indent=2, default=str), encoding="utf-8")
    print(f"\n  Review table written. Edit actions in: {dec_path}")
    print(f"  Then commit: sys_edge.py commit-import {path} --decisions {dec_path}")


def cmd_commit_import(args):
    """Apply accepted import decisions to the graph.

    Reads the original import JSON and the decisions JSON (from preview-import),
    then calls create commands for each accepted entity.
    """
    _require_licence("commit-import")
    import json as _json
    data_path = Path(args.file)
    dec_path  = Path(args.decisions)
    if not data_path.exists():
        print(f"ERROR: {data_path} not found", file=sys.stderr); sys.exit(1)
    if not dec_path.exists():
        print(f"ERROR: {dec_path} not found — run preview-import first", file=sys.stderr)
        sys.exit(1)

    data      = _json.loads(data_path.read_text(encoding="utf-8"))
    decisions = _json.loads(dec_path.read_text(encoding="utf-8"))
    kind      = data.get("kind","unknown")
    instance  = data.get("instance","")

    def _text(v):
        if isinstance(v, (list, tuple)):
            return "\n".join(str(x) for x in v if str(x).strip())
        return v or ""

    def _fields(kind_, ent):
        """Graph properties for an imported entity (empty values dropped)."""
        if kind_ == "stories":
            props = {"title": ent.get("title"), "actor": ent.get("actor"), "goal": ent.get("goal"),
                     "benefit": ent.get("benefit"), "narrative": ent.get("narrative"),
                     "priority": ent.get("priority"),
                     "acceptanceCriteria": ent.get("acceptance_criteria") or ent.get("acceptanceCriteria")}
        elif kind_ == "use-cases":
            props = {"title": ent.get("title"), "name": ent.get("name"), "actor": ent.get("actor"),
                     "description": ent.get("description"), "priority": ent.get("priority"),
                     "preconditions": _text(ent.get("preconditions")),
                     "mainFlow": _text(ent.get("main_flow") or ent.get("mainFlow")),
                     "alternateFlows": _text(ent.get("alternate_flows") or ent.get("alternateFlows")),
                     "postconditions": _text(ent.get("postconditions")),
                     "authorizedRoles": _text(ent.get("authorized_roles") or ent.get("authorizedRoles")),
                     "failureScenarios": _text(ent.get("failure_scenarios") or ent.get("failureScenarios"))}
        else:
            props = {"name": ent.get("name"), "description": ent.get("description")}
        return {k: v for k, v in props.items() if v not in (None, "", [])}

    labels = {"stories": ("SysUserStory", "US-IMPORT"), "use-cases": ("SysUseCase", "UC-IMPORT"),
              "requirements": ("SysFeature", "F-IMPORT")}
    if kind not in labels:
        print(f"ERROR: unknown import kind '{kind}'", file=sys.stderr); sys.exit(1)
    label, id_prefix = labels[kind]

    committed = skipped = failed = 0
    drv = _driver()
    with drv.session() as s:
        def exists(node_id):
            return bool(s.run(f"MATCH (n:{label} {{id:$id}}) RETURN n.id AS id", id=node_id).single())

        def next_free_id():
            n = 1
            while exists(f"{id_prefix}-{n:03d}"):
                n += 1
            return f"{id_prefix}-{n:03d}"

        for dec in decisions:
            action = dec.get("action", "skip")
            ent = dec.get("entity", {}) or {}
            ref = dec.get("ref", "?")
            if action not in ("create", "update", "merge-into"):
                skipped += 1  # skip / review: left for a person to decide
                continue
            try:
                props = _fields(kind, ent)
                if action == "create":
                    node_id = ent.get("id_hint") or next_free_id()
                    if exists(node_id):
                        print(f"  ⚠ {ref}: {node_id} already exists — not overwritten "
                              f"(use action 'update' or 'merge-into' with match_id)", file=sys.stderr)
                        skipped += 1
                        continue
                    if kind == "use-cases":
                        props.setdefault("name", node_id.lower())
                        props["instance"] = instance or ent.get("instance", "")
                    elif kind == "stories":
                        props["instance"] = instance
                        props.setdefault("priority", "P1")
                    else:
                        props["status"] = "Proposed"
                    s.run(f"CREATE (n:{label} {{id:$id}}) SET n += $props", id=node_id, props=props)
                else:  # update / merge-into an existing node: only provided fields change
                    node_id = dec.get("match_id") or ent.get("id_hint")
                    if not node_id or not exists(node_id):
                        print(f"  ✗ {ref}: '{action}' needs match_id of an existing {label}",
                              file=sys.stderr)
                        failed += 1
                        continue
                    s.run(f"MATCH (n:{label} {{id:$id}}) SET n += $props", id=node_id, props=props)

                if kind == "stories":
                    for fid in (ent.get("feature_refs") or []):
                        s.run("""MATCH (us:SysUserStory {id:$uid}),(f:SysFeature)
                                 WHERE f.id=$fid OR toLower(f.name)=toLower($fid)
                                 MERGE (us)-[:REQUIRES]->(f)""", uid=node_id, fid=fid)
                elif kind == "use-cases":
                    for sid in (ent.get("story_refs") or []):
                        s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                                 MERGE (us)-[:REALIZED_BY]->(uc)""", sid=sid, uid=node_id)
                    for fid in (ent.get("feature_refs") or []):
                        s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                                 MERGE (uc)-[:REQUIRES]->(f)""", uid=node_id, fid=fid)
                elif ent.get("moduleId"):
                    s.run("""MATCH (m:SysModule),(f:SysFeature {id:$fid})
                             WHERE m.id=$mod OR m.name=$mod
                             MERGE (m)-[:PROVIDES]->(f)""", fid=node_id, mod=ent["moduleId"])
                title = ent.get("title") or ent.get("name") or ""
                print(f"  ✓ {action:<10} {node_id}  {title[:50]}")
                committed += 1
            except Exception as e:
                print(f"  ✗ {ref}: {e}", file=sys.stderr)
                failed += 1
    drv.close()

    print(f"\n{'═'*60}")
    print(f"  committed={committed}  skipped={skipped}  failed={failed}")
    if failed:
        print(f"  ⚠  {failed} failures — re-run for unprocessed entities")


def cmd_create_usecase(args):
    """Create or update a SysUseCase node, optionally linking to stories and features.

    On CREATE: all fields required (--title mandatory).
    On MATCH (existing UC): only non-empty provided fields are updated; existing
    data is preserved. --title becomes optional for existing UCs.
    """
    drv = _driver()
    with drv.session() as s:
        if not args.instance:
            print("ERROR: --instance is required for create-usecase", file=sys.stderr)
            drv.close(); sys.exit(1)

        existing = s.run("MATCH (uc:SysUseCase {id:$id}) RETURN uc.title AS t",
                         id=args.id).single()

        if not existing:
            # New UC — --title required
            if not args.title:
                print("ERROR: --title is required when creating a new use case", file=sys.stderr)
                drv.close(); sys.exit(1)
            s.run("""
                CREATE (uc:SysUseCase {id:$id, name:$name, title:$title,
                    instance:$inst, description:$desc, priority:$prio,
                    preconditions:$precond, mainFlow:$main_flow,
                    postconditions:$postcond, authorizedRoles:$auth_roles,
                    failureScenarios:$fail_scenarios})
            """, id=args.id,
                 name=args.name or args.title.lower().replace(" ", "-"),
                 title=args.title, inst=args.instance,
                 desc=args.description or "", prio=args.priority or "P1",
                 precond=getattr(args, "preconditions", "") or "",
                 main_flow=getattr(args, "main_flow", "") or "",
                 postcond=getattr(args, "postconditions", "") or "",
                 auth_roles=getattr(args, "authorized_roles", "") or "",
                 fail_scenarios=getattr(args, "failure_scenarios", "") or "")
        else:
            # Existing UC — only update explicitly provided non-empty fields
            updates = {}
            if args.title:                                    updates["title"]           = args.title
            if args.name:                                     updates["name"]            = args.name
            if args.description:                              updates["description"]     = args.description
            if args.priority:                                 updates["priority"]        = args.priority
            if args.instance:                                 updates["instance"]        = args.instance
            if getattr(args, "preconditions",    ""):         updates["preconditions"]   = args.preconditions
            if getattr(args, "main_flow",        ""):         updates["mainFlow"]        = args.main_flow
            if getattr(args, "postconditions",   ""):         updates["postconditions"]  = args.postconditions
            if getattr(args, "authorized_roles", ""):         updates["authorizedRoles"] = args.authorized_roles
            if getattr(args, "failure_scenarios",""):         updates["failureScenarios"]= args.failure_scenarios
            if updates:
                set_clause = ", ".join(f"uc.{k}=${k}" for k in updates)
                s.run(f"MATCH (uc:SysUseCase {{id:$id}}) SET {set_clause}",
                      id=args.id, **updates)

        # Link to stories via REALIZED_BY (story→UC) — warn if story not found
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            if not s.run("MATCH (us:SysUserStory {id:$sid}) RETURN us.id", sid=sid).single():
                print(f"⚠  story '{sid}' not found — skipping link", file=sys.stderr)
                continue
            s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                     MERGE (us)-[:REALIZED_BY]->(uc)""", sid=sid, uid=args.id)

        # Link to features via REQUIRES (UC→feature)
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                     MERGE (uc)-[:REQUIRES]->(f)""", uid=args.id, fid=fid)

        # DEF-1392: schema contract check — warn about non-standard UC properties
        _UC_KNOWN_FIELDS = {
            "id", "name", "title", "instance", "description", "priority",
            "preconditions", "mainFlow", "postconditions", "authorizedRoles",
            "failureScenarios", "auditTestLastRun", "auditTestResult",
            "coverageReviewLastRun", "coverageReviewResult", "auditTestFailStreak",
        }
        uc_props = s.run("MATCH (uc:SysUseCase {id:$id}) RETURN keys(uc) AS k",
                         id=args.id).single()
        if uc_props:
            unknown = sorted(set(uc_props["k"]) - _UC_KNOWN_FIELDS)
            if unknown:
                print(f"  ⚠  Non-standard UC fields detected: {', '.join(unknown)}", file=sys.stderr)
                print(f"     Known contract fields: {', '.join(sorted(_UC_KNOWN_FIELDS - {'auditTestLastRun','auditTestResult','coverageReviewLastRun','coverageReviewResult','auditTestFailStreak'}))}",
                      file=sys.stderr)
                print(f"     These won't appear in audit-test or coverage-review outputs.", file=sys.stderr)

    drv.close()
    title_display = args.title or existing["t"] if existing else args.title
    print(f"✓ {args.id}  [{args.instance}]  {title_display}")


def cmd_create_feature(args):
    """Create a SysFeature node linked to a module."""
    drv = _driver()
    with drv.session() as s:
        if not args.module:
            print("ERROR: --module is required for create-feature", file=sys.stderr)
            drv.close(); sys.exit(1)
        if not s.run("MATCH (m:SysModule {id:$id}) RETURN m.id", id=args.module).single():
            print(f"ERROR: module '{args.module}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""
            MERGE (f:SysFeature {id:$id})
            SET f.name=$name, f.status=$status,
                f.description=$desc, f.narrative=$narr,
                f.source=$src
        """, id=args.id, name=args.name, status=args.status or "Proposed",
             desc=args.description or "", narr=args.narrative or "",
             src=args.source or "manual")
        s.run("""MATCH (m:SysModule {id:$mid}),(f:SysFeature {id:$fid})
                 MERGE (m)-[:PROVIDES]->(f)""", mid=args.module, fid=args.id)
        for sid in [s.strip() for s in (args.story or "").split(",") if s.strip()]:
            s.run("""MATCH (us:SysUserStory {id:$sid}),(f:SysFeature {id:$fid})
                     MERGE (us)-[:REQUIRES]->(f)""", sid=sid, fid=args.id)
    drv.close()
    print(f"✓ {args.id}  [{args.module}]  {args.name}")


def cmd_create_adr(args):
    """Create a SysArchDecision (ADR) node."""
    _require_licence("create-adr")
    drv = _driver()
    with drv.session() as s:
        s.run("""
            MERGE (a:SysArchDecision {id:$id})
            SET a.title=$title, a.status=$status, a.date=$date,
                a.decision=$dec, a.context=$ctx, a.consequences=$cons
        """, id=args.id, title=args.title, status=args.status or "proposed",
             date=args.date or _now_iso()[:10],
             dec=args.decision or getattr(args, "description", "") or "",
             ctx=args.context or "", cons=args.consequences or "")
        sys_node = s.run("MATCH (sys:SysSystem) RETURN sys.id AS id LIMIT 1").single()
        if sys_node:
            s.run("MATCH (sys:SysSystem {id:$sid}),(a:SysArchDecision {id:$aid}) "
                  "MERGE (sys)-[:GOVERNED_BY]->(a)", sid=sys_node["id"], aid=args.id)
        for std_id in [x.strip() for x in (args.addresses or "").split(",") if x.strip()]:
            s.run("""MATCH (adr:SysArchDecision {id:$aid}),(std:SysArchStd {id:$sid})
                     MERGE (adr)-[:ADDRESSES]->(std)""", aid=args.id, sid=std_id)
        for sup in [x.strip() for x in (args.supersedes or "").split(",") if x.strip()]:
            s.run("""MATCH (new:SysArchDecision {id:$nid}),(old:SysArchDecision {id:$oid})
                     MERGE (new)-[:SUPERSEDES]->(old)
                     SET old.status='superseded'""", nid=args.id, oid=sup)
    drv.close()
    print(f"✓ {args.id}  [{args.status or 'proposed'}]  {args.title}")


_VALID_SYS_LABELS = {label for _, label, _ in CONSTRAINTS}


def cmd_merge_nodes(args):
    """Transfer all edges from --absorb to --keep, then delete --absorb.

    Properties: scalars use --strategy (union copies list fields, prefer-keep
    keeps keep's value for scalars, prefer-absorb copies absorb's non-null scalars).
    ADRs always use prefer-keep for scalar fields.
    """
    _require_licence("merge-nodes")
    if args.type not in _VALID_SYS_LABELS:
        print(f"ERROR: unknown node type '{args.type}'. Valid types: {sorted(_VALID_SYS_LABELS)}",
              file=sys.stderr)
        sys.exit(1)
    drv = _driver(admin=True)
    with drv.session() as s:
        keep_row = s.run(f"MATCH (n:{args.type} {{id:$id}}) RETURN n {{.*}} AS n",
                         id=args.keep).single()
        absorb_row = s.run(f"MATCH (n:{args.type} {{id:$id}}) RETURN n {{.*}} AS n",
                           id=args.absorb).single()
        if not keep_row:
            print(f"ERROR: keep node '{args.keep}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        if not absorb_row:
            print(f"ERROR: absorb node '{args.absorb}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        if args.keep == args.absorb:
            print("ERROR: --keep and --absorb must be different", file=sys.stderr)
            drv.close(); sys.exit(1)

        strategy = args.strategy or "union"
        # ADRs are always prefer-keep for safety
        if args.type == "SysArchDecision":
            strategy = "prefer-keep"

        # Re-point incoming edges from absorb → keep, preserving relationship properties
        in_rels = s.run(f"""
            MATCH (src)-[r]->(absorb:{args.type} {{id:$id}})
            RETURN src.id AS src_id, type(r) AS rt, properties(r) AS rprops
        """, id=args.absorb).data()
        for rel in in_rels:
            src_id, rt, rprops = rel["src_id"], rel["rt"], rel["rprops"] or {}
            if not _REL_TYPE_RE.match(rt or ""):
                print(f"  ⚠  skipping relationship with unexpected type {rt!r}", file=sys.stderr)
                continue
            exists = s.run(f"""
                MATCH (src {{id:$sid}})-[r:{rt}]->(keep:{args.type} {{id:$kid}})
                RETURN count(r) AS n
            """, sid=src_id, kid=args.keep).single()["n"]
            if not exists:
                # Properties go in as one map parameter: stored keys are never
                # interpolated into Cypher.
                s.run(f"""
                    MATCH (src {{id:$sid}})-[r:{rt}]->(absorb:{args.type} {{id:$absorb_id}})
                    MATCH (keep:{args.type} {{id:$keep_id}})
                    CREATE (src)-[new:{rt}]->(keep) SET new += $rprops
                    DELETE r
                """, sid=src_id, absorb_id=args.absorb, keep_id=args.keep, rprops=rprops)
            else:
                s.run(f"""
                    MATCH (src {{id:$sid}})-[r:{rt}]->(absorb:{args.type} {{id:$absorb_id}})
                    DELETE r
                """, sid=src_id, absorb_id=args.absorb)

        # Re-point outgoing edges from absorb → keep, preserving relationship properties
        out_rels = s.run(f"""
            MATCH (absorb:{args.type} {{id:$id}})-[r]->(tgt)
            RETURN tgt.id AS tgt_id, type(r) AS rt, properties(r) AS rprops
        """, id=args.absorb).data()
        for rel in out_rels:
            tgt_id, rt, rprops = rel["tgt_id"], rel["rt"], rel["rprops"] or {}
            if not _REL_TYPE_RE.match(rt or ""):
                print(f"  ⚠  skipping relationship with unexpected type {rt!r}", file=sys.stderr)
                continue
            exists = s.run(f"""
                MATCH (keep:{args.type} {{id:$kid}})-[r:{rt}]->(tgt {{id:$tid}})
                RETURN count(r) AS n
            """, kid=args.keep, tid=tgt_id).single()["n"]
            if not exists:
                s.run(f"""
                    MATCH (absorb:{args.type} {{id:$absorb_id}})-[r:{rt}]->(tgt {{id:$tid}})
                    MATCH (keep:{args.type} {{id:$keep_id}})
                    CREATE (keep)-[new:{rt}]->(tgt) SET new += $rprops
                    DELETE r
                """, absorb_id=args.absorb, keep_id=args.keep, tid=tgt_id, rprops=rprops)
            else:
                s.run(f"""
                    MATCH (absorb:{args.type} {{id:$absorb_id}})-[r:{rt}]->(tgt {{id:$tid}})
                    DELETE r
                """, absorb_id=args.absorb, tid=tgt_id)

        # Property merge
        absorb_props = dict(absorb_row["n"])
        keep_props = dict(keep_row["n"])
        merged = {}
        if strategy == "union":
            for k, v in absorb_props.items():
                if k == "id": continue
                if k not in keep_props or keep_props[k] is None:
                    merged[k] = v
        elif strategy == "prefer-absorb":
            for k, v in absorb_props.items():
                if k == "id": continue
                if v is not None:
                    merged[k] = v
        # prefer-keep: no property updates needed
        if merged:
            s.run(f"MATCH (n:{args.type} {{id:$id}}) SET n += $merged",
                  id=args.keep, merged=merged)

        # Delete the absorbed node
        s.run(f"MATCH (n:{args.type} {{id:$id}}) DETACH DELETE n", id=args.absorb)

    drv.close()
    print(f"✓ {args.absorb} merged into {args.keep}  [{args.type}]  strategy={strategy or 'union'}")


def cmd_analyse(args):
    """Analyse the graph for merge candidates, split candidates, or orphans."""
    _require_licence("analyse")
    as_json = getattr(args, "json", False)
    if as_json:
        # Same queries, machine-readable result: text output is discarded.
        import contextlib as _ctx, io as _io
        result = {"orphans": {}, "merge": [], "split": []}
        with _ctx.redirect_stdout(_io.StringIO()):
            _analyse(args, result)
        print(json.dumps(result, indent=2, default=str))
        return
    _analyse(args, None)


def _analyse(args, result):
    """Body of cmd_analyse; fills `result` (when given) for --json."""
    drv = _driver()
    instance = getattr(args, "instance", "") or ""
    W = 62

    with drv.session() as s:
        mod_ids = []
        if instance:
            mod_ids = [r["id"] for r in s.run(
                "MATCH (m:SysModule {instance:$inst}) RETURN m.id AS id",
                inst=instance).data()]

        # ── ORPHANS ──────────────────────────────────────────────────────────
        if args.orphans or (not args.merge and not args.split):
            print(f"\n{'─'*W}\n  ORPHANS\n{'─'*W}")
            orphan_counts = {}

            inst_uc   = " AND uc.instance=$inst" if instance else ""
            inst_mod  = " AND m.instance=$inst"  if instance else ""
            inst_feat = (" AND EXISTS { MATCH (:SysModule {instance:$inst})-[:PROVIDES]->(f) }"
                         if instance else "")
            checks = [
                ("Stories without UC",
                 "MATCH (us:SysUserStory) WHERE NOT (us)-[:REALIZED_BY]->() RETURN us.id AS id, us.title AS title"),
                ("Use cases without features",
                 f"MATCH (uc:SysUseCase) WHERE NOT (uc)-[:REQUIRES]->(:SysFeature){inst_uc} RETURN uc.id AS id, uc.instance AS title"),
                ("Features without module",
                 f"MATCH (f:SysFeature) WHERE NOT ()-[:PROVIDES]->(f) AND NOT f.status='Superseded'{inst_feat} RETURN f.id AS id, f.name AS title"),
                ("Modules without features",
                 f"MATCH (m:SysModule) WHERE NOT (m)-[:PROVIDES]->(){inst_mod} RETURN m.id AS id, m.name AS title"),
                ("ADRs without addressed standards",
                 "MATCH (a:SysArchDecision) WHERE NOT (a)-[:ADDRESSES]->() RETURN a.id AS id, a.title AS title"),
                ("Architecture standards with no addressing decision",
                 "MATCH (std:SysArchStd) WHERE NOT ()-[:ADDRESSES]->(std) "
                 "AND NOT std.id STARTS WITH 'AS-' "
                 "RETURN std.id AS id, coalesce(std.name, std.title) AS title ORDER BY std.id"),
            ]
            if _lightweight():
                checks = [c for c in checks if c[0] != "Stories without UC"]
            total_orphans = 0
            for label, query in checks:
                rows = s.run(query, inst=instance).data()
                if result is not None:
                    result["orphans"][label] = [{"id": r["id"], "title": r.get("title")} for r in rows]
                if rows:
                    print(f"\n  {label} ({len(rows)}):")
                    for r in rows[:8]:
                        print(f"    {r['id']:<20} {(r.get('title') or '')[:45]}")
                    if len(rows) > 8:
                        print(f"    … and {len(rows)-8} more")
                    total_orphans += len(rows)
                else:
                    print(f"  ✓ {label}: none")
            print(f"\n  Total orphans: {total_orphans}")

        # ── MERGE CANDIDATES ─────────────────────────────────────────────────
        if args.merge:
            threshold = getattr(args, "threshold", 0.80) or 0.80
            print(f"\n{'─'*W}\n  MERGE CANDIDATES  (threshold {threshold})\n{'─'*W}")
            print(f"  Note: similarity scored by Claude reasoning on names + descriptions.")
            print(f"  Run: sys_edge.py merge-nodes --type <T> --keep <id> --absorb <id>\n")

            for node_type, id_prefix in [("SysFeature","F-"),("SysUseCase","UC-"),("SysUserStory","US-")]:
                inst_filter = "AND m.instance=$inst" if instance else ""
                if node_type == "SysFeature":
                    rows = s.run(f"""
                        MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
                        WHERE 1=1 {inst_filter}
                        RETURN f.id AS id, f.name AS name,
                               coalesce(f.description,'') AS desc
                        ORDER BY f.id
                    """, inst=instance).data()
                elif node_type == "SysUseCase":
                    rows = s.run(f"""
                        MATCH (uc:SysUseCase)
                        WHERE ($inst='' OR uc.instance=$inst)
                        RETURN uc.id AS id,
                               coalesce(uc.title,uc.name,'') AS name,
                               coalesce(uc.description,'') AS desc
                        ORDER BY uc.id
                    """, inst=instance).data()
                else:
                    rows = s.run("""
                        MATCH (us:SysUserStory)
                        RETURN us.id AS id, us.title AS name,
                               coalesce(us.goal,'') AS desc
                        ORDER BY us.id
                    """).data()

                if len(rows) < 2:
                    continue
                print(f"\n  {node_type} ({len(rows)} nodes) — review pairs with similar names:")
                for i, a in enumerate(rows):
                    for b in rows[i+1:]:
                        # Simple token-overlap heuristic (no embeddings needed)
                        a_tokens = set(a["name"].lower().split())
                        b_tokens = set(b["name"].lower().split())
                        if not a_tokens or not b_tokens:
                            continue
                        overlap = len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
                        if overlap >= threshold:
                            if result is not None:
                                result["merge"].append({
                                    "type": node_type, "overlap": round(overlap, 3),
                                    "a": {"id": a["id"], "name": a["name"], "description": a["desc"]},
                                    "b": {"id": b["id"], "name": b["name"], "description": b["desc"]},
                                })
                            print(f"    {a['id']:<22} ←→  {b['id']:<22}  overlap={overlap:.2f}")
                            print(f"      '{a['name'][:50]}'")
                            print(f"      '{b['name'][:50]}'")
                            print(f"      merge-nodes --type {node_type} --keep {a['id']} --absorb {b['id']}")

        # ── SPLIT CANDIDATES ─────────────────────────────────────────────────
        if args.split:
            max_r = getattr(args, "max_responsibilities", 3) or 3
            print(f"\n{'─'*W}\n  SPLIT CANDIDATES  (> {max_r} unrelated UCs)\n{'─'*W}")
            inst_filter = "AND m.instance=$inst" if instance else ""
            rows = s.run(f"""
                MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
                WHERE 1=1 {inst_filter}
                MATCH (uc:SysUseCase)-[:REQUIRES]->(f)
                WITH f, count(DISTINCT uc) AS uc_count
                WHERE uc_count > $max_r
                RETURN f.id AS id, f.name AS name, uc_count
                ORDER BY uc_count DESC
            """, inst=instance, max_r=max_r).data()
            if rows:
                for r in rows:
                    print(f"\n  {r['id']:<20} {r['name'][:40]}  ({r['uc_count']} UCs)")
                    uc_rows = s.run("""
                        MATCH (uc:SysUseCase)-[:REQUIRES]->(f:SysFeature {id:$fid})
                        RETURN uc.id AS id, coalesce(uc.title,uc.name,'') AS title
                        ORDER BY uc.id
                    """, fid=r["id"]).data()
                    if result is not None:
                        result["split"].append({"id": r["id"], "name": r["name"],
                                                "uc_count": r["uc_count"], "use_cases": uc_rows})
                    for uc in uc_rows:
                        print(f"    → {uc['id']:<22} {uc['title'][:45]}")
                    print(f"    Suggest: split into {min(r['uc_count']//2+1, 3)} features, re-attach UCs manually")
            else:
                print(f"  ✓ No features with > {max_r} use cases")

    drv.close()


# ── Export: detailed design ──────────────────────────────────────────────────
# Premium helpers for `export --type detailed-design`. make_lite.py stubs every
# top-level `_export_*` function out of the free CLI alongside cmd_export.

_DD_INACTIVE = ("Superseded", "Deprecated")


def _export_dd_collect(s):
    """Read every node and edge the detailed-design export needs (read-only)."""
    return {
        "modules": s.run("MATCH (m:SysModule) RETURN m {.*} AS m ORDER BY m.id").data(),
        "features": s.run("""
            MATCH (f:SysFeature)
            OPTIONAL MATCH (m:SysModule)-[:PROVIDES]->(f)
            RETURN f {.*} AS f, collect(DISTINCT m.id) AS mods
            ORDER BY f.id
        """).data(),
        "usecases": s.run("""
            MATCH (uc:SysUseCase)
            OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
            RETURN uc {.*} AS uc, collect(DISTINCT f.id) AS feats
            ORDER BY uc.id
        """).data(),
        "stories": s.run("""
            MATCH (us:SysUserStory)
            OPTIONAL MATCH (us)-[:REALIZED_BY]->(uc:SysUseCase)
            OPTIONAL MATCH (us)-[:REQUIRES]->(f:SysFeature)
            RETURN us {.*} AS us, collect(DISTINCT uc.id) AS ucs, collect(DISTINCT f.id) AS feats
            ORDER BY us.id
        """).data(),
        "tests": s.run("""
            MATCH (t:SysTest)
            OPTIONAL MATCH (t)-[:VERIFIES]->(f:SysFeature)
            OPTIONAL MATCH (p:SysTestPackage)-[:CONTAINS_TEST]->(t)
            RETURN t {.*} AS t, collect(DISTINCT f.id) AS feats,
                   head(collect(p.testCategory)) AS pkgCat, head(collect(p.area)) AS pkgArea
            ORDER BY t.id
        """).data(),
    }


def _export_dd_list(value):
    """Normalise a list-or-text property (flows, criteria, roles) to a list of strings."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except ValueError:
            pass
    items = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if line:
            items.append(line)
    return items


def _export_dd_cell(value):
    """Make a value safe for a Markdown table cell."""
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _export_dd_anchor(node_id):
    return re.sub(r"[^a-z0-9-]", "-", str(node_id).lower())


def _export_dd_ref(node_id):
    return f"[{node_id}](#{_export_dd_anchor(node_id)})"


def _export_dd_tier(t):
    return t.get("testType") or t.get("_pkgCat") or "unclassified"


def _export_dd_scope(raw, instance="", module_id="", resolve=None):
    """Index the raw graph rows and select the slice in scope.

    Unscoped exports include every node. A module or instance scope starts from
    its modules' features and pulls in the use cases, stories and tests linked
    to them, plus nodes carrying a matching `instance` property so orphans in
    that scope are still reported.

    Each test's file is checked with `resolve` (default _resolve_test_file).
    Tests that do not resolve to a file in this repo are moved to
    `unresolved_tests` so they never count as coverage.
    """
    resolve = resolve or _resolve_test_file
    mods = {r["m"]["id"]: r["m"] for r in raw["modules"]}
    feats, ucs, stories, tests = {}, {}, {}, {}
    for r in raw["features"]:
        feats[r["f"]["id"]] = dict(r["f"], _mods=sorted(r["mods"] or []))
    for r in raw["usecases"]:
        ucs[r["uc"]["id"]] = dict(r["uc"], _feats=sorted(r["feats"] or []))
    for r in raw["stories"]:
        stories[r["us"]["id"]] = dict(r["us"], _ucs=sorted(r["ucs"] or []),
                                      _feats=sorted(r["feats"] or []))
    for r in raw["tests"]:
        tests[r["t"]["id"]] = dict(r["t"], _feats=sorted(r["feats"] or []),
                                   _pkgCat=r.get("pkgCat"), _pkgArea=r.get("pkgArea"))

    scoped = bool(module_id or instance)
    if module_id:
        mod_ids = {module_id} & set(mods)
    elif instance:
        mod_ids = {m for m, n in mods.items() if n.get("instance") == instance}
    else:
        mod_ids = set(mods)

    def in_inst(node):
        return bool(instance) and not module_id and node.get("instance") == instance

    if scoped:
        f_ids = {f for f, n in feats.items() if set(n["_mods"]) & mod_ids or in_inst(n)}
        uc_ids = {u for u, n in ucs.items() if set(n["_feats"]) & f_ids or in_inst(n)}
        us_ids = {s for s, n in stories.items()
                  if set(n["_ucs"]) & uc_ids or set(n["_feats"]) & f_ids or in_inst(n)}
        t_ids = {t for t, n in tests.items()
                 if set(n["_feats"]) & f_ids
                 or (not n["_feats"] and in_inst({"instance": n.get("_pkgArea")}))}
    else:
        f_ids, uc_ids, us_ids, t_ids = set(feats), set(ucs), set(stories), set(tests)

    resolved, unresolved, unlinked_unresolved = {}, {}, 0
    for k in sorted(t_ids):
        t = tests[k]
        status, path = resolve(_test_file_of(t), t.get("repo") or "")
        t["_link"], t["_path"] = status, path
        if status in ("ok", "fixed"):
            resolved[k] = t
        elif t["_feats"]:
            unresolved[k] = t
        else:
            unlinked_unresolved += 1  # stale scan leftovers: counted, not listed

    return {
        "modules":  {k: mods[k] for k in sorted(mod_ids)},
        "features": {k: feats[k] for k in sorted(f_ids)},
        "usecases": {k: ucs[k] for k in sorted(uc_ids)},
        "stories":  {k: stories[k] for k in sorted(us_ids)},
        "tests":    resolved,
        "unresolved_tests": unresolved,
        "unlinked_unresolved": unlinked_unresolved,
    }


def _export_dd_analyse(d):
    """Derive reverse links, orphans and coverage gaps from a scoped slice."""
    feats, ucs, stories, tests = d["features"], d["usecases"], d["stories"], d["tests"]
    uc_stories, f_ucs, f_stories, f_tests = {}, {}, {}, {}
    for sid, us in stories.items():
        for u in us["_ucs"]:
            uc_stories.setdefault(u, []).append(sid)
        for f in us["_feats"]:
            f_stories.setdefault(f, []).append(sid)
    for uid, uc in ucs.items():
        for f in uc["_feats"]:
            f_ucs.setdefault(f, []).append(uid)
    for tid, t in tests.items():
        for f in t["_feats"]:
            f_tests.setdefault(f, []).append(tid)

    def active(f):
        return (feats[f].get("status") or "") not in _DD_INACTIVE

    orphans = {
        "stories":  [s for s, n in stories.items() if not n["_ucs"] and not n["_feats"]],
        "usecases": [u for u, n in ucs.items() if not n["_feats"]],
        "features": [f for f, n in feats.items() if not n["_mods"]],
        "tests":    [t for t, n in tests.items() if not n["_feats"]],
    }
    light = _lightweight()
    gaps = {
        "features_without_tests": [f for f in feats if active(f) and not f_tests.get(f)],
        "features_without_usecases": [] if light else [f for f in feats if active(f) and not f_ucs.get(f)],
        "usecases_without_stories": [] if light else [u for u in ucs if ucs[u]["_feats"] and not uc_stories.get(u)],
        "stories_without_usecases": [] if light else [s for s, n in stories.items() if not n["_ucs"] and n["_feats"]],
    }
    return {"uc_stories": uc_stories, "f_ucs": f_ucs, "f_stories": f_stories,
            "f_tests": f_tests, "orphans": orphans, "gaps": gaps}


def _export_dd_render(d, scope, today):
    """Render the detailed-design document. Returns (lines, structured_data)."""
    a = _export_dd_analyse(d)
    mods, feats, ucs, stories, tests = (d["modules"], d["features"], d["usecases"],
                                        d["stories"], d["tests"])
    orphan_ids = {i for ids in a["orphans"].values() for i in ids}
    rendered = set()
    out = []
    w = out.append

    def mark(node_id):
        return "  ⚠ **ORPHAN**" if node_id in orphan_ids else ""

    def anchor(node_id):
        return f'<a id="{_export_dd_anchor(node_id)}"></a>\n'

    def bullets(title, items, numbered=False):
        if not items:
            return
        w(f"\n**{title}:**\n\n")
        for n, item in enumerate(items, 1):
            w(f"{n}. {item}\n" if numbered else f"- {item}\n")

    def render_story(sid, level):
        us = stories[sid]
        if sid in rendered:
            w(f"- {_export_dd_ref(sid)} — {us.get('title', '?')} *(detailed above)*\n")
            return
        rendered.add(sid)
        w(f"\n{anchor(sid)}{'#' * level} {sid} — {us.get('title', '?')}{mark(sid)}\n\n")
        w(f"**Priority:** {us.get('priority') or '?'}  \n")
        if us.get("actor") or us.get("goal"):
            line = f"As a **{us.get('actor') or '?'}**, I want {us.get('goal') or '?'}"
            w(line + (f", so that {us['benefit']}.  \n" if us.get("benefit") else ".  \n"))
        if us.get("narrative"):
            w(f"\n{us['narrative']}\n")
        if us.get("description"):
            w(f"\n{us['description']}\n")
        bullets("Acceptance criteria", _export_dd_list(us.get("acceptanceCriteria")), numbered=True)
        if us["_ucs"]:
            w(f"\n**Realised by:** {', '.join(_export_dd_ref(u) for u in us['_ucs'])}  \n")
        if us["_feats"]:
            w(f"**Requires features:** {', '.join(_export_dd_ref(f) for f in us['_feats'])}  \n")

    def render_uc(uid, level):
        uc = ucs[uid]
        title = uc.get("title") or uc.get("name") or "?"
        if uid in rendered:
            w(f"\n- {_export_dd_ref(uid)} — {title} *(detailed above)*\n")
            return
        rendered.add(uid)
        w(f"\n{anchor(uid)}{'#' * level} {uid} — {title}{mark(uid)}\n\n")
        w(f"**Instance:** {uc.get('instance') or '?'}  **Priority:** {uc.get('priority') or '?'}  \n")
        roles = _export_dd_list(uc.get("authorizedRoles"))
        if roles:
            w(f"**Authorised roles:** {', '.join(roles)}  \n")
        if len(uc["_feats"]) > 1:
            w(f"**Requires features:** {', '.join(_export_dd_ref(f) for f in uc['_feats'])}  \n")
        if uc.get("description"):
            w(f"\n{uc['description']}\n")
        bullets("Preconditions", _export_dd_list(uc.get("preconditions") or uc.get("precondition")))
        bullets("Main flow", _export_dd_list(uc.get("mainFlow") or uc.get("main_flow")), numbered=True)
        bullets("Alternate flows", _export_dd_list(uc.get("alternateFlows") or uc.get("alternate_flows")))
        bullets("Postconditions", _export_dd_list(uc.get("postconditions")))
        bullets("Failure scenarios", _export_dd_list(uc.get("failureScenarios")))
        sids = a["uc_stories"].get(uid, [])
        if sids:
            w(f"\n**User stories realised ({len(sids)}):**\n")
            for sid in sids:
                render_story(sid, min(level + 1, 6))
        else:
            w("\n⚠ *No user story realises this use case.*\n")

    def render_feature(fid, level):
        f = feats[fid]
        rendered.add(fid)
        w(f"\n{anchor(fid)}{'#' * level} {fid} — {f.get('name') or f.get('title') or '?'}{mark(fid)}\n\n")
        w(f"**Status:** {f.get('status') or '?'}")
        if len(f["_mods"]) > 1:
            w(f"  **Modules:** {', '.join(f['_mods'])}")
        w("  \n")
        if f.get("description"):
            w(f"\n{f['description']}\n")
        tids = a["f_tests"].get(fid, [])
        if tids:
            by_file = {}
            for tid in tids:
                t = tests[tid]
                by_file.setdefault(t.get("_path") or t.get("file") or "—", []).append(t)
            files = f"{len(by_file)} file" + ("s" if len(by_file) != 1 else "")
            w(f"\n**Verified by tests ({len(tids)} in {files}):**\n\n")
            w("| File | Tier | Tests | Last status | Last tested | Functions |\n")
            w("|---|---|---|---|---|---|\n")
            for path, ts in sorted(by_file.items()):
                tiers = ", ".join(sorted({_export_dd_tier(t) for t in ts}))
                status = collections.Counter(t.get("lastStatus") or "not run" for t in ts)
                last = max((str(t.get("lastTestedAt") or "")[:10] for t in ts), default="")
                fns = [str(t.get("function") or "?") for t in ts]
                shown = ", ".join(fns[:8]) + (f", … +{len(fns) - 8}" if len(fns) > 8 else "")
                w(f"| {_export_dd_cell(path)} | {_export_dd_cell(tiers)} | {len(ts)} "
                  f"| {_export_dd_cell(', '.join(f'{n} {k}' for k, n in status.most_common()))} "
                  f"| {_export_dd_cell(last)} | {_export_dd_cell(shown)} |\n")
        elif (f.get("status") or "") not in _DD_INACTIVE:
            w("\n⚠ *No tests verify this feature.*\n")
        uids = a["f_ucs"].get(fid, [])
        if uids:
            w(f"\n**Use cases ({len(uids)}):**\n")
            for uid in uids:
                render_uc(uid, min(level + 1, 6))
        direct = [sid for sid in a["f_stories"].get(fid, []) if not stories[sid]["_ucs"]]
        if direct:
            w(f"\n**User stories linked directly (no use case) ({len(direct)}):**\n")
            for sid in direct:
                render_story(sid, min(level + 1, 6))

    # Header + summary
    w(f"# Detailed Design — {scope}  ({today})\n\n")
    w("> Generated from the SysEdge graph. Traceability chain: "
      "User Story →REALIZED_BY→ Use Case →REQUIRES→ Feature ←VERIFIES← Test, "
      "with Modules →PROVIDES→ Features.\n\n")
    unresolved = d.get("unresolved_tests", {})
    fixed_paths = sum(1 for t in tests.values() if t.get("_link") == "fixed")
    w("## Summary\n\n| Modules | Features | Use cases | User stories | Tests | Unresolved tests | Orphans |\n")
    w("|---|---|---|---|---|---|---|\n")
    w(f"| {len(mods)} | {len(feats)} | {len(ucs)} | {len(stories)} | {len(tests)} "
      f"| {len(unresolved)} | {len(orphan_ids)} |\n\n")
    w("Contents: 1. Design by Module · 2. Traceability Matrix · 3. Coverage Gaps · "
      "4. Test Link Integrity · 5. Orphan Nodes\n")

    # 1. Nested design
    w("\n## 1. Design by Module\n")
    if not mods:
        w("\n(no entries)\n")
    for mid, m in mods.items():
        w(f"\n{anchor(mid)}### {mid} — {m.get('name') or '?'}\n\n")
        w(f"**Instance:** {m.get('instance') or '?'}  **Type:** {m.get('type') or '?'}  \n")
        if m.get("paths"):
            w(f"**Paths:** `{'`, `'.join(_export_dd_list(m['paths']))}`  \n")
        if m.get("description"):
            w(f"\n{m['description']}\n")
        mod_feats = [fid for fid, f in feats.items() if mid in f["_mods"]]
        if not mod_feats:
            w("\n(no features)\n")
        for fid in mod_feats:
            if fid in rendered:
                w(f"\n- {_export_dd_ref(fid)} *(detailed above)*\n")
            else:
                render_feature(fid, 4)

    # 2. Traceability matrix
    w("\n## 2. Traceability Matrix\n\n")
    w("| User Story | Use Case | Feature | Module | Tests | Tiers |\n|---|---|---|---|---|---|\n")
    rows = []

    def feature_cells(fid):
        if fid not in feats:
            return [_export_dd_cell(fid), "—", "—", "—"]
        tids = a["f_tests"].get(fid, [])
        tiers = sorted({_export_dd_tier(tests[t]) for t in tids})
        return [_export_dd_ref(fid), _export_dd_cell(", ".join(feats[fid]["_mods"])),
                str(len(tids)) if tids else "⚠ 0", _export_dd_cell(", ".join(tiers))]

    for sid, us in stories.items():
        paths = []
        for uid in us["_ucs"]:
            uc_feats = ucs[uid]["_feats"] if uid in ucs else []
            for fid in uc_feats or [None]:
                paths.append((uid, fid))
        for fid in us["_feats"]:
            if not any(p[1] == fid for p in paths):
                paths.append((None, fid))
        for uid, fid in paths or [(None, None)]:
            rows.append([_export_dd_ref(sid) + (" ⚠" if sid in orphan_ids else ""),
                         _export_dd_ref(uid) if uid else "—"]
                        + (feature_cells(fid) if fid else ["—", "—", "—", "—"]))
    for uid, uc in ucs.items():
        if a["uc_stories"].get(uid):
            continue
        for fid in uc["_feats"] or [None]:
            rows.append(["—", _export_dd_ref(uid) + (" ⚠" if uid in orphan_ids else "")]
                        + (feature_cells(fid) if fid else ["—", "—", "—", "—"]))
    for fid in feats:
        if not a["f_ucs"].get(fid) and not a["f_stories"].get(fid):
            rows.append(["—", "—"] + feature_cells(fid))
    for r in rows:
        w("| " + " | ".join(r) + " |\n")
    if not rows:
        w("| (no entries) | | | | | |\n")
    w("\n⚠ marks an orphan node or a feature with no verifying tests.\n")

    # 3. Coverage gaps
    w("\n## 3. Coverage Gaps\n")
    gap_titles = {
        "features_without_tests": ("Active features with no tests", feats, "name"),
        "features_without_usecases": ("Active features with no use cases", feats, "name"),
        "usecases_without_stories": ("Use cases not realising any user story", ucs, "title"),
        "stories_without_usecases": ("User stories linked to features but no use case", stories, "title"),
    }
    for key, (title, nodes, field) in gap_titles.items():
        ids = a["gaps"][key]
        w(f"\n### {title} ({len(ids)})\n\n")
        if not ids:
            w("(none)\n")
            continue
        for i in ids:
            n = nodes[i]
            w(f"- {_export_dd_ref(i)} — {n.get(field) or n.get('name') or n.get('title') or '?'}\n")

    # 4. Test link integrity
    w("\n## 4. Test Link Integrity\n\n")
    linked_ok = sum(1 for t in tests.values() if t["_feats"])
    w(f"{linked_ok} linked tests resolve to a file in this repo"
      + (f" ({fixed_paths} only after path normalisation)" if fixed_paths else "")
      + f"; {len(unresolved)} do not and are excluded from coverage above.\n")
    if d.get("unlinked_unresolved"):
        w(f"\nA further {d['unlinked_unresolved']} unlinked test nodes point at files not in this repo.\n")
    if fixed_paths or unresolved:
        w("\nRepair the graph with `sys_edge.py check-test-links` (dry run), then `--fix`.\n")
    status_titles = (("foreign", "Belong to another repo"),
                     ("missing", "File not found in this repo (deleted, moved, or scanned from another repo)"),
                     ("nofile", "No file recorded"))
    for status, title in status_titles:
        ids = [k for k, t in unresolved.items() if t["_link"] == status]
        if not ids:
            continue
        w(f"\n### {title} ({len(ids)})\n\n| Test | Stored path | Linked to |\n|---|---|---|\n")
        for k in ids:
            t = unresolved[k]
            w(f"| {_export_dd_cell(k)} ⚠ | {_export_dd_cell(t.get('file'))} "
              f"| {', '.join(_export_dd_ref(f) for f in t['_feats']) or '—'} |\n")

    # 5. Orphans
    w("\n---\n\n## 5. Orphan Nodes\n\n")
    w("These nodes exist in the graph but are not linked to any parent. "
      "Review and link or delete them. Each is marked ⚠ **ORPHAN**.\n")
    w(f"\n### Orphan Stories ({len(a['orphans']['stories'])})\n")
    if a["orphans"]["stories"]:
        for sid in a["orphans"]["stories"]:
            render_story(sid, 4)
    else:
        w("\n(none)\n")
    w(f"\n### Orphan Use Cases ({len(a['orphans']['usecases'])})\n")
    if a["orphans"]["usecases"]:
        for uid in a["orphans"]["usecases"]:
            render_uc(uid, 4)
    else:
        w("\n(none)\n")
    w(f"\n### Orphan Features ({len(a['orphans']['features'])})\n")
    if a["orphans"]["features"]:
        for fid in a["orphans"]["features"]:
            render_feature(fid, 4)
    else:
        w("\n(none)\n")
    w(f"\n### Orphan Tests ({len(a['orphans']['tests'])})\n\n")
    if a["orphans"]["tests"]:
        by_file = {}
        for tid in a["orphans"]["tests"]:
            t = tests[tid]
            by_file.setdefault(t.get("_path") or t.get("file") or "—", []).append(t)
        w("Scanned tests that verify nothing, grouped by file.\n\n")
        w("| File ⚠ ORPHAN | Tier | Tests | Functions |\n|---|---|---|---|\n")
        for f, ts in sorted(by_file.items()):
            fns = [str(t.get("function") or "?") for t in ts]
            shown = ", ".join(fns[:5]) + (f", … +{len(fns) - 5}" if len(fns) > 5 else "")
            tiers = ", ".join(sorted({_export_dd_tier(t) for t in ts}))
            w(f"| {_export_dd_cell(f)} | {_export_dd_cell(tiers)} | {len(ts)} | {_export_dd_cell(shown)} |\n")
    else:
        w("(none)\n")

    # Linked nodes whose parents fall outside the export scope
    leftovers = [i for i in list(ucs) + list(stories) if i not in rendered]
    if leftovers:
        w("\n## 6. Other Linked Items in Scope\n\n")
        w("Linked to nodes outside this export's scope.\n")
        for i in leftovers:
            if i in ucs:
                render_uc(i, 3)
            elif i not in rendered:
                render_story(i, 3)

    def strip(nodes):
        return [{**{k: v for k, v in n.items() if not k.startswith("_")},
                 "links": {k[1:]: v for k, v in n.items() if k in ("_mods", "_feats", "_ucs")}}
                for n in nodes.values()]

    data = {
        "counts": {"modules": len(mods), "features": len(feats), "usecases": len(ucs),
                   "stories": len(stories), "tests": len(tests),
                   "unresolved_tests": len(unresolved), "orphans": len(orphan_ids)},
        "modules": list(mods.values()),
        "features": strip(feats), "usecases": strip(ucs),
        "stories": strip(stories), "tests": strip(tests),
        "orphans": a["orphans"], "gaps": a["gaps"],
        "test_links": {
            "resolved": sum(1 for t in tests.values() if t["_feats"]), "fixed_paths": fixed_paths,
            "unlinked_unresolved": d.get("unlinked_unresolved", 0),
            **{st: sorted(k for k, t in unresolved.items() if t["_link"] == st)
               for st in ("foreign", "missing", "nofile")},
        },
    }
    return out, data


def cmd_export(args):
    """Export a slice of the graph as a Markdown or JSON document."""
    _require_licence("export")
    import textwrap
    export_type = args.type
    instance    = getattr(args, "instance", "") or ""
    module_id   = getattr(args, "module", "") or ""
    out_fmt     = getattr(args, "format", "md") or "md"
    today       = _now_iso()[:10]

    scope = module_id or instance or "all"
    default_out = Path(f"docs/exports/{export_type}-{scope}-{today}.md")
    out_path = Path(args.output) if getattr(args, "output", None) else default_out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    drv = _driver()
    lines = []
    data = None

    with drv.session() as s:
        if export_type == "detailed-design":
            scoped = _export_dd_scope(_export_dd_collect(s), instance, module_id)
            lines, data = _export_dd_render(scoped, scope, today)

        elif export_type == "stories":
            lines.append(f"# User Stories — {scope}  ({today})\n")
            mod_ids = []
            if module_id:
                mod_ids = [module_id]
            elif instance:
                mod_ids = [r["id"] for r in s.run(
                    "MATCH (m:SysModule {instance:$inst}) RETURN m.id AS id",
                    inst=instance).data()]

            if mod_ids:
                stories = s.run("""
                    MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)<-[:REQUIRES]-(us:SysUserStory)
                    WHERE m.id IN $mids
                    RETURN DISTINCT us {.*} AS us, f.id AS fid, f.name AS fname, m.id AS mid
                    ORDER BY us.id
                """, mids=mod_ids).data()
            else:
                stories = s.run("MATCH (us:SysUserStory) RETURN us {.*} AS us ORDER BY us.id").data()

            seen = set()
            for r in stories:
                us = r.get("us") or r
                uid = us.get("id","?")
                if uid in seen: continue
                seen.add(uid)
                lines.append(f"## {uid} — {us.get('title','?')}\n")
                lines.append(f"**Actor:** {us.get('actor','?')}  \n")
                lines.append(f"**Goal:** {us.get('goal','?')}  \n")
                if us.get("benefit"): lines.append(f"**Benefit:** {us['benefit']}  \n")
                if us.get("narrative"): lines.append(f"\n{us['narrative']}\n")
                if us.get("acceptanceCriteria"):
                    lines.append("\n**Acceptance Criteria:**\n")
                    for ac in (us["acceptanceCriteria"] or []):
                        lines.append(f"- {ac}\n")
                lines.append("\n---\n")

        elif export_type == "use-cases":
            lines.append(f"# Use Cases — {scope}  ({today})\n")
            if instance and not module_id:
                uc_rows = s.run("""
                    MATCH (uc:SysUseCase {instance:$inst})
                    OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
                    OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
                    RETURN uc {.*} AS uc,
                           collect(DISTINCT us.id) AS story_ids,
                           collect(DISTINCT f.name) AS feat_names
                    ORDER BY uc.id
                """, inst=instance).data()
            else:
                # --module narrows to use cases requiring that module's features
                uc_rows = s.run("""
                    MATCH (uc:SysUseCase)
                    WHERE $mid = '' OR EXISTS {
                        MATCH (uc)-[:REQUIRES]->(:SysFeature)<-[:PROVIDES]-(:SysModule {id:$mid}) }
                    OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
                    OPTIONAL MATCH (uc)-[:REQUIRES]->(f:SysFeature)
                    RETURN uc {.*} AS uc,
                           collect(DISTINCT us.id) AS story_ids,
                           collect(DISTINCT f.name) AS feat_names
                    ORDER BY uc.id
                """, mid=module_id).data()
            for r in uc_rows:
                uc = r["uc"]
                lines.append(f"## {uc.get('id','?')} — {uc.get('title') or uc.get('name','?')}\n")
                lines.append(f"**Instance:** {uc.get('instance','?')}  **Priority:** {uc.get('priority','?')}  \n")
                if r["story_ids"]: lines.append(f"**Realises:** {', '.join(r['story_ids'])}  \n")
                if r["feat_names"]: lines.append(f"**Requires:** {', '.join(r['feat_names'])}  \n")
                if uc.get("description"): lines.append(f"\n{uc['description']}\n")
                if uc.get("preconditions"):
                    lines.append("\n**Preconditions:**\n")
                    for p in uc["preconditions"]: lines.append(f"- {p}\n")
                if uc.get("main_flow") or uc.get("mainFlow"):
                    flow = uc.get("main_flow") or uc.get("mainFlow") or []
                    lines.append("\n**Main Flow:**\n")
                    for step in flow: lines.append(f"1. {step}\n")
                lines.append("\n---\n")

        elif export_type == "application-arch":
            lines.append(f"# Application Architecture — {scope}  ({today})\n")
            if module_id:
                mods = s.run("MATCH (m:SysModule {id:$mid}) RETURN m {.*} AS m", mid=module_id).data()
            elif instance:
                mods = s.run("MATCH (m:SysModule {instance:$inst}) RETURN m {.*} AS m ORDER BY m.id",
                             inst=instance).data()
            else:
                mods = s.run("MATCH (m:SysModule) RETURN m {.*} AS m ORDER BY m.instance, m.id").data()
            for mr in mods:
                m = mr["m"]
                lines.append(f"## {m['id']} — {m.get('name','?')}\n")
                lines.append(f"**Instance:** {m.get('instance','?')}  **Type:** {m.get('type','?')}  \n")
                if m.get("paths"): lines.append(f"**Paths:** `{'`, `'.join(m['paths'])}`  \n")
                feats = s.run("""
                    MATCH (m:SysModule {id:$id})-[:PROVIDES]->(f:SysFeature)
                    OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
                    RETURN f.id AS fid, f.name AS fname, f.status AS fstat, count(t) AS tests
                    ORDER BY f.id
                """, id=m["id"]).data()
                if feats:
                    lines.append("\n| Feature | Status | Tests |\n|---|---|---|\n")
                    for f in feats:
                        lines.append(f"| {f['fid']} {f['fname']} | {f['fstat'] or '?'} | {f['tests']} |\n")
                lines.append("\n")

        elif export_type == "technical-arch":
            lines.append(f"# Technical Architecture — {scope}  ({today})\n")
            lines.append("\n## Architecture Decisions\n")
            for status in ("adopted","proposed","deprecated","superseded"):
                adrs = s.run("""
                    MATCH (a:SysArchDecision {status:$st})
                    RETURN a {.*} AS a ORDER BY a.id
                """, st=status).data()
                if adrs:
                    lines.append(f"\n### {status.capitalize()} ({len(adrs)})\n")
                    for ar in adrs:
                        a = ar["a"]
                        lines.append(f"\n#### {a['id']} — {a.get('title','?')}\n")
                        if a.get("date"): lines.append(f"**Date:** {a['date']}  \n")
                        if a.get("decision"): lines.append(f"\n{a['decision']}\n")
                        if a.get("consequences"): lines.append(f"\n**Consequences:** {a['consequences']}\n")
            lines.append("\n## Architecture Standards\n")
            for cat in ("execution","development","operations","security","infrastructure"):
                stds = s.run("""
                    MATCH (s:SysArchStd {category:$cat})
                    RETURN s {.*} AS s ORDER BY s.id
                """, cat=cat).data()
                if stds:
                    lines.append(f"\n### {cat.capitalize()} ({len(stds)})\n")
                    for sr in stds:
                        s_node = sr["s"]
                        addressed = s.run("""
                            MATCH (d)-[:ADDRESSES]->(std:SysArchStd {id:$id})
                            RETURN d.id AS did
                        """, id=s_node["id"]).data()
                        status_mark = "✓" if addressed else "✗"
                        lines.append(f"\n**{status_mark} {s_node['id']}** — {s_node.get('name','?')}  \n")
                        if s_node.get("rationale"): lines.append(f"*{s_node['rationale']}*  \n")
                        if addressed: lines.append(f"Addressed by: {', '.join(r['did'] for r in addressed)}  \n")
        elif export_type == "test-coverage":
            lines.append(f"# Test Coverage Report — {scope}  ({today})\n")
            lines.append(f"\nV-model tiers: **cmp** = component/unit · **int** = integration/API · "
                         f"**uc** = UI flow (per UC) · **e2e** = full journey (per US)\n\n")
            if module_id:
                mods = s.run("MATCH (m:SysModule {id:$mid}) RETURN m {.*} AS m", mid=module_id).data()
            elif instance:
                mods = s.run("MATCH (m:SysModule {instance:$inst}) RETURN m {.*} AS m ORDER BY m.id",
                             inst=instance).data()
            else:
                mods = s.run("MATCH (m:SysModule) RETURN m {.*} AS m ORDER BY m.instance, m.id").data()

            total_feats = total_cmp = total_int = total_uc = total_e2e = 0
            lines.append("| Module | Features | cmp | int | uc | e2e | Gaps |\n")
            lines.append("|---|---|---|---|---|---|---|\n")
            for mr in mods:
                m = mr["m"]
                cov = s.run("""
                    MATCH (m:SysModule {id:$id})-[:PROVIDES]->(f:SysFeature)
                    WHERE NOT f.status IN ['Superseded']
                    OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
                    WHERE tc.testType IN ['component','go-unit']
                       OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
                    OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
                    WHERE ti.testType = 'integration'
                       OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
                    OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
                    WHERE tu.testType = 'usecase'
                       OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
                    OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
                    WHERE te.testType = 'e2e'
                       OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
                    RETURN count(DISTINCT f) AS tot,
                           count(DISTINCT CASE WHEN tc IS NOT NULL THEN f END) AS cmp,
                           count(DISTINCT CASE WHEN ti IS NOT NULL THEN f END) AS int_,
                           count(DISTINCT CASE WHEN tu IS NOT NULL THEN f END) AS uc,
                           count(DISTINCT CASE WHEN te IS NOT NULL THEN f END) AS e2e
                """, id=m["id"]).single()
                if not cov or cov["tot"] == 0: continue
                n = cov["tot"]
                gaps = n - max(cov["cmp"], cov["int_"], cov["uc"])
                gap_str = f"**{gaps} untested**" if gaps > 0 else "✓"
                lines.append(f"| {m['id']} | {n} | {cov['cmp']}/{n} | {cov['int_']}/{n} | "
                              f"{cov['uc']}/{n} | {cov['e2e']}/{n} | {gap_str} |\n")
                total_feats += n; total_cmp += cov["cmp"]; total_int += cov["int_"]
                total_uc += cov["uc"]; total_e2e += cov["e2e"]
            lines.append(f"| **Total** | **{total_feats}** | **{total_cmp}** | **{total_int}** | "
                         f"**{total_uc}** | **{total_e2e}** | |\n\n")
            # Features with zero tests
            gap_rows = s.run("""
                MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
                WHERE ($inst='' OR m.instance=$inst)
                  AND NOT f.status IN ['Superseded']
                  AND NOT exists((:SysTest)-[:VERIFIES]->(f))
                RETURN m.id AS mid, f.id AS fid, f.name AS fname
                ORDER BY m.id, f.id
            """, inst=instance).data()
            if gap_rows:
                lines.append(f"\n## Untested Features ({len(gap_rows)})\n\n")
                lines.append("| Module | Feature | Name |\n|---|---|---|\n")
                for r in gap_rows:
                    lines.append(f"| {r['mid']} | {r['fid']} | {r['fname']} |\n")

        elif export_type == "test-summary":
            lines.append(f"# Test Summary — {scope}  ({today})\n")
            # Recent test runs — query SysTestRun nodes for accurate per-run data
            runs = s.run("""
                MATCH (run:SysTestRun)
                OPTIONAL MATCH (pkg:SysTestPackage {id: run.packageId})
                WHERE ($inst='' OR pkg.area=$inst)
                RETURN run.packageId AS id, pkg.area AS area, pkg.testCategory AS cat,
                       run.passed AS passed, run.failed AS failed,
                       run.ranAt AS ran, run.durationS AS dur
                ORDER BY run.ranAt DESC LIMIT 50
            """, inst=instance).data()
            if not runs:
                # Fallback: read snapshot from package nodes (no SysTestRun history)
                runs = s.run("""
                    MATCH (pkg:SysTestPackage)
                    WHERE ($inst='' OR pkg.area=$inst)
                      AND pkg.lastRun IS NOT NULL
                    RETURN pkg.id AS id, pkg.area AS area, pkg.testCategory AS cat,
                           pkg.lastPassed AS passed, pkg.lastFailed AS failed,
                           pkg.lastRun AS ran, pkg.lastDuration AS dur
                    ORDER BY pkg.lastRun DESC LIMIT 50
                """, inst=instance).data()
            lines.append(f"\n## Recent Test Runs ({len(runs)} packages)\n\n")
            lines.append("| Package | Area | Tier | Passed | Failed | Last Run | Duration |\n")
            lines.append("|---|---|---|---|---|---|---|\n")
            for r in runs:
                name = (r["id"] or "").split("/")[-1].replace(".py","")
                status = "✓" if not r["failed"] else f"**✗ {r['failed']}**"
                lines.append(f"| {name} | {r['area'] or '?'} | {r['cat'] or '?'} | "
                              f"{r['passed'] or 0} | {status} | "
                              f"{str(r['ran'] or '')[:10]} | {r['dur'] or '?'}s |\n")
            # Test counts by type
            totals = s.run("""
                MATCH (t:SysTest)
                RETURN t.testType AS tier, count(t) AS cnt
                ORDER BY tier
            """).data()
            lines.append(f"\n## Tests by Tier\n\n| Tier | Count |\n|---|---|\n")
            for r in totals:
                lines.append(f"| {r['tier'] or 'untyped'} | {r['cnt']} |\n")
            total_tests = sum(r["cnt"] for r in totals)
            lines.append(f"| **Total** | **{total_tests}** |\n")

        else:
            print(f"ERROR: unknown export type '{export_type}'. "
                  f"Choose: stories|use-cases|application-arch|technical-arch|test-coverage|test-summary|detailed-design",
                  file=sys.stderr)
            drv.close(); sys.exit(1)

    drv.close()

    if out_fmt == "json":
        import json as _json
        payload = {"type": export_type, "content": "".join(lines)}
        if data is not None:
            payload["data"] = data
        out_path.with_suffix(".json").write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"✓ Exported to {out_path.with_suffix('.json')}")
    else:
        out_path.write_text("".join(lines), encoding="utf-8")
        print(f"✓ Exported to {out_path}  ({len(lines)} lines)")
    if data is not None:
        c = data["counts"]
        print(f"  Modules: {c['modules']}    Features: {c['features']}    Use cases: {c['usecases']}    "
              f"Stories: {c['stories']}    Tests: {c['tests']}    "
              f"Unresolved tests: {c['unresolved_tests']}    Orphans: {c['orphans']}")


# ── Compliance: external refs, change log, baselines, matrix, ReqIF ───────────
# Building blocks for teams that must evidence traceability (P-10 steps 1–4).
# The change log is free (it records every audited write); baselines, the
# signed traceability matrix and ReqIF are Kit commands (_premium_* helpers are
# stubbed out of the free CLI by make_lite.py).

_EXTERNAL_REF_LABELS = ("SysUserStory", "SysUseCase", "SysFeature", "SysDefect", "SysEnhancement")
_EXTERNAL_SYSTEM_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")

# Commands whose effect on one node is recorded in the change log:
# command -> (label, args attribute holding the node id)
_AUDITED_COMMANDS = {
    "create-story":       ("SysUserStory",   "id"),
    "update-story":       ("SysUserStory",   "id"),
    "link-story":         ("SysUserStory",   "story"),
    "create-usecase":     ("SysUseCase",     "id"),
    "update-usecase":     ("SysUseCase",     "id"),
    "link-usecase":       ("SysUseCase",     "id"),
    "unlink-usecase":     ("SysUseCase",     "id"),
    "create-feature":     ("SysFeature",     "id"),
    "update-feature":     ("SysFeature",     "id"),
    "retire-feature":     ("SysFeature",     "id"),
    "update-enhancement": ("SysEnhancement", "id"),
    "close-enhancement":  ("SysEnhancement", "id"),
    "update-defect":      ("SysDefect",      "id"),
    "close-defect":       ("SysDefect",      "id"),
    "set-external-ref":   (None,             "id"),
}


@functools.lru_cache(maxsize=None)
def _actor() -> str:
    """Who is making changes: SYSEDGE_ACTOR, else git user.email, else the OS user."""
    actor = os.environ.get("SYSEDGE_ACTOR", "").strip()
    if not actor:
        try:
            actor = subprocess.run(["git", "config", "--get", "user.email"], capture_output=True,
                                   text=True, cwd=_root, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            actor = ""
    if not actor:
        import getpass
        try:
            actor = getpass.getuser()
        except Exception:
            actor = "unknown"
    return actor


def _node_snapshot(session, label: str | None, node_id: str) -> dict | None:
    """Properties plus outgoing/incoming edges of one node (None if absent)."""
    if label and not _SYS_LABEL_RE.match(label):
        return None
    match = f"MATCH (n:`{label}` {{id:$id}})" if label else \
        "MATCH (n {id:$id}) WHERE any(l IN labels(n) WHERE l STARTS WITH 'Sys')"
    row = session.run(match + """
        RETURN head([l IN labels(n) WHERE l STARTS WITH 'Sys']) AS label, n {.*} AS props,
               [(n)-[r]->(m) WHERE m.id IS NOT NULL | type(r) + '>' + m.id] AS out,
               [(m)-[r]->(n) WHERE m.id IS NOT NULL | m.id + '>' + type(r)] AS inc
    """, id=node_id).single()
    if not row or row.get("props") is None:
        return None
    return {"label": row["label"], "props": dict(row["props"]),
            "edges": sorted(set(row["out"] or []) | {"<" + e for e in row["inc"] or []})}


def _snapshot_diff(before: dict | None, after: dict | None) -> dict:
    """Field- and edge-level difference between two node snapshots."""
    bp = (before or {}).get("props", {})
    ap = (after or {}).get("props", {})
    fields = {k: {"before": bp.get(k), "after": ap.get(k)}
              for k in sorted(set(bp) | set(ap))
              if json.dumps(bp.get(k), default=str) != json.dumps(ap.get(k), default=str)}
    be, ae = set((before or {}).get("edges", [])), set((after or {}).get("edges", []))
    return {"fields": fields, "edges_added": sorted(ae - be), "edges_removed": sorted(be - ae)}


def _record_change(session, command: str, node_id: str, before: dict | None,
                   after: dict | None) -> str | None:
    """Append a SysChange entry, hash-chained to the previous one (tamper-evident)."""
    diff = _snapshot_diff(before, after)
    if not (diff["fields"] or diff["edges_added"] or diff["edges_removed"]):
        return None
    action = "create" if before is None else ("delete" if after is None else "update")
    for attempt in range(3):  # parallel sessions may race for the next seq
        try:
            return _write_change(session, command, node_id, before, after, action, diff)
        except Exception as e:
            if "ConstraintValidation" not in type(e).__name__ and "already exists" not in str(e):
                raise
    return None


def _write_change(session, command, node_id, before, after, action, diff) -> str:
    import hashlib
    prev = session.run("""
        MATCH (c:SysChange) RETURN c.hash AS h ORDER BY c.seq DESC LIMIT 1
    """).single()
    prev_hash = (prev or {}).get("h") or ""
    seq_row = session.run("MATCH (c:SysChange) RETURN coalesce(max(c.seq), 0) AS s").single()
    seq = int((seq_row or {}).get("s") or 0) + 1
    entry = {
        "seq": seq, "at": _now_iso(), "actor": _actor(), "command": command,
        "entityId": node_id, "label": (after or before or {}).get("label") or "",
        "action": action, "diff": json.dumps(diff, sort_keys=True, default=str),
        "prevHash": prev_hash,
    }
    entry["hash"] = hashlib.sha256(
        (prev_hash + json.dumps(entry, sort_keys=True, default=str)).encode("utf-8")).hexdigest()
    entry["id"] = f"CHG-{seq:06d}"
    session.run("CREATE (c:SysChange) SET c = $entry", entry=entry)
    if node_id and action != "delete":
        session.run("""
            MATCH (c:SysChange {id:$cid}), (n {id:$nid})
            WHERE any(l IN labels(n) WHERE l STARTS WITH 'Sys') AND NOT n:SysChange
            MERGE (c)-[:CHANGED]->(n)
        """, cid=entry["id"], nid=node_id)
    return entry["id"]


def _audited(command: str, fn):
    """Wrap a write command so its effect on the target node is logged."""
    label, attr = _AUDITED_COMMANDS[command]

    def run(args):
        node_id = getattr(args, attr, "") or ""
        if not node_id or os.environ.get("SYSEDGE_NO_CHANGELOG"):
            return fn(args)
        drv = _driver()
        with drv.session() as s:
            before = _node_snapshot(s, label, node_id)
        try:
            return fn(args)
        finally:
            try:
                with drv.session() as s:
                    _record_change(s, command, node_id, before, _node_snapshot(s, label, node_id))
            except Exception as e:  # never fail the user's command over the log
                print(f"  ⚠  change log not written: {e}", file=sys.stderr)
            drv.close()
    return run


def cmd_changes(args):
    """List change-log entries (newest first), optionally for one node; --verify checks the hash chain."""
    import hashlib
    drv = _driver()
    with drv.session() as s:
        if args.verify:
            rows = s.run("MATCH (c:SysChange) RETURN c {.*} AS c ORDER BY c.seq").data()
            prev, bad = "", []
            for r in rows:
                c = dict(r["c"])
                stored = c.pop("hash", "")
                c.pop("id", None)
                calc = hashlib.sha256((prev + json.dumps(c, sort_keys=True, default=str))
                                      .encode("utf-8")).hexdigest()
                if c.get("prevHash", "") != prev or calc != stored:
                    bad.append(c.get("seq"))
                prev = stored
            drv.close()
            if bad:
                print(f"✗ change log chain broken at seq {', '.join(map(str, bad[:10]))}"
                      f"{' …' if len(bad) > 10 else ''} ({len(rows)} entries)")
                sys.exit(1)
            print(f"✓ change log intact ({len(rows)} entries)")
            return
        where = "WHERE c.entityId = $id" if args.id else ""
        rows = s.run(f"""
            MATCH (c:SysChange) {where}
            RETURN c {{.*}} AS c ORDER BY c.seq DESC LIMIT $lim
        """, id=args.id, lim=args.limit).data()
    drv.close()
    if args.json:
        print(json.dumps([r["c"] for r in rows], indent=2, default=str))
        return
    if not rows:
        print("  No change-log entries.")
        return
    for r in rows:
        c = r["c"]
        diff = json.loads(c.get("diff") or "{}")
        parts = [f"{k}" for k in diff.get("fields", {})]
        parts += [f"+{e}" for e in diff.get("edges_added", [])]
        parts += [f"-{e}" for e in diff.get("edges_removed", [])]
        print(f"  {c['id']}  {str(c['at'])[:19]}  {c['actor']:<24} {c['action']:<6} "
              f"{c['entityId']:<16} {c['command']:<18} {', '.join(parts)[:60]}")


def cmd_set_external_ref(args):
    """Attach an external tracker reference (Jira, Azure DevOps, Polarion…) to a node."""
    system = args.system.lower()
    if not _EXTERNAL_SYSTEM_RE.match(system):
        print("ERROR: --system must be a short lowercase name, e.g. jira, ado, polarion",
              file=sys.stderr)
        sys.exit(1)
    ref = f"{system}:{args.ref}"
    drv = _driver()
    with drv.session() as s:
        row = s.run("""
            MATCH (n {id:$id}) WHERE any(l IN labels(n) WHERE l IN $labels)
            SET n.externalRefs = CASE
                WHEN $remove THEN [r IN coalesce(n.externalRefs, []) WHERE r <> $ref]
                WHEN $ref IN coalesce(n.externalRefs, []) THEN n.externalRefs
                ELSE coalesce(n.externalRefs, []) + $ref END
            RETURN n.externalRefs AS refs
        """, id=args.id, labels=list(_EXTERNAL_REF_LABELS), ref=ref, remove=args.remove).single()
    drv.close()
    if not row:
        print(f"⚠ {args.id} not found (story, use case, feature, defect or enhancement)",
              file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.id} external refs: {', '.join(row['refs'] or []) or '(none)'}")


# ── Baselines (Kit) ───────────────────────────────────────────────────────────

_BASELINE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _premium_snapshot(scoped: dict) -> dict:
    """Plain, deterministic snapshot of a scoped detailed-design slice."""
    def clean(nodes, keep_links=("_mods", "_feats", "_ucs")):
        out = {}
        for k, n in sorted(nodes.items()):
            props = {p: v for p, v in n.items() if not p.startswith("_")}
            props["links"] = {p[1:]: n[p] for p in keep_links if p in n}
            out[k] = props
        return out
    return {
        "modules": {k: {p: v for p, v in m.items()} for k, m in sorted(scoped["modules"].items())},
        "features": clean(scoped["features"]),
        "usecases": clean(scoped["usecases"]),
        "stories": clean(scoped["stories"]),
        "tests": {k: {"file": t.get("_path") or t.get("file"), "function": t.get("function"),
                      "tier": t.get("testType") or t.get("_pkgCat"),
                      "lastStatus": t.get("lastStatus"), "verifies": t.get("_feats", [])}
                  for k, t in sorted(scoped["tests"].items())},
    }


def _premium_baseline_path(name: str) -> Path:
    return Path("data") / "baselines" / f"{name}.json"


_VOLATILE_FIELDS = {"lastTestedAt", "scannedAt", "coverageReviewLastRun", "auditTestLastRun",
                    "coverageReviewCacheKey", "coverageReviewCacheData"}


def _premium_baseline_diff(a: dict, b: dict) -> dict:
    """Added / removed / changed entries per category between two snapshots."""
    out = {}
    for cat in ("modules", "features", "usecases", "stories", "tests"):
        x, y = a.get(cat, {}), b.get(cat, {})
        changed = {}
        for k in sorted(set(x) & set(y)):
            fields = sorted(f for f in set(x[k]) | set(y[k]) if f not in _VOLATILE_FIELDS and
                            json.dumps(x[k].get(f), sort_keys=True, default=str) !=
                            json.dumps(y[k].get(f), sort_keys=True, default=str))
            if fields:
                changed[k] = fields
        out[cat] = {"added": sorted(set(y) - set(x)), "removed": sorted(set(x) - set(y)),
                    "changed": changed}
    return out


def _premium_current_scope(session, instance: str, module_id: str) -> dict:
    return _export_dd_scope(_export_dd_collect(session), instance, module_id)


def cmd_baseline(args):
    """Freeze, list, show or diff requirement/test baselines (Kit)."""
    _require_licence("baseline")
    import hashlib
    if args.action == "list":
        drv = _driver()
        with drv.session() as s:
            rows = s.run("MATCH (b:SysBaseline) RETURN b {.*} AS b ORDER BY b.createdAt").data()
        drv.close()
        if not rows:
            print("  No baselines yet — create one with: baseline create --name R1.0")
        for r in rows:
            b = r["b"]
            print(f"  {b['id']:<20} {str(b['createdAt'])[:19]}  {b.get('actor',''):<24} "
                  f"{b.get('scope','all'):<10} {b.get('counts','')}  sha256:{b['sha256'][:12]}")
        return

    if args.action == "create":
        if not args.name or not _BASELINE_NAME_RE.match(args.name):
            print("ERROR: --name required (letters, digits, . _ -; max 64)", file=sys.stderr)
            sys.exit(1)
        path = _premium_baseline_path(args.name)
        if path.exists():
            print(f"ERROR: baseline {args.name} already exists — baselines are immutable",
                  file=sys.stderr)
            sys.exit(1)
        drv = _driver()
        with drv.session() as s:
            snap = _premium_snapshot(_premium_current_scope(s, args.instance, args.module))
            body = {"baseline": args.name, "createdAt": _now_iso(), "actor": _actor(),
                    "scope": args.module or args.instance or "all", "repo": _repo_id(),
                    "note": args.note, "snapshot": snap}
            text = json.dumps(body, indent=2, sort_keys=True, default=str)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            counts = " ".join(f"{k}={len(v)}" for k, v in snap.items())
            s.run("""
                CREATE (b:SysBaseline {id:$id, name:$id, createdAt:$at, actor:$actor,
                                       scope:$scope, sha256:$sha, path:$path, counts:$counts,
                                       note:$note})
            """, id=args.name, at=body["createdAt"], actor=body["actor"], scope=body["scope"],
                 sha=digest, path=str(path), counts=counts, note=args.note)
        drv.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"✓ Baseline {args.name}  ({counts})\n  {path}  sha256:{digest}")
        return

    # show / diff need stored baselines
    def load(name):
        p = _premium_baseline_path(name)
        if not p.exists():
            print(f"ERROR: baseline {name} not found ({p})", file=sys.stderr)
            sys.exit(1)
        text = p.read_text(encoding="utf-8")
        drv = _driver()
        with drv.session() as s:
            row = s.run("MATCH (b:SysBaseline {id:$id}) RETURN b.sha256 AS sha", id=name).single()
        drv.close()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row and row.get("sha") and row["sha"] != digest:
            print(f"✗ baseline {name} file does not match its recorded sha256 — it was modified",
                  file=sys.stderr)
            sys.exit(1)
        return json.loads(text)

    if args.action == "show":
        b = load(args.name)
        print(f"  {b['baseline']}  {b['createdAt'][:19]}  by {b['actor']}  scope={b['scope']}")
        for k, v in b["snapshot"].items():
            print(f"    {k:<10} {len(v)}")
        return

    if args.action == "diff":
        a = load(args.name)["snapshot"]
        if args.against:
            b, against = load(args.against)["snapshot"], args.against
        else:
            drv = _driver()
            with drv.session() as s:
                b = json.loads(json.dumps(_premium_snapshot(
                    _premium_current_scope(s, args.instance, args.module)), default=str))
            drv.close()
            against = "current graph"
        diff = _premium_baseline_diff(a, b)
        if args.json:
            print(json.dumps(diff, indent=2))
            return
        print(f"\n  Baseline {args.name} → {against}\n")
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
        print(f"\n  {total} differences" if total else "  ✓ No differences")


# ── Traceability matrix (Kit) ─────────────────────────────────────────────────

_MATRIX_COLUMNS = ["story", "story_title", "use_case", "use_case_title", "feature",
                   "feature_title", "feature_status", "module", "test", "test_file",
                   "test_tier", "test_last_status", "external_refs"]


def _premium_matrix_rows(snapshot: dict) -> list:
    """One row per story → use case → feature → test path (gaps keep empty cells)."""
    feats, ucs, stories, tests = (snapshot["features"], snapshot["usecases"],
                                  snapshot["stories"], snapshot["tests"])
    f_tests = {}
    for tid, t in tests.items():
        for f in t.get("verifies") or []:
            f_tests.setdefault(f, []).append(tid)
    uc_stories = {}
    for sid, s in stories.items():
        for u in s["links"].get("ucs", []):
            uc_stories.setdefault(u, []).append(sid)

    def refs(*nodes):
        out = []
        for n in nodes:
            out += (n or {}).get("externalRefs") or []
        return ";".join(sorted(set(out)))

    rows = []

    def add(sid, uid, fid):
        s, u, f = stories.get(sid), ucs.get(uid), feats.get(fid)
        base = {"story": sid or "", "story_title": (s or {}).get("title", ""),
                "use_case": uid or "", "use_case_title": (u or {}).get("title") or (u or {}).get("name", ""),
                "feature": fid or "", "feature_title": (f or {}).get("name", ""),
                "feature_status": (f or {}).get("status", ""),
                "module": ",".join((f or {}).get("links", {}).get("mods", [])),
                "external_refs": refs(s, u, f)}
        tids = f_tests.get(fid, []) if fid else []
        for tid in tids or [None]:
            t = tests.get(tid) or {}
            rows.append(dict(base, test=tid or "", test_file=t.get("file") or "",
                             test_tier=t.get("tier") or "", test_last_status=t.get("lastStatus") or ""))

    covered_f = set()
    for sid, s in stories.items():
        paths = [(u, f) for u in s["links"].get("ucs", []) for f in (ucs.get(u, {}).get("links", {}).get("feats") or [None])]
        paths += [(None, f) for f in s["links"].get("feats", []) if not any(p[1] == f for p in paths)]
        for u, f in paths or [(None, None)]:
            add(sid, u, f)
            covered_f.add(f)
    for uid, u in ucs.items():
        if not uc_stories.get(uid):
            for f in u["links"].get("feats") or [None]:
                add(None, uid, f)
                covered_f.add(f)
    for fid in feats:
        if fid not in covered_f:
            add(None, None, fid)
    return rows


def _premium_sign(path: Path, key_file: str) -> Path:
    """Detached Ed25519 signature over the file bytes (customer's own key)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    raw = Path(key_file).read_bytes()
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except ValueError:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw.decode().strip()))
    sig = path.with_suffix(path.suffix + ".sig")
    sig.write_text(key.sign(path.read_bytes()).hex() + "\n", encoding="utf-8")
    return sig


def cmd_trace_matrix(args):
    """Export the traceability matrix (CSV / XLSX / JSON) with a SHA-256 manifest, optionally signed (Kit)."""
    _require_licence("trace-matrix")
    import csv
    import hashlib
    if args.baseline:
        p = _premium_baseline_path(args.baseline)
        if not p.exists():
            print(f"ERROR: baseline {args.baseline} not found", file=sys.stderr)
            sys.exit(1)
        snapshot = json.loads(p.read_text(encoding="utf-8"))["snapshot"]
        source = f"baseline {args.baseline}"
    else:
        drv = _driver()
        with drv.session() as s:
            snapshot = json.loads(json.dumps(_premium_snapshot(
                _premium_current_scope(s, args.instance, args.module)), default=str))
        drv.close()
        source = "current graph"
    rows = _premium_matrix_rows(snapshot)
    scope = args.baseline or args.module or args.instance or "all"
    out = Path(args.output) if args.output else \
        Path("docs/exports") / f"trace-matrix-{scope}-{_now_iso()[:10]}.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_MATRIX_COLUMNS)
            w.writeheader()
            w.writerows(rows)
    elif args.format == "json":
        out.write_text(json.dumps({"source": source, "columns": _MATRIX_COLUMNS, "rows": rows},
                                  indent=2), encoding="utf-8")
    else:
        try:
            from openpyxl import Workbook
        except ImportError:
            print("ERROR: XLSX needs openpyxl (pip install openpyxl) — or use --format csv",
                  file=sys.stderr)
            sys.exit(1)
        wb = Workbook()
        ws = wb.active
        ws.title = "Traceability"
        ws.append(_MATRIX_COLUMNS)
        for r in rows:
            ws.append([r[c] for c in _MATRIX_COLUMNS])
        meta = wb.create_sheet("About")
        for k, v in (("source", source), ("generated", _now_iso()), ("actor", _actor()),
                     ("repo", _repo_id())):
            meta.append([k, v])
        wb.save(out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    manifest = out.with_suffix(out.suffix + ".sha256")
    manifest.write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    print(f"✓ {len(rows)} rows → {out}  (from {source})\n  sha256 {digest}  → {manifest.name}")
    key_file = args.sign_key or os.environ.get("SYSEDGE_SIGNING_KEY_FILE", "")
    if args.sign or args.sign_key:
        if not key_file:
            print("ERROR: --sign needs --sign-key FILE or SYSEDGE_SIGNING_KEY_FILE", file=sys.stderr)
            sys.exit(1)
        sig = _premium_sign(out, key_file)
        print(f"  Ed25519 signature → {sig.name}")


# ── ReqIF 1.2 (Kit) ───────────────────────────────────────────────────────────

_REQIF_NS = "http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"


def _premium_reqif_export(snapshot: dict, title: str) -> str:
    """Minimal ReqIF 1.2: stories, use cases and features as SPEC-OBJECTs with
    one String attribute each for title and description, plus SPEC-RELATIONs."""
    from xml.sax.saxutils import escape, quoteattr
    now = _now_iso()
    kinds = [("stories", "Story"), ("usecases", "UseCase"), ("features", "Feature")]
    objs, rels = [], []
    for cat, kind in kinds:
        for nid, n in snapshot.get(cat, {}).items():
            text = n.get("title") or n.get("name") or ""
            desc = n.get("description") or n.get("goal") or n.get("narrative") or ""
            objs.append(f"""      <SPEC-OBJECT IDENTIFIER={quoteattr(nid)} LONG-NAME={quoteattr(text)} LAST-CHANGE="{now}">
        <TYPE><SPEC-OBJECT-TYPE-REF>SOT-{kind}</SPEC-OBJECT-TYPE-REF></TYPE>
        <VALUES>
          <ATTRIBUTE-VALUE-STRING THE-VALUE={quoteattr(text)}><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>AD-Title</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
          <ATTRIBUTE-VALUE-STRING THE-VALUE={quoteattr(desc)}><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>AD-Description</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
        </VALUES>
      </SPEC-OBJECT>""")
    def rel(src, tgt, kind):
        rels.append(f"""      <SPEC-RELATION IDENTIFIER={quoteattr(f"REL-{src}-{kind}-{tgt}")} LAST-CHANGE="{now}">
        <TYPE><SPEC-RELATION-TYPE-REF>SRT-{kind}</SPEC-RELATION-TYPE-REF></TYPE>
        <SOURCE><SPEC-OBJECT-REF>{escape(src)}</SPEC-OBJECT-REF></SOURCE>
        <TARGET><SPEC-OBJECT-REF>{escape(tgt)}</SPEC-OBJECT-REF></TARGET>
      </SPEC-RELATION>""")
    known = {nid for cat, _ in kinds for nid in snapshot.get(cat, {})}
    for sid, s in snapshot.get("stories", {}).items():
        for u in s["links"].get("ucs", []):
            if u in known:
                rel(sid, u, "REALIZED_BY")
        for f in s["links"].get("feats", []):
            if f in known:
                rel(sid, f, "REQUIRES")
    for uid, u in snapshot.get("usecases", {}).items():
        for f in u["links"].get("feats", []):
            if f in known:
                rel(uid, f, "REQUIRES")
    obj_types = "\n".join(
        f'        <SPEC-OBJECT-TYPE IDENTIFIER="SOT-{k}" LONG-NAME="{k}" LAST-CHANGE="{now}">'
        f'<SPEC-ATTRIBUTES>'
        f'<ATTRIBUTE-DEFINITION-STRING IDENTIFIER="AD-Title" LONG-NAME="Title" LAST-CHANGE="{now}"><TYPE><DATATYPE-DEFINITION-STRING-REF>DT-String</DATATYPE-DEFINITION-STRING-REF></TYPE></ATTRIBUTE-DEFINITION-STRING>'
        f'<ATTRIBUTE-DEFINITION-STRING IDENTIFIER="AD-Description" LONG-NAME="Description" LAST-CHANGE="{now}"><TYPE><DATATYPE-DEFINITION-STRING-REF>DT-String</DATATYPE-DEFINITION-STRING-REF></TYPE></ATTRIBUTE-DEFINITION-STRING>'
        f'</SPEC-ATTRIBUTES></SPEC-OBJECT-TYPE>' for _, k in kinds)
    rel_types = "\n".join(f'        <SPEC-RELATION-TYPE IDENTIFIER="SRT-{k}" LONG-NAME="{k}" LAST-CHANGE="{now}"/>'
                          for k in ("REALIZED_BY", "REQUIRES"))
    hierarchy = "\n".join(f'          <SPEC-HIERARCHY IDENTIFIER={quoteattr("H-" + nid)} LAST-CHANGE="{now}">'
                          f'<OBJECT><SPEC-OBJECT-REF>{escape(nid)}</SPEC-OBJECT-REF></OBJECT></SPEC-HIERARCHY>'
                          for cat, _ in kinds for nid in snapshot.get(cat, {}))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<REQ-IF xmlns="{_REQIF_NS}">
  <THE-HEADER>
    <REQ-IF-HEADER IDENTIFIER="HDR-sysedge">
      <CREATION-TIME>{now}</CREATION-TIME>
      <REQ-IF-TOOL-ID>SysEdge</REQ-IF-TOOL-ID>
      <REQ-IF-VERSION>1.2</REQ-IF-VERSION>
      <SOURCE-TOOL-ID>SysEdge</SOURCE-TOOL-ID>
      <TITLE>{escape(title)}</TITLE>
    </REQ-IF-HEADER>
  </THE-HEADER>
  <CORE-CONTENT>
    <REQ-IF-CONTENT>
      <DATATYPES>
        <DATATYPE-DEFINITION-STRING IDENTIFIER="DT-String" LONG-NAME="String" MAX-LENGTH="65535" LAST-CHANGE="{now}"/>
      </DATATYPES>
      <SPEC-TYPES>
{obj_types}
{rel_types}
        <SPECIFICATION-TYPE IDENTIFIER="ST-Requirements" LONG-NAME="Requirements" LAST-CHANGE="{now}"/>
      </SPEC-TYPES>
      <SPEC-OBJECTS>
{chr(10).join(objs)}
      </SPEC-OBJECTS>
      <SPEC-RELATIONS>
{chr(10).join(rels)}
      </SPEC-RELATIONS>
      <SPECIFICATIONS>
        <SPECIFICATION IDENTIFIER="SPEC-sysedge" LONG-NAME={quoteattr(title)} LAST-CHANGE="{now}">
          <TYPE><SPECIFICATION-TYPE-REF>ST-Requirements</SPECIFICATION-TYPE-REF></TYPE>
          <CHILDREN>
{hierarchy}
          </CHILDREN>
        </SPECIFICATION>
      </SPECIFICATIONS>
    </REQ-IF-CONTENT>
  </CORE-CONTENT>
</REQ-IF>
"""


def _premium_reqif_parse(text: str) -> list:
    """SPEC-OBJECTs from a ReqIF file: [{identifier, title, description, type}]."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    def local(tag):
        return tag.rsplit("}", 1)[-1]
    defs = {}
    for el in root.iter():
        if local(el.tag).startswith("ATTRIBUTE-DEFINITION-"):
            defs[el.get("IDENTIFIER")] = (el.get("LONG-NAME") or "").lower()
    out = []
    for obj in root.iter():
        if local(obj.tag) != "SPEC-OBJECT":
            continue
        rec = {"identifier": obj.get("IDENTIFIER"), "title": obj.get("LONG-NAME") or "",
               "description": "", "type": ""}
        for el in obj.iter():
            tag = local(el.tag)
            if tag == "SPEC-OBJECT-TYPE-REF":
                rec["type"] = (el.text or "").strip()
            if tag.startswith("ATTRIBUTE-VALUE-"):
                ref = next((c.text for c in el.iter() if local(c.tag).startswith("ATTRIBUTE-DEFINITION-")
                            and local(c.tag).endswith("-REF")), "")
                name = defs.get((ref or "").strip(), "")
                val = el.get("THE-VALUE")
                if val is None:
                    val = " ".join(t.strip() for t in el.itertext() if t.strip())
                if name in ("title", "name", "reqif.name", "heading") and not rec["title"]:
                    rec["title"] = val
                elif name in ("description", "text", "reqif.text", "chapter"):
                    rec["description"] = val
        if rec["identifier"]:
            out.append(rec)
    return out


def cmd_reqif(args):
    """Export requirements as ReqIF 1.2, or import SPEC-OBJECTs as user stories (Kit)."""
    _require_licence("reqif")
    if args.action == "export":
        drv = _driver()
        with drv.session() as s:
            snapshot = json.loads(json.dumps(_premium_snapshot(
                _premium_current_scope(s, args.instance, args.module)), default=str))
        drv.close()
        out = Path(args.file or f"docs/exports/requirements-{args.module or args.instance or 'all'}.reqif")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_premium_reqif_export(snapshot, args.title or _repo_id()), encoding="utf-8")
        n = sum(len(snapshot[c]) for c in ("stories", "usecases", "features"))
        print(f"✓ {n} requirements → {out}")
        return
    if not args.file:
        print("ERROR: reqif import needs a file", file=sys.stderr)
        sys.exit(1)
    recs = _premium_reqif_parse(Path(args.file).read_text(encoding="utf-8"))
    drv = _driver()
    with drv.session() as s:
        existing = {r["ref"]: r["id"] for r in s.run("""
            MATCH (us:SysUserStory) UNWIND coalesce(us.externalRefs, []) AS ref
            WITH us, ref WHERE ref STARTS WITH 'reqif:' RETURN ref, us.id AS id
        """).data()}
        plan = []
        for i, r in enumerate(recs, 1):
            ref = f"reqif:{r['identifier']}"
            plan.append(("update" if ref in existing else "create", existing.get(ref) or
                         f"{args.prefix}-{i:03d}", ref, r))
        for action, sid, ref, r in plan:
            print(f"  {action:<7} {sid:<14} {ref:<40} {r['title'][:50]}")
        if not args.apply:
            print(f"\n  (dry run — {len(plan)} SPEC-OBJECTs; --apply to write)")
            drv.close()
            return
        for action, sid, ref, r in plan:
            s.run("""
                MERGE (us:SysUserStory {id:$id})
                SET us.title = $title, us.description = $desc,
                    us.source = 'reqif', us.instance = coalesce(us.instance, $inst),
                    us.externalRefs = CASE WHEN $ref IN coalesce(us.externalRefs, [])
                                           THEN us.externalRefs
                                           ELSE coalesce(us.externalRefs, []) + $ref END
            """, id=sid, title=r["title"], desc=r["description"], ref=ref, inst=args.instance)
    drv.close()
    print(f"\n✓ {len(plan)} user stories written from {args.file}")


def cmd_search(args):
    """Free-text search across SysFeature, SysEnhancement, SysDefect, SysUseCase, SysUserStory titles and descriptions."""
    q = (args.query or "").strip().lower()
    if not q:
        print("ERROR: --query is required", file=sys.stderr); sys.exit(1)
    node_types = [t.strip() for t in (getattr(args, "type", "") or "").split(",") if t.strip()] \
                 or ["SysFeature", "SysEnhancement", "SysDefect", "SysUseCase", "SysUserStory"]
    limit = getattr(args, "limit", 20) or 20
    drv = _driver()
    with drv.session() as s:
        all_hits = []
        for ntype in node_types:
            if ntype not in _VALID_SYS_LABELS:
                print(f"  ⚠  unknown type '{ntype}' — skipping", file=sys.stderr)
                continue
            rows = s.run(f"""
                MATCH (n:{ntype})
                WHERE toLower(coalesce(n.title, n.name, '')) CONTAINS $q
                   OR toLower(coalesce(n.description, ''))   CONTAINS $q
                RETURN n.id AS id, coalesce(n.title, n.name, '') AS title,
                       coalesce(n.status,'') AS status,
                       coalesce(n.instance,'') AS inst,
                       '{ntype}' AS ntype
                LIMIT $lim
            """, q=q, lim=limit).data()
            all_hits.extend(rows)
    drv.close()
    if not all_hits:
        print(f"  No results for '{q}'")
        return
    print(f"\n  Search: '{q}'  ({len(all_hits)} hits)\n")
    for r in all_hits:
        label = r["ntype"].replace("Sys","")
        status_tag = f"  [{r['status']}]" if r["status"] and r["status"] not in ("","active") else ""
        inst_tag   = f"  [{r['inst']}]" if r["inst"] else ""
        print(f"  {r['id']:<18} {label:<16} {r['title'][:50]}{status_tag}{inst_tag}")
    print()


def cmd_full_inventory(args):
    """Full cross-instance work inventory — all open defects and enhancements grouped by instance.

    Primarily used by the architect instance to plan work allocation across sessions.
    Provides a summary table plus per-instance detail of every open defect and
    pending enhancement (proposed / approved / in-progress).
    """
    drv = _driver()
    today = date.today().isoformat()
    W = 72
    system = getattr(args, "system", "") or ""

    with drv.session() as s:
        # Resolve which instances belong to the requested system (if any)
        if system:
            sys_instances = {r["inst"] for r in s.run("""
                MATCH (sys:SysSystem {id: $sys})-[:CONTAINS]->(d:SysDomain)-[:CONTAINS]->(m:SysModule)
                WHERE m.instance IS NOT NULL AND m.instance <> ''
                RETURN DISTINCT m.instance AS inst
            """, sys=system).data()}
            if not sys_instances:
                print(f"⚠  No instances found for system '{system}'. "
                      f"Check: sys_edge.py search --query '{system}' --type SysSystem",
                      file=sys.stderr)
                drv.close(); return
        else:
            sys_instances = set()

        # All known instances (from module nodes), filtered by system if given
        instances = sorted({r["inst"] for r in s.run("""
            MATCH (m:SysModule) WHERE m.instance IS NOT NULL AND m.instance <> ''
            RETURN DISTINCT m.instance AS inst
        """).data()} if not sys_instances else sys_instances)

        # Build instance filter clause
        inst_list = list(sys_instances) if sys_instances else []

        # All open defects — filtered to system instances when --system is given
        defects = s.run("""
            MATCH (d:SysDefect) WHERE (d.status <> 'closed' AND d.closedAt IS NULL)
              AND ($inst_list = [] OR d.instance IN $inst_list)
            OPTIONAL MATCH (d)-[:AFFECTS]->(f:SysFeature)
            RETURN d.id AS did, d.title AS title,
                   coalesce(d.severity,'medium') AS sev,
                   coalesce(d.instance,'?') AS instance,
                   coalesce(d.status,'open') AS status,
                   f.id AS fid
            ORDER BY
              CASE coalesce(d.instance,'?') WHEN '?' THEN 'zzz' ELSE coalesce(d.instance,'?') END,
              CASE coalesce(d.severity,'medium')
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium'   THEN 2 ELSE 3 END,
              d.id
        """, inst_list=inst_list).data()

        # All open enhancements — filtered to system instances when --system is given
        enhancements = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.status IN ['proposed','approved','in-progress']
              AND ($inst_list = [] OR e.instance IN $inst_list)
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)
            RETURN e.id AS eid, e.title AS title,
                   e.status AS status,
                   coalesce(e.priority,'Should') AS priority,
                   coalesce(e.instance,'?') AS instance,
                   collect(DISTINCT f.id) AS feature_ids
            ORDER BY
              CASE coalesce(e.instance,'?') WHEN '?' THEN 'zzz' ELSE coalesce(e.instance,'?') END,
              CASE e.status
                WHEN 'in-progress' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
              CASE coalesce(e.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              e.id
        """, inst_list=inst_list).data()

    drv.close()

    # Group by instance
    by_inst_def: dict = {}
    for d in defects:
        by_inst_def.setdefault(d["instance"], []).append(d)

    by_inst_enh: dict = {}
    for e in enhancements:
        by_inst_enh.setdefault(e["instance"], []).append(e)

    all_insts = sorted(set(list(by_inst_def.keys()) + list(by_inst_enh.keys()) + instances))

    print("═" * W)
    scope_label = f"SYSTEM: {system}" if system else "ALL INSTANCES"
    print(f"  FULL INVENTORY — {scope_label}  ({today})")
    print(f"  Cross-instance work allocation  ·  use worklog --instance <x> for per-session detail")
    if system:
        print(f"  Scoped to system '{system}' — pass no --system to see everything")
    print("═" * W)

    # Summary table
    print(f"\n  {'INSTANCE':<20}  {'DEFECTS':>8}  {'MUST-ENH':>9}  {'ENH-TOTAL':>10}  {'IN-PROG':>8}")
    print(f"  {'─'*20}  {'─'*8}  {'─'*9}  {'─'*10}  {'─'*8}")
    total_d = total_e = total_ip = 0
    for inst in all_insts:
        d_list = by_inst_def.get(inst, [])
        e_list = by_inst_enh.get(inst, [])
        must_count = sum(1 for e in e_list if e["priority"] == "Must")
        ip_count   = sum(1 for e in e_list if e["status"] == "in-progress")
        urgent_d   = sum(1 for d in d_list if d["sev"] in ("critical", "high"))
        d_tag = f"{len(d_list)}" + (f" ({urgent_d}!)" if urgent_d else "  ")
        print(f"  {inst:<20}  {d_tag:>8}  {must_count:>9}  {len(e_list):>10}  {ip_count:>8}")
        total_d += len(d_list)
        total_e += len(e_list)
        total_ip += ip_count
    print(f"  {'─'*20}  {'─'*8}  {'─'*9}  {'─'*10}  {'─'*8}")
    print(f"  {'TOTAL':<20}  {total_d:>8}  {'':>9}  {total_e:>10}  {total_ip:>8}")

    # Per-instance detail
    filter_inst = getattr(args, "instance", "") or ""
    inst_filter = [filter_inst] if filter_inst else all_insts

    for inst in inst_filter:
        d_list = by_inst_def.get(inst, [])
        e_list = by_inst_enh.get(inst, [])
        if not d_list and not e_list:
            continue

        print(f"\n{'─' * W}")
        print(f"  {inst.upper()}")
        print(f"{'─' * W}")

        # Defects
        if d_list:
            print(f"\n  DEFECTS ({len(d_list)})")
            seen: set = set()
            for d in d_list:
                if d["did"] in seen:
                    continue
                seen.add(d["did"])
                sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}.get(d["sev"],"●●○○")
                stat_tag = f"  [{d['status']}]" if d["status"] not in ("open","") else ""
                fid_tag  = f"  → {d['fid']}" if d.get("fid") else ""
                print(f"    {sev_mark} {d['did']:<12}  [{d['sev']}]{stat_tag}{fid_tag}  {d['title'][:50]}")
        else:
            print(f"\n  DEFECTS — none")

        # Enhancements grouped by status
        if e_list:
            in_p  = [e for e in e_list if e["status"] == "in-progress"]
            apprv = [e for e in e_list if e["status"] == "approved"]
            prop  = [e for e in e_list if e["status"] == "proposed"]

            for grp_label, grp in [("IN PROGRESS", in_p), ("APPROVED", apprv), ("PROPOSED", prop)]:
                if not grp:
                    continue
                print(f"\n  ENHANCEMENTS — {grp_label} ({len(grp)})")
                for e in grp:
                    prio_mark = {"Must":"★★★","Should":"★★☆","Could":"★☆☆"}.get(e["priority"],"★★☆")
                    fids      = [f for f in (e.get("feature_ids") or []) if f]
                    fid_tag   = f"  [{fids[0]}]" if fids else ""
                    print(f"    {prio_mark} {e['eid']:<12}  [{e['priority']}]{fid_tag}  {e['title'][:52]}")
        else:
            print(f"\n  ENHANCEMENTS — none pending")

    print(f"\n{'═' * W}")
    print(f"  {total_d} defect(s)  ·  {total_e} enhancement(s)  ·  {total_ip} in-progress")
    print(f"  sys_edge.py worklog --instance <name>   — per-session detail")
    print(f"  sys_edge.py status                      — coverage + health dashboard")
    print(f"{'═' * W}")


def cmd_status(args):
    """Cross-instance status dashboard — enhancements, defects, coverage, health."""
    drv = _driver()
    today = date.today().isoformat()
    W = 70

    with drv.session() as s:
        instances = [r["inst"] for r in s.run("""
            MATCH (m:SysModule) WHERE m.instance IS NOT NULL AND m.instance <> ''
            RETURN DISTINCT m.instance AS inst ORDER BY inst
        """).data()]

        enh_rows = s.run("""
            MATCH (e:SysEnhancement) WHERE e.status <> 'done'
            RETURN e.instance AS inst,
                   sum(CASE WHEN e.priority='Must'   THEN 1 ELSE 0 END) AS must,
                   sum(CASE WHEN e.priority='Should' THEN 1 ELSE 0 END) AS should,
                   sum(CASE WHEN e.priority='Could'  THEN 1 ELSE 0 END) AS could,
                   sum(CASE WHEN e.status='in-progress' THEN 1 ELSE 0 END) AS wip,
                   count(e) AS total
            ORDER BY inst
        """).data()
        enh_map = {r["inst"]: r for r in enh_rows}

        def_rows = s.run("""
            MATCH (d:SysDefect) WHERE (d.status <> 'closed' AND d.closedAt IS NULL)
            RETURN coalesce(d.instance,'?') AS inst,
                   sum(CASE WHEN d.severity IN ['critical','high'] THEN 1 ELSE 0 END) AS urgent,
                   count(d) AS total
            ORDER BY inst
        """).data()
        def_map = {r["inst"]: r for r in def_rows}

        # Single-pass coverage: one OPTIONAL MATCH + testType index (replaces 4-pass EXISTS approach)
        cov_rows = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE NOT f.status IN ['Superseded','Deprecated']
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN m.instance AS inst,
                   count(DISTINCT f) AS total,
                   count(DISTINCT CASE WHEN t IS NOT NULL THEN f END) AS any_cov,
                   count(DISTINCT CASE WHEN t.testType IN ['component','go-unit'] THEN f END) AS cmp,
                   count(DISTINCT CASE WHEN t.testType = 'integration' THEN f END) AS int_,
                   count(DISTINCT CASE WHEN t.testType IN ['usecase','e2e'] THEN f END) AS uc
            ORDER BY inst
        """).data()
        cov_map = {r["inst"]: r for r in cov_rows}

        open_defs = s.run("""
            MATCH (d:SysDefect) WHERE (d.status <> 'closed' AND d.closedAt IS NULL)
            RETURN d.id AS id, d.title AS title,
                   coalesce(d.severity,'medium') AS sev,
                   coalesce(d.instance,'?') AS inst
            ORDER BY CASE d.severity
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 ELSE 3 END, d.id
        """).data()

        in_prog = s.run("""
            MATCH (e:SysEnhancement {status:'in-progress'})
            RETURN e.id AS id, e.title AS title, e.instance AS inst
            ORDER BY e.instance, e.id
        """).data()

        # Targeted counts — avoids full graph scan across all labels
        counts = {}
        for _lbl in ("SysEnhancement", "SysDefect", "SysFeature", "SysTest",
                     "SysTestPackage", "SysModule", "SysUseCase", "SysUserStory",
                     "SysFeedback", "SysSystem", "SysDomain"):
            counts[_lbl] = s.run(f"MATCH (n:{_lbl}) RETURN count(n) AS c").single()["c"]

    drv.close()

    print("═" * W)
    print(f"  SYSTEM STATUS  v{VERSION}  ({today})")
    print("═" * W)
    print(f"\n  {'Instance':<14} {'Must':>4} {'Shld':>4} {'Cld':>4} {'WIP':>4}  "
          f"{'Def':>4}  {'Cover':>5}  cmp  int   uc")
    print(f"  {'─'*14} {'─'*4} {'─'*4} {'─'*4} {'─'*4}  "
          f"{'─'*4}  {'─'*5}  {'─'*3}  {'─'*3}  {'─'*3}")

    all_must = all_def_urgent = 0
    for inst in instances:
        e = enh_map.get(inst, {"must":0,"should":0,"could":0,"wip":0,"total":0})
        d = def_map.get(inst, {"urgent":0,"total":0})
        c = cov_map.get(inst, {"total":0,"any_cov":0,"cmp":0,"int_":0,"uc":0})
        pct = int(100*c["any_cov"]/c["total"]) if c["total"] else 0
        cov_mark = "✓" if pct==100 else ("~" if pct>0 else "✗")
        def_flag = "⚠" if d["urgent"] > 0 else " "
        must_s = f"{e['must']:4d}" if e["must"] else "   ."
        def_s  = f"{d['total']:4d}" if d["total"] else "   ."
        print(f"  {inst:<14} {must_s} {e['should']:4d} {e['could']:4d} "
              f"{e['wip']:4d}  {def_s}{def_flag} "
              f" {cov_mark}{pct:3d}%  {c['cmp']:3d}  {c['int_']:3d}  {c['uc']:3d}")
        all_must += e["must"]
        all_def_urgent += d["urgent"]

    print()
    if in_prog:
        print(f"  IN PROGRESS ({len(in_prog)})")
        for e in in_prog:
            print(f"    {e['id']:<12} [{e['inst']:<12}]  {e['title'][:45]}")
        print()

    sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}
    if open_defs:
        print(f"  OPEN DEFECTS ({len(open_defs)})")
        for d in open_defs:
            print(f"    {sev_mark.get(d['sev'],'●●○○')}  {d['id']:<12} "
                  f"[{d['inst']:<12}]  {d['title'][:40]}")
        print()
    else:
        print(f"  OPEN DEFECTS — none ✓\n")

    total_enh  = sum(r["total"]   for r in enh_map.values())
    total_feat = sum(r["total"]   for r in cov_map.values())
    total_cov  = sum(r["any_cov"] for r in cov_map.values())
    overall    = int(100*total_cov/total_feat) if total_feat else 0

    print(f"  {'─'*W}")
    print(f"  {len(instances)} instances  ·  "
          f"{total_enh} open enhancements ({all_must} Must)  ·  "
          f"{len(open_defs)} defects ({all_def_urgent} urgent)  ·  "
          f"{overall}% overall coverage")
    print(f"  graph: {counts.get('SysUserStory',0)} stories  "
          f"{counts.get('SysUseCase',0)} UCs  "
          f"{counts.get('SysFeature',0)} features  "
          f"{counts.get('SysTest',0)} tests")
    if all_must:
        print(f"\n  ⚠  {all_must} Must-priority enhancements — run worklog --instance <name>")
    if all_def_urgent:
        print(f"  ⚠  {all_def_urgent} critical/high defects open")
    print(f"{'═'*W}")


# ── CLI wiring ────────────────────────────────────────────────────────────────

# Grouping for the top-level --help. Commands not listed here appear under
# OTHER, so the help can never silently omit a command.
# Commands that never touch the graph database (work on any backend)
_BACKEND_NEUTRAL = {"config"}

_HELP_GROUPS = [
    ("SETUP & DATA", ["init", "config", "seed", "backup", "reset", "seed-standards"]),
    ("SESSION START", ["briefing", "worklog", "status", "test-gaps", "health-report"]),
    ("WORK ITEMS", ["create-enhancement", "start-enhancement", "close-enhancement",
                    "abandon-enhancement", "show-enhancement", "update-enhancement",
                    "link-defect", "claim-defect", "unclaim-defect", "close-defect",
                    "show-defect", "update-defect"]),
    ("REQUIREMENTS", ["create-story", "create-usecase", "create-feature", "update-feature",
                      "link-story", "link-usecase", "unlink-usecase", "show-story", "show-usecase",
                      "show-feature", "list-usecases", "search", "set-external-ref", "changes"]),
    ("CODE & TESTS", ["scan-tests", "scan-go-tests", "scan-code", "scan-features",
                      "scan-endpoints", "link-feature", "link-test", "link-symbol",
                      "link-endpoint", "check-test-links", "record-run", "run-history",
                      "test-status", "prune-runs", "prune-stale-tests"]),
    ("ANALYSIS & DOCUMENTS", ["export", "analyse", "merge-nodes", "audit-test",
                              "coverage-review", "quality-review", "preview-import",
                              "commit-import", "create-adr", "baseline", "trace-matrix",
                              "reqif"]),
    ("NOTES & FEEDBACK", ["add-note", "show-notes", "session-close"]),
]


def _grouped_help(subparsers_action) -> str:
    """Category listing generated from the registered subcommands."""
    helps = {a.dest: a.help for a in subparsers_action._choices_actions
             if a.help and a.help != argparse.SUPPRESS}
    lines = ["Commands by category (run <cmd> --help for details):", ""]
    seen: set = set()
    for title, cmds in _HELP_GROUPS + [("OTHER", sorted(helps))]:
        rows = [c for c in cmds if c in helps and c not in seen]
        if not rows:
            continue
        lines.append(f"  {title}")
        for c in rows:
            seen.add(c)
            tag = "" if c in PREMIUM_COMMANDS else ""
            lines.append(f"    {c:<20} {tag}{helps[c]}")
        lines.append("")
    lines.append("Exit codes: 0 ok · 1 error · 2 database unreachable / not configured · 3 not licensed")
    return "\n".join(lines)


class _HideInternalFormatter(argparse.RawDescriptionHelpFormatter):
    """Formatter that omits subcommands whose help is set to SUPPRESS."""

    def _format_action(self, action):
        if action.help == argparse.SUPPRESS:
            return ""
        # For subparser group actions, filter out individual SUPPRESS entries
        if hasattr(action, "_get_subactions"):
            parts = []
            for sub in action._get_subactions():
                if sub.help != argparse.SUPPRESS:
                    parts.append(self._format_action(sub))
            return "".join(parts)
        return super()._format_action(action)

    def _metavar_formatter(self, action, default_metavar):
        # Narrow the {cmd1|cmd2|...} usage line to only visible subcommands
        if hasattr(action, "_get_subactions"):
            visible = [s.dest for s in action._get_subactions()
                       if s.help != argparse.SUPPRESS]
            def _format(tuple_size):
                if tuple_size == 1:
                    return (("{%s}" % ",".join(visible)),)
                return (("{%s}" % ",".join(visible)),) * tuple_size
            return _format
        return super()._metavar_formatter(action, default_metavar)


def main():
    # Status symbols (✓ ⚠ ✗) must not crash piped output on Windows code pages.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # Always run from the project root so relative paths work regardless of CWD.
    # _root is derived from __file__ (absolute), so this is safe from any directory.
    os.chdir(_root)

    _GROUPED_HELP = """
Commands by category (run <cmd> --help for details):

  SETUP & DATA
    seed            Load a JSON backup into the graph
    backup          Export all graph data to a JSON backup file
    seed-standards  Load architecture standards from YAML

  GRAPH HEALTH & INSIGHT
    briefing        Dashboard for one instance: features, ENHs, defects, coverage
    worklog         Prioritised work list for this session
    full-inventory  All open work across all instances (architect view)
    test-status     Test-run summary across all packages
    status          Cross-instance summary dashboard
    test-gaps       Features with no test coverage
    analyse         Orphan check, merge/split candidates  [licence]
    audit-test      Deep dive: tests linked to a UC/feature/story

  WORK ITEMS
    create-defect   Create a defect and optionally link it to a feature
    link-defect     Alias for create-defect
    close-defect    Mark a defect closed
    show-defect     Display full defect details
    update-defect   Patch defect title/description/severity
    create-enhancement  Create an enhancement
    start-enhancement   Claim an enhancement as in-progress
    close-enhancement   Mark an enhancement done
    show-enhancement    Display full enhancement details
    update-enhancement  Patch enhancement title/description/priority

  TRACEABILITY
    link-feature    Link tests to a feature via VERIFIES edges
    link-endpoint   Link an API endpoint to a feature
    link-symbol     Link a code symbol to a feature
    link-usecase    Add feature/story/test links to a use case
    unlink-usecase      Remove stale REQUIRES or REALIZED_BY edges
    link-story          Create or update a user story
    link-enhancement    Link an enhancement to a feature
    unlink-enhancement  Remove stale Enhancement→Feature EXTENDS edges
    actor-coverage      Actor × feature story coverage matrix
    uc-state-check      AI check UC postconditions align with paired UC preconditions
    enh-align           AI check ENH description covers linked UC main flows

  TESTING
    scan-tests      Scan Python test files and register SysTest nodes
    scan-go-tests   Scan Go test files and register SysTest nodes
    scan-endpoints  Scan Go source for routes, diff vs graph (missing/stale/perm-mismatch)
    record-run      Record a test-run result; --results-json for per-test accuracy
    run-history     Pass-rate trend from SysTestRun history, with flaky detection
    prune-runs      Delete old SysTestRun nodes (--days 30 --keep 5)
    export          Generate Markdown docs (stories, coverage, test-summary, detailed-design…)

  SEARCH & DISCOVERY
    show-feature    Display feature details
    show-story      Display user story details
    show-usecase    Display use case details

  SESSION MANAGEMENT
    create-note       Attach a note to the graph for this instance
    show-note         Display a note
    quality-review    AI review of coverage & quality

  ADMIN
    merge-nodes     Merge two duplicate nodes
    analyse         Find orphans, merge/split candidates

Run: python3 scripts/sys_edge.py <cmd> --help
"""

    p = argparse.ArgumentParser(
        usage=f"{Path(sys.argv[0]).name} <command> [options]   ({Path(sys.argv[0]).name} <command> --help)",
        description="SysEdge — requirements traceability graph for AI coding agents",
        formatter_class=_HideInternalFormatter,
    )
    p.add_argument("--version", action="version", version=f"SysEdge {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command> [options]")

    sub.add_parser("init", help="Create constraints and indexes (run once per database)")

    sp = sub.add_parser("seed-standards",
                        help="Load the architecture standards library from YAML")
    sp.add_argument("file", nargs="?", default=None,
                    help="Path to YAML standards file (default: bundled arch-standards.yaml)")

    sp = sub.add_parser("seed", help="Load JSON seed file")
    sp.add_argument("file", help="Path to seed JSON")
    sp.add_argument("--instance", default="",
                    help="Only seed nodes belonging to this instance (safe for parallel sessions)")
    sp.add_argument("--module",   default="",
                    help="Only seed nodes belonging to this module ID")
    sp.add_argument("--no-backup",    action="store_true",
                    help="Skip the automatic pre-seed backup (sys-edge only)")
    sp.add_argument("--full-restore", action="store_true",
                    help="Allow unfiltered seed — sys-edge only, overwrites all instances")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="Preview what would change without writing to the graph")

    sp = sub.add_parser("briefing", help="Session-start briefing for an instance")
    sp.add_argument("--instance", required=True, help="Instance name (e.g. framework)")
    sp.add_argument("--system",   default="",    help="Scope to a system/repo (e.g. rms, SYS-CE)")
    sp.add_argument("--compact",  action="store_true",
                    help="Skip per-feature list for fully-passing modules")

    sp = sub.add_parser("coverage", help=argparse.SUPPRESS)
    sp.add_argument("--instance", required=True)
    sp.add_argument("--system",   default="")
    sp.add_argument("--compact",  action="store_true")

    sp = sub.add_parser("link-test", help=argparse.SUPPRESS)
    sp.add_argument("--feature",  required=True)
    sp.add_argument("--file",     required=True)
    sp.add_argument("--fn",       required=True, help="Test function name")
    sp.add_argument("--type",     default="integration")

    sp = sub.add_parser("test-gaps", help="Show test coverage gaps by tier for an instance (or packages with no feature links)")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--instance", default="", help="Instance name, e.g. core, manage, plan")
    grp.add_argument("--system",   default="", help="System ID without --instance shows all modules, e.g. SYS-CE")
    grp.add_argument("--unlinked-packages", "--p6", dest="p6", action="store_true", help="Test packages with no feature links (alias: --p6)")
    sp.add_argument("--tier", default="all",
                    choices=["all","component","integration","usecase","e2e"],
                    help="Filter to one tier (default: all three)")
    sp.add_argument("--days", default=30, type=int,
                    help="Look-back window (days) for recently-closed ENHs with no tests (default: 30)")

    sp = sub.add_parser("worklog", help="Show open defects and pending enhancements with feature/code links")
    sp.add_argument("--instance", required=True, help="Instance name, e.g. core, manage, plan")
    sp.add_argument("--system",   default="",    help="Scope to a system/repo (e.g. rms, SYS-CE)")
    sp.add_argument("--all",     action="store_true",
                    help="Show all ENHs including those whose feature links are outside this instance's modules")
    sp.add_argument("--since",    default="",
                    help="Only show ENHs created on or after this date, e.g. 2026-05-20")
    sp.add_argument("--cross-instance", action="store_true", dest="cross_instance",
                    help="Also show proposed enhancements from all other instances (auto-enabled for architect)")

    def _build_defect_sp(sp):
        sp.add_argument("--id",          default="", help="Defect ID, e.g. DEF-001 (auto-assigned if omitted)")
        sp.add_argument("--feature",     default="", help="Feature ID to link via AFFECTS edge (optional — can link later with update-defect)")
        sp.add_argument("--title",       required=True)
        sp.add_argument("--description", default="", help="Optional prose description: root cause, repro steps, findings")
        sp.add_argument("--severity",    default="medium", choices=["critical","high","medium","low"])
        sp.add_argument("--instance",    default="")
        sp.add_argument("--acceptance-criteria", default="", dest="acceptance_criteria",
                        help="The specific assertion or eval question that currently fails and must pass after the fix")
        sp.add_argument("--blocks", default="", metavar="ENH-xxx",
                        help="Enhancement this defect blocks — creates SysDefect-[:BLOCKS]->SysEnhancement. "
                             "Required when filing a test-debt defect for close-enhancement --force.")
        return sp
    sp = _build_defect_sp(sub.add_parser("link-defect",   help="Create a defect node and optionally link it to a feature"))
    sp = _build_defect_sp(sub.add_parser("create-defect", help="Create a defect node and optionally link it to a feature (alias for link-defect)"))

    sp = sub.add_parser("close-defect", help="Mark a defect as closed")
    sp.add_argument("--id", required=True)
    sp.add_argument("--skip-uc-check", action="store_true", dest="skip_uc_check",
                    help="Suppress UC prose review (use when defect was env/test issue, not a behaviour change)")
    sp.add_argument("--confirm-tests", action="store_true", dest="confirm_tests",
                    help="Confirm that integration, unit, and regression tests were run and passed")
    sp.add_argument("--no-code", action="store_true", dest="no_code",
                    help="No code was changed (documentation / graph-only fix) — waives test-run requirement")
    sp.add_argument("--resolution", default="",
                    help="Brief description of how the defect was fixed — stored in graph for audit trail")

    sp = sub.add_parser("claim-defect", help="Mark a defect as in-progress (signal intent to fix)")
    sp.add_argument("--id",       required=True, help="Defect ID, e.g. DEF-042")
    sp.add_argument("--instance", default="",    help="Your instance name — recorded as claimedBy")

    sp = sub.add_parser("unclaim-defect", help="Return an in-progress defect to open state")
    sp.add_argument("--id", required=True, help="Defect ID, e.g. DEF-042")

    sp = sub.add_parser("update-enhancement", help="Update title, description, priority, source, instance, deploy-checklist, or acceptance-criteria on an enhancement")
    sp.add_argument("--id",               required=True)
    sp.add_argument("--title",            default="")
    sp.add_argument("--description",      default="")
    sp.add_argument("--priority",         default="", choices=["","Must","Should","Could"])
    sp.add_argument("--source",           default="")
    sp.add_argument("--instance",         default="", help="Reassign to a different instance")
    sp.add_argument("--deploy-checklist", default="", dest="deploy_checklist",
                    help="Markdown checklist of deployment steps, e.g. '- [ ] restart gateway\\n- [ ] rebuild image'")
    sp.add_argument("--acceptance-criteria", default="", dest="acceptance_criteria",
                    help="What 'done' looks like — must be set before start-enhancement is run")

    sp = sub.add_parser("update-feature",
                        help="Update name, description, status, or module link on a SysFeature")
    sp.add_argument("--id",          required=True, help="Feature ID, e.g. F-SE-001")
    sp.add_argument("--name",        default="", help="Short display name")
    sp.add_argument("--description", default="", help="Full functional description")
    sp.add_argument("--narrative",   default="", help="Longer narrative text")
    sp.add_argument("--source",      default="", help="Override source tag (e.g. 'manual')")
    sp.add_argument("--status",      default="",
                    choices=["","Proposed","InProgress","Implemented","Deprecated","Superseded"],
                    help="Feature lifecycle status (case-sensitive)")
    sp.add_argument("--module",      default="",
                    help="Add a PROVIDES edge from this SysModule.id to the feature")

    sp = sub.add_parser("update-usecase", help="Update fields on a SysUseCase")
    sp.add_argument("--id",              required=True, help="UC ID, e.g. UC-DLG-001")
    sp.add_argument("--title",           default="")
    sp.add_argument("--description",     default="")
    sp.add_argument("--preconditions",   default="")
    sp.add_argument("--main-flow",         default="", dest="main_flow")
    sp.add_argument("--postconditions",    default="")
    sp.add_argument("--authorized-roles",  default="", dest="authorized_roles",
                    help="Roles permitted to perform this UC, e.g. 'Manager,Analyst'")
    sp.add_argument("--failure-scenarios", default="", dest="failure_scenarios",
                    help="Reachable failure states, e.g. 'Delegate rejects; delegation expires'")
    sp.add_argument("--priority",          default="", choices=["","P0","P1","P2","P3"])

    sp = sub.add_parser("update-story", help="Update fields on a SysUserStory")
    sp.add_argument("--id",                    required=True, help="Story ID, e.g. US-007")
    sp.add_argument("--title",                 default="")
    sp.add_argument("--actor",                 default="", help="Actor/role for the story, e.g. 'Manager'")
    sp.add_argument("--goal",                  default="")
    sp.add_argument("--benefit",               default="")
    sp.add_argument("--narrative",             default="")
    sp.add_argument("--acceptance-criteria",   default="", dest="acceptance_criteria")
    sp.add_argument("--out-of-scope",          default="", dest="out_of_scope")

    sp = sub.add_parser("create-enhancement", help="Create a new SysEnhancement with auto-incremented ID")
    sp.add_argument("--title",       required=True)
    sp.add_argument("--instance",    required=True, help="Owning instance, e.g. api, web, worker")
    sp.add_argument("--description", default="",    help="Full description")
    sp.add_argument("--priority",    default="Should", choices=["Must","Should","Could"])
    sp.add_argument("--feature",     default="",    help="Comma-separated feature IDs to link via EXTENDS")
    sp.add_argument("--source",      default="",    help="Source reference, e.g. FB-082 or ENHANCE.md")
    sp.add_argument("--acceptance-criteria", default="", dest="acceptance_criteria",
                    help="What 'done' looks like — specific test/assertion that must pass (set before starting work)")

    sp = sub.add_parser("link-enhancement", help="Link an enhancement to a feature via EXTENDS")
    sp.add_argument("--id",      required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--feature", required=True, help="Comma-separated feature IDs")

    sp = sub.add_parser("unlink-enhancement", help="Remove stale Enhancement→Feature EXTENDS edges")
    sp.add_argument("--id",      required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--feature", required=True, help="Comma-separated feature IDs to unlink")

    sp = sub.add_parser("unlink-usecase", help="Remove stale UC→Feature REQUIRES or Story→UC REALIZED_BY edges")
    sp.add_argument("--id",      required=True, help="Use case ID, e.g. UC-DLG-001")
    sp.add_argument("--feature", default="",    help="Comma-separated feature IDs to unlink")
    sp.add_argument("--story",   default="",    help="Comma-separated story IDs to remove REALIZED_BY from")

    sp = sub.add_parser("link-usecase", help="Add feature, story, or test links to an existing use case")
    sp.add_argument("--id",      required=True, help="Use case ID, e.g. UC-AUTH-001")
    sp.add_argument("--feature", default="", help="Comma-separated SysFeature IDs → REQUIRES")
    sp.add_argument("--story",   default="", help="Comma-separated SysUserStory IDs → REALIZED_BY")
    sp.add_argument("--tests",   default="", help="Comma-separated SysTest IDs → VERIFIES UC's features")

    sp = sub.add_parser("close-enhancement", help="Mark an enhancement as done")
    id_grp = sp.add_mutually_exclusive_group(required=True)
    id_grp.add_argument("--id",  default="", help="Single enhancement ID, e.g. ENH-024")
    id_grp.add_argument("--ids", default="", help="Comma-separated batch, e.g. ENH-024,ENH-025,ENH-026")
    sp.add_argument("--instance", default="",    help="Your instance name — warns on mismatch")
    sp.add_argument("--verify-entity", default="", metavar="TYPES",
                    help="Comma-sep entity types to verify exist in graph (e.g. SysFeature,SysUseCase)")
    sp.add_argument("--skip-checklist", action="store_true",
                    help="Suppress the pre-close checklist reminder")
    sp.add_argument("--graph-only", action="store_true",
                    help="Enhancement made no code changes — skip endpoint/symbol/test checklist")
    sp.add_argument("--test-only",  action="store_true",
                    help="Enhancement added tests only — skip endpoint/symbol checklist items")
    sp.add_argument("--force", action="store_true",
                    help="Close even when linked features have no verifying tests (documents test debt)")
    sp.add_argument("--confirm-tests", action="store_true", dest="confirm_tests",
                    help="Confirm that integration, unit, and regression tests were run and passed (required unless --graph-only / --test-only / --force)")

    sp = sub.add_parser("reconcile-done",
                        help=argparse.SUPPRESS)
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--id",     default="", help="Enhancement ID, e.g. ENH-024")
    grp.add_argument("--defect", default="", help="Defect ID, e.g. DEF-001")
    sp.add_argument("--instance", default="", help="Calling session instance — used for scope decisions")

    sp = sub.add_parser("start-enhancement", help="Mark an enhancement as in-progress")
    id_grp2 = sp.add_mutually_exclusive_group(required=True)
    id_grp2.add_argument("--id",  default="", help="Single enhancement ID, e.g. ENH-024")
    id_grp2.add_argument("--ids", default="", help="Comma-separated batch, e.g. ENH-024,ENH-025,ENH-026")
    sp.add_argument("--instance", default="",    help="Your instance name — warns on mismatch")
    sp.add_argument("--skip-ac-check", action="store_true", dest="skip_ac_check",
                    help="Skip acceptance-criteria gate (use for graph-only or documentation-only enhancements)")

    sp = sub.add_parser("approve-enhancement", help="Promote a proposed enhancement to approved state")
    sp.add_argument("--id", required=True, help="Enhancement ID, e.g. ENH-024")

    sp = sub.add_parser("abandon-enhancement", help="Return an in-progress enhancement to proposed (release claim)")
    sp.add_argument("--id", required=True, help="Enhancement ID, e.g. ENH-024")

    sp = sub.add_parser("reopen-enhancement",
                        help="Return a done/approved enhancement to proposed state (recovery from accidental close)")
    sp.add_argument("--id", required=True, help="Enhancement ID, e.g. ENH-042")

    sp = sub.add_parser("wontfix-enhancement", help="Close an enhancement as wont-fix")
    sp.add_argument("--id",     required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--reason", default="",    help="Optional reason for wont-fix decision")

    sp = sub.add_parser("defer-enhancement", help="Mark an enhancement as deferred (not in current scope)")
    sp.add_argument("--id",     required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--reason", default="",    help="Optional reason for deferral")

    sp = sub.add_parser("show-enhancement", help="Display full details of an enhancement")
    sp.add_argument("--id", required=True, help="Enhancement ID, e.g. ENH-024")

    sp = sub.add_parser("list-defects",
                        help="List defects for an instance (default: open)")
    sp.add_argument("--instance", default="", help="Filter by instance name")
    sp.add_argument("--status", default="open",
                    choices=["open", "closed", "all", "in-progress"],
                    help="Status filter (default: open)")

    sp = sub.add_parser("list-enhancements",
                        help="List enhancements for an instance (default: open)")
    sp.add_argument("--instance", default="", help="Filter by instance name")
    sp.add_argument("--status", default="open",
                    choices=["open", "done", "proposed", "in-progress", "approved", "all"],
                    help="Status filter (default: open)")
    sp.add_argument("--priority", default="",
                    choices=["", "Must", "Should", "Could"],
                    help="Filter by priority")

    sp = sub.add_parser("show-usecase", help="Display full details of a SysUseCase")
    sp.add_argument("--id", required=True, help="UC ID, e.g. UC-DLG-001")

    sp = sub.add_parser("list-usecases", help="List use cases, optionally filtered by instance, module, or story")
    sp.add_argument("--instance", default="", help="Filter by owning instance, e.g. manage")
    sp.add_argument("--module",   default="", help="Filter by SysModule ID, e.g. MOD-dlg")
    sp.add_argument("--story",    default="", help="Filter by SysUserStory ID, e.g. US-007")

    sp = sub.add_parser("show-defect", help="Display full details of a defect")
    sp.add_argument("--id", required=True, help="Defect ID, e.g. DEF-042")

    sp = sub.add_parser("show-feature", help="Display full details of a feature including tests, UCs, defects")
    sp.add_argument("--id", required=True, help="Feature ID, e.g. F-API-002")

    sp = sub.add_parser("show-story", help="Display full details of a user story including linked use cases")
    sp.add_argument("--id", required=True, help="Story ID, e.g. US-CTM-010")

    sp = sub.add_parser("update-defect", help="Patch title, description, severity, or acceptance-criteria on a defect")
    sp.add_argument("--id",          required=True, help="Defect ID, e.g. DEF-042")
    sp.add_argument("--title",       default="", help="New title")
    sp.add_argument("--description", default="", help="New or updated description")
    sp.add_argument("--severity",    default="", choices=["","critical","high","medium","low"])
    sp.add_argument("--acceptance-criteria", default="", dest="acceptance_criteria",
                    help="The specific assertion that currently fails and must pass after the fix")


    # ── Notes (instance memory) ───────────────────────────────────────────────
    sp = sub.add_parser("add-note",
                        help="Add a SysNote — instance memory for design context and session reminders")
    sp.add_argument("--instance", required=True, help="Your instance name")
    sp.add_argument("--body",     required=True, help="Note body text")
    sp.add_argument("--expires",  default="",   help="Optional ISO date when this note becomes stale, e.g. 2026-06-04")

    sp = sub.add_parser("show-notes", help="Show SysNote entries for an instance")
    sp.add_argument("--instance", required=True)
    sp.add_argument("--all", action="store_true", help="Include archived notes")

    sp = sub.add_parser("expire-note",
                        help="Mark a SysNote as expired (removes from worklog without deleting)")
    sp.add_argument("--id", required=True, help="Note ID, e.g. NOTE-001")

    sp = sub.add_parser("link-story", help="Add REQUIRES edges from a user story to features")
    sp.add_argument("--story",    required=True, help="Story ID, e.g. US-016")
    sp.add_argument("--features", required=True, help="Comma-separated feature IDs")

    sp = sub.add_parser("show-feature-tests",
                        help="Show tests linked to a feature + unlinked tests in the same module area")
    sp.add_argument("--feature", required=True, help="Feature ID, e.g. F-API-002")

    sp = sub.add_parser("retire-feature", help="Mark a feature as Superseded (excluded from gap reports)")
    sp.add_argument("--id",     required=True, help="Feature ID, e.g. F-IMP-005")
    sp.add_argument("--reason", default="",    help="Why this feature was retired")

    sp = sub.add_parser("coverage-report",
                        help="4-tier V-model gap analysis: US→UC→Feature→Test traceability")
    sp.add_argument("--instance", default="", help="Scope T2/T3 to one instance (T1/T4 always global)")
    sp.add_argument("--tier", default="all", choices=["all","1","2","3","4"],
                    help="Show one tier only (default: all)")
    sp.add_argument("--format", default="md", choices=["md","text"],
                    help="Output format: md (default) or text summary")

    sp = sub.add_parser("coverage-review",
                        help="AI sufficiency check on US→UC and UC→Feature chains (assistant-driven by default)")
    sp.add_argument("--scope",    default="both", choices=["us","uc","both"],
                    help="Which chain to review: us (story→UC), uc (UC→feature), or both (default)")
    sp.add_argument("--instance", default="",
                    help="Scope to one instance's modules/UCs")
    sp.add_argument("--story",    default="",
                    help="Review a single user story, e.g. US-007 (implies --scope us)")
    sp.add_argument("--module",   default="",
                    help="Scope to one module, e.g. MOD-delegation")
    sp.add_argument("--model",    default="haiku", choices=["haiku","sonnet"],
                    help="Claude model to use for direct mode (default: haiku)")
    sp.add_argument("--file-proposals", action="store_true", dest="file_proposals",
                    help="Create SysProposal nodes for every GAP finding")
    sp.add_argument("--us",       default="",
                    help="Single US id — runs AS-REQ dimensional review (e.g. US-CTM-010)")
    sp.add_argument("--uc",       default="",
                    help="Single UC id — runs AS-REQ internal adequacy review (e.g. UC-DLG-001)")
    sp.add_argument("--provider", default="",
                    help="Force a specific AI provider for direct mode. "
                         "Overrides SYSEDGE_AI_PROVIDERS. Graph content is sent to this provider.")
    sp.add_argument("--analyse", action="store_true",
                    help="Emit an assistant-driven analysis request instead of calling AI directly")
    sp.add_argument("--output-request", default="", dest="output_request",
                    help="Write the analysis request JSON to this file (default: print to stdout)")
    sp.add_argument("--apply-results", default="", dest="apply_results",
                    help="Apply assistant-produced result JSON from this file")
    sp.add_argument("--batch-size", type=int, default=1, dest="batch_size",
                    help="Number of subjects per request in --analyse mode (default: 1)")

    sp = sub.add_parser("audit-status",
                        help="Show last audit-test and coverage-review timestamps per UC/US")
    sp.add_argument("--instance", default="", help="Filter to one instance's UCs/USs")
    sp.add_argument("--days",     type=int, default=14, help="Flag items older than N days (default: 14)")
    sp.add_argument("--scope",    default="both", choices=["uc","us","both"],
                    help="Show use cases, user stories, or both (default: both)")

    sp = sub.add_parser("audit-test",
                        help="AI test adequacy audit against AS-TEST architecture standards")
    sp.add_argument("--uc",       default="", help="Use case id, e.g. UC-DLG-001 (default tier: usecase)")
    sp.add_argument("--ucs",      default="", help="Comma-separated UC ids for batch mode, e.g. UC-A,UC-B")
    sp.add_argument("--feature",  default="", help="Feature id, e.g. F-DLG-001 (default tier: integration)")
    sp.add_argument("--us",       default="", help="User story id, e.g. US-CTM-010 (default tier: e2e)")
    sp.add_argument("--file",     default="", help="Override test file path (else auto-discovered from graph)")
    sp.add_argument("--tier",     default="", choices=["", "usecase", "integration", "e2e"],
                    help="Override tier: usecase | integration | e2e")
    sp.add_argument("--model",    default="sonnet", choices=["haiku", "sonnet"],
                    help="Claude model to use for direct mode (default: sonnet)")
    sp.add_argument("--preflight", action="store_true",
                    help="Show which test file would be used without running the LLM audit")
    sp.add_argument("--provider", default="",
                    help="Force a specific AI provider for direct mode. "
                         "Overrides SYSEDGE_AI_PROVIDERS. Graph content is sent to this provider.")
    sp.add_argument("--analyse", action="store_true",
                    help="Emit an assistant-driven analysis request instead of calling AI directly")
    sp.add_argument("--output-request", default="", dest="output_request",
                    help="Write the analysis request JSON to this file (default: print to stdout)")
    sp.add_argument("--apply-results", default="", dest="apply_results",
                    help="Apply assistant-produced result JSON from this file")

    sp = sub.add_parser("link-blocks", help="Mark one enhancement as BLOCKED_BY another")
    sp.add_argument("--id",         required=True, help="Enhancement that is blocked, e.g. ENH-042")
    sp.add_argument("--blocked-by", required=True, dest="blocked_by",
                    help="Enhancement that is blocking, e.g. ENH-020")

    sp = sub.add_parser("unlink-blocks",
                        help="Remove incorrect BLOCKED_BY edge(s) from an enhancement")
    sp.add_argument("--id",      required=True, help="Enhancement whose BLOCKED_BY edges to remove")
    sp.add_argument("--unblock", required=True,
                    help="Comma-separated ENH ids to unblock from, e.g. ENH-020,ENH-021")

    sp = sub.add_parser("migrate-binary",
                        help=argparse.SUPPRESS)
    sp.add_argument("--from", required=True, dest="from_binary", help="Source binary name, e.g. core")
    sp.add_argument("--to",   required=True, dest="to_binary",   help="Target binary name, e.g. platform")
    sp.add_argument("--features", default="", help="Comma-separated feature IDs to scope the migration")

    sp = sub.add_parser("create-usecase", help="Create or update a SysUseCase node")
    sp.add_argument("--id",                required=True)
    sp.add_argument("--title",             default="", help="Required when creating; optional when updating existing UC")
    sp.add_argument("--instance",          required=True)
    sp.add_argument("--name",              default="", help="Slug (defaults to kebab-case of title)")
    sp.add_argument("--description",       default="")
    sp.add_argument("--priority",          default="P1", choices=["P0","P1","P2","P3"])
    sp.add_argument("--story",             default="", help="Comma-separated SysUserStory IDs")
    sp.add_argument("--feature",           default="", help="Comma-separated SysFeature IDs")
    sp.add_argument("--preconditions",     default="", help="Cockburn preconditions field")
    sp.add_argument("--main-flow",         default="", dest="main_flow", help="Cockburn main flow steps")
    sp.add_argument("--postconditions",    default="", help="Cockburn postconditions field")
    sp.add_argument("--authorized-roles",  default="", dest="authorized_roles", help="Roles authorised to perform this UC")
    sp.add_argument("--failure-scenarios", default="", dest="failure_scenarios", help="Exception/failure paths")

    sp = sub.add_parser("create-feature", help="Create a SysFeature linked to a module")
    sp.add_argument("--id",          required=True)
    sp.add_argument("--name",        required=True)
    sp.add_argument("--module",      required=True, help="SysModule.id")
    sp.add_argument("--status",      default="Proposed",
                    choices=["Proposed","InProgress","Implemented","Deprecated","Superseded"])
    sp.add_argument("--description", default="")
    sp.add_argument("--narrative",   default="")
    sp.add_argument("--source",      default="manual")
    sp.add_argument("--story",       default="", help="Comma-separated SysUserStory IDs")

    sp = sub.add_parser("create-adr", help="Create a SysArchDecision (ADR) node")
    sp.add_argument("--id",           required=True)
    sp.add_argument("--title",        required=True)
    sp.add_argument("--status",       default="proposed",
                    choices=["proposed","adopted","deprecated","superseded"])
    sp.add_argument("--date",         default="")
    sp.add_argument("--decision",     default="")
    sp.add_argument("--description",  default="", help="Alias for --decision (same field)")
    sp.add_argument("--context",      default="")
    sp.add_argument("--consequences", default="")
    sp.add_argument("--addresses",    default="", help="Comma-separated SysArchStd IDs")
    sp.add_argument("--supersedes",   default="", help="Comma-separated ADR IDs this supersedes")

    sp = sub.add_parser("merge-nodes", help="Merge a duplicate node into another (edges and properties)")
    sp.add_argument("--type",     required=True,
                    help="Node label, e.g. SysFeature, SysUseCase, SysUserStory")
    sp.add_argument("--keep",     required=True, help="ID of node to keep")
    sp.add_argument("--absorb",   required=True, help="ID of node to absorb (will be deleted)")
    sp.add_argument("--strategy", default="union",
                    choices=["union","prefer-keep","prefer-absorb"])

    sp = sub.add_parser("preview-import", help="Preview importing stories/use cases from a document")
    sp.add_argument("file", help="Path to sysedge.import.v1 JSON file")

    sp = sub.add_parser("commit-import", help="Commit a reviewed import decisions file")
    sp.add_argument("file",        help="Path to sysedge.import.v1 JSON file")
    sp.add_argument("--decisions", required=True, help="Path to decisions JSON (from preview-import)")

    sp = sub.add_parser("analyse", help="Analyse graph for orphans, merge candidates, or split candidates")
    sp.add_argument("--instance",  default="")
    sp.add_argument("--merge",     action="store_true", help="Find merge candidates")
    sp.add_argument("--split",     action="store_true", help="Find split candidates")
    sp.add_argument("--orphans",   action="store_true", help="Find orphan nodes")
    sp.add_argument("--threshold", type=float, default=0.80, help="Merge similarity threshold (default 0.80)")
    sp.add_argument("--max-responsibilities", type=int, default=3, dest="max_responsibilities",
                    help="Max UCs per feature before flagging as split candidate (default 3)")
    sp.add_argument("--json",      action="store_true", help="Output as JSON")

    sp = sub.add_parser("export", help="Export graph slice as Markdown or JSON document")
    sp.add_argument("--type",     required=True,
                    choices=["stories","use-cases","application-arch","technical-arch",
                             "test-coverage","test-summary","detailed-design"])
    sp.add_argument("--instance", default="")
    sp.add_argument("--module",   default="")
    sp.add_argument("--output",   default="")
    sp.add_argument("--format",   default="md", choices=["md","json"])

    sp = sub.add_parser("create-story", help="Create a user story")
    sp.add_argument("--id",                  required=True, help="Story ID, e.g. US-015")
    sp.add_argument("--title",               required=True)
    sp.add_argument("--actor",               default="")
    sp.add_argument("--goal",                default="")
    sp.add_argument("--benefit",             default="")
    sp.add_argument("--acceptance-criteria", default="", dest="acceptance_criteria",
                    help="Testable acceptance criteria for this story")
    sp.add_argument("--priority",            default="Should", choices=["Must","Should","Could","Won't"])
    sp.add_argument("--features",            default="", help="Comma-separated feature IDs")
    sp.add_argument("--user",                default="", help="SysUser ID that initiates this story")

    sp = sub.add_parser("actor-coverage", help="Show actor×feature story coverage matrix")
    sp.add_argument("--instance", default="", help="Instance name, e.g. manage")
    sp.add_argument("--feature",  default="", help="Single feature ID, e.g. F-MGT-001")

    sp = sub.add_parser("uc-state-check", help="AI check that UC postconditions align with related UC preconditions")
    sp.add_argument("--instance", default="", help="Instance name — checks all UC pairs in instance")
    sp.add_argument("--uc",       default="", help="Single UC ID — checks against all shared-story UCs")
    sp.add_argument("--model",    default="haiku", choices=["haiku","sonnet"])
    sp.add_argument("--provider", default="")

    sp = sub.add_parser("enh-align", help="AI check that an ENH description covers its linked UC behaviours")
    sp.add_argument("--id",       default="", help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--instance", default="", help="Instance — checks all open ENHs in instance")
    sp.add_argument("--model",    default="haiku", choices=["haiku","sonnet"])
    sp.add_argument("--provider", default="")

    sp = sub.add_parser("link-feature",
                        help="Bulk-link matching scanned tests to a feature by pattern")
    sp.add_argument("--feature", required=True, help="Feature ID, e.g. F-API-001")
    sp.add_argument("--tests",   required=True,
                    help="Comma-separated substrings matched against SysTest.id "
                         "(pytest node ID format: file.py::ClassName or file.py::fn)")
    sp.add_argument("--tier", default="",
                    choices=["", "component", "integration", "usecase", "e2e"],
                    help="Override testType on matched tests (useful for pure-Go projects "
                         "where scan-go-tests infers [integration] but tests are component-tier)")

    sp = sub.add_parser("link-endpoint", help="Link an API endpoint to a feature")
    sp.add_argument("--feature",    required=True)
    sp.add_argument("--method",     required=True)
    sp.add_argument("--path",       required=True)
    sp.add_argument("--binary",     default="")
    sp.add_argument("--permission", default="")

    sp = sub.add_parser(
        "link-symbol",
        help="Register a code symbol and link it to a feature (IMPLEMENTS + CONTAINS_SYMBOL).",
        description=(
            "Creates a SysSymbol node with an IMPLEMENTS edge to each --feature. "
            "Also creates a CONTAINS_SYMBOL edge from the owning module so that briefing "
            "symbol counts are correct. The owning module is inferred from registered "
            "codePaths; pass --module to set it explicitly. "
            "Use scan-code to bulk-register symbols from source without linking to features."
        ),
    )
    sp.add_argument("--feature", required=True,
                    help="Feature ID(s) this symbol implements, comma-separated")
    sp.add_argument("--file",    required=True,
                    help="Repo-relative file path, e.g. src/api/handlers.go")
    sp.add_argument("--symbol",  required=True,
                    help="Symbol name, e.g. HandleGetPositions")
    sp.add_argument("--line",    type=int, default=0,
                    help="Line number of the symbol definition (optional)")
    sp.add_argument("--symtype", default="handler",
                    help="Symbol kind: handler, function, type, constant, etc. (default: handler)")
    sp.add_argument("--module",  default="",
                    help="SysModule id to attach CONTAINS_SYMBOL (inferred from codePaths if omitted)")

    sp = sub.add_parser(
        "refresh-lines",
        help="Re-resolve SysSymbol line numbers by symbol name — patches stale line offsets without re-registration",
        description=(
            "Reads each registered symbol file, finds the symbol by name (regex), "
            "and updates sym.line if it has drifted. Safe to run at session start. "
            "Does not change sym.id, sym.symbol, sym.file, or any relationships.\n\n"
            "Scope: pass --instance, --module, or --file (at least one required).\n\n"
            "Language support: .go  .py  .ts  .tsx  .js  .jsx"
        ),
    )
    sp.add_argument("--instance", default="", help="Refresh all symbols for modules in this instance")
    sp.add_argument("--module",   default="", help="Refresh symbols owned by this SysModule id")
    sp.add_argument("--file",     default="", help="Refresh symbols whose file path contains this substring")
    sp.add_argument("--root",     default="", help="Prepend to relative file paths (e.g. /home/user/MyProject)")
    sp.add_argument("--dry-run",  action="store_true", help="Print changes without writing to graph")

    sp = sub.add_parser("features", help="List features for a module")
    sp.add_argument("--module", required=True)

    sub.add_parser("status",  help="Cross-instance dashboard: enhancements, defects, coverage across all instances")

    sp = sub.add_parser("full-inventory",
                        help="Full cross-instance work inventory grouped by instance — primarily for the architect session")
    sp.add_argument("--instance", default="",
                    help="Limit per-instance detail to this instance (summary table always shows all)")
    sp.add_argument("--system", default="",
                    help="Scope to a system/repo — filters instances to those in that SysSystem node "
                         "(e.g. --system SYS-CE for CoreEdge, --system rms for OrgEdge)")

    sp = sub.add_parser("search", help="Free-text search across features, enhancements, defects, use cases, stories")
    sp.add_argument("--query", required=True, help="Search string (case-insensitive, matched on title and description)")
    sp.add_argument("--type",  default="",    help="Comma-separated node types to search, e.g. SysFeature,SysDefect (default: all)")
    sp.add_argument("--limit", type=int, default=20, help="Max results per node type (default: 20)")
    sp = sub.add_parser("stories", help="List all user stories and their features")
    sp.add_argument("--gap", action="store_true",
                    help="Show only user stories with no linked use case (REALIZED_BY)")
    sub.add_parser("scenarios", help="Show scenarios and training with decomposition status")

    sp = sub.add_parser("link-chapter", help="Link a scenario chapter to a feature")
    sp.add_argument("--chapter", required=True, help="Chapter ID, e.g. SCN-001-CH-02")
    sp.add_argument("--feature", required=True)

    sp = sub.add_parser("link-scenario-story",
                        help=argparse.SUPPRESS)
    sp.add_argument("--scenario", required=True, help="Scenario ID, e.g. SCN-004")
    sp.add_argument("--story",    required=True, help="User story ID, e.g. US-042")

    sp = sub.add_parser("link-training", help="Link a training module to a feature")
    sp.add_argument("--module",  required=True, help="Training module ID, e.g. TRN-MGR-03")
    sp.add_argument("--feature", required=True)

    sp = sub.add_parser("record-run", help="Record a test-run result for a package")
    sp.add_argument("--package",  required=True, help="Test package path, e.g. tests/integration")
    sp.add_argument("--passed",   type=int, default=0)
    sp.add_argument("--failed",   type=int, default=0)
    sp.add_argument("--skipped",  type=int, default=0)
    sp.add_argument("--xfailed",  type=int, default=0, help="Expected failures (xfail)")
    sp.add_argument("--duration", type=float, default=0.0, help="Total run duration in seconds")
    sp.add_argument("--notes",       default="", help="Optional run notes")
    sp.add_argument("--skip-reason", default="", dest="skip_reason",
                    help="Why tests were skipped: missing-fixture | code-bug | ai-dependent | env-config | other")
    sp.add_argument("--results-json", default="", dest="results_json", metavar="PATH",
                    help="Path to pytest-json-report JSON file for per-test outcome accuracy "
                         "(pip install pytest-json-report; run with --json-report --json-report-file=results.json)")

    sp = sub.add_parser("run-history", help="Show pass-rate trend from SysTestRun history, with flaky detection")
    sp.add_argument("--package",  default="", help="Filter to a single test package ID")
    sp.add_argument("--instance", default="", help="Filter to packages for this instance")
    sp.add_argument("--runs",     type=int, default=10, help="Number of recent runs to show per package (default: 10)")

    sp = sub.add_parser("prune-runs", help="Delete old SysTestRun nodes to control graph size")
    sp.add_argument("--days",     type=int, default=30, help="Prune runs older than N days (default: 30)")
    sp.add_argument("--keep",     type=int, default=5,  help="Always keep at least N recent runs per package (default: 5)")
    sp.add_argument("--dry-run",  action="store_true", dest="dry_run", help="Report what would be pruned without deleting")

    sp = sub.add_parser("prune-stale-tests",
                        help="Find and remove stale SysTest→Feature VERIFIES edges (legacy T- nodes, renamed/deleted classes)")
    sp.add_argument("--feature",  default="", help="Limit scan to features whose id starts with this prefix (e.g. F-MST)")
    sp.add_argument("--dry-run",  action="store_true", dest="dry_run", help="Report stale links without deleting (default behaviour)")
    sp.add_argument("--yes",      action="store_true", help="Delete without interactive prompt")

    sp = sub.add_parser("check-test-links",
                        help="Check test file paths against this repo; --fix re-keys mis-formatted paths and deletes tests whose file is gone")
    sp.add_argument("--fix",     action="store_true", help="Apply repairs (default: dry run)")
    sp.add_argument("--yes",     action="store_true", help="Apply without interactive prompt")
    sp.add_argument("--verbose", action="store_true", help="List every affected node")

    sp = sub.add_parser("scan-endpoints", help="Scan Go source for route registrations and diff against graph SysEndpoint nodes")
    sp.add_argument("--path",     default="",
                    help="Root directory to scan for Go route registrations (default: sysedge.yaml go_root, else .)")
    sp.add_argument("--binary",   default="", help="Filter to a specific binary (core|tools|platform|licence-admin)")
    sp.add_argument("--instance", default="", help="Filter graph side to endpoints linked to this instance's features")
    sp.add_argument("--apply",    action="store_true",
                    help="Upsert missing/mismatched endpoints into graph (no IMPLEMENTS edge — run link-endpoint to link)")

    sp = sub.add_parser("test-status", help="Show last-run timestamps and stale tests")
    sp.add_argument("--days",     type=int, default=7,  help="Flag tests not run in this many days (default 7)")
    sp.add_argument("--instance", default="",           help="Filter by instance (e.g. api, web, worker)")

    sp = sub.add_parser("backup", help="Export all Sys* nodes to a seed-compatible JSON file")
    sp.add_argument("--output", default="", help="Output path (default: data/sys-backup-YYYY-MM-DD.json)")
    sp.add_argument("--allow-empty", action="store_true", dest="allow_empty",
                    help="Write a backup even when the graph has no SysEdge nodes")

    sp = sub.add_parser("reset", help="Delete all SysEdge nodes after a backup (interactive terminal only)")

    sp = sub.add_parser("changes", help="Change log of requirement edits (who, when, what); --verify checks the hash chain")
    sp.add_argument("--id",     default="", help="Only changes to this node")
    sp.add_argument("--limit",  type=int, default=50)
    sp.add_argument("--verify", action="store_true", help="Verify the tamper-evident hash chain")
    sp.add_argument("--json",   action="store_true")

    sp = sub.add_parser("set-external-ref", help="Attach an external tracker ref (jira, ado, polarion…) to a node")
    sp.add_argument("--id",     required=True, help="Story, use case, feature, defect or enhancement id")
    sp.add_argument("--system", required=True, help="Tracker name, e.g. jira, ado, polarion, doors")
    sp.add_argument("--ref",    required=True, help="Key in that system, e.g. PROJ-123")
    sp.add_argument("--remove", action="store_true", help="Remove the reference instead")

    sp = sub.add_parser("baseline", help="Freeze, list, show or diff requirement/test baselines")
    sp.add_argument("action", choices=["create", "list", "show", "diff"])
    sp.add_argument("--name",     default="", help="Baseline name, e.g. R1.0")
    sp.add_argument("--against",  default="", help="diff: compare with this baseline (default: current graph)")
    sp.add_argument("--instance", default="")
    sp.add_argument("--module",   default="")
    sp.add_argument("--note",     default="", help="create: free-text note stored with the baseline")
    sp.add_argument("--json",     action="store_true")

    sp = sub.add_parser("trace-matrix", help="Traceability matrix export (csv|xlsx|json) with sha256 manifest, optional signature")
    sp.add_argument("--format",   default="csv", choices=["csv", "xlsx", "json"])
    sp.add_argument("--baseline", default="", help="Export a frozen baseline instead of the current graph")
    sp.add_argument("--instance", default="")
    sp.add_argument("--module",   default="")
    sp.add_argument("--output",   default="")
    sp.add_argument("--sign",     action="store_true", help="Write a detached Ed25519 signature")
    sp.add_argument("--sign-key", default="", dest="sign_key",
                    help="Ed25519 private key file (PEM or hex); default SYSEDGE_SIGNING_KEY_FILE")

    sp = sub.add_parser("reqif", help="Export requirements as ReqIF 1.2, or import ReqIF SPEC-OBJECTs as user stories")
    sp.add_argument("action", choices=["export", "import"])
    sp.add_argument("file", nargs="?", default="", help="export: output path; import: ReqIF file")
    sp.add_argument("--instance", default="")
    sp.add_argument("--module",   default="")
    sp.add_argument("--title",    default="", help="export: specification title")
    sp.add_argument("--prefix",   default="US-REQIF", help="import: id prefix for new stories")
    sp.add_argument("--apply",    action="store_true", help="import: write (default is a dry run)")

    sp = sub.add_parser("config", help="Show or set project settings (sysedge.yaml), e.g. model lightweight|full")
    sp.add_argument("action", choices=["get", "set"])
    sp.add_argument("key", nargs="?", default="")
    sp.add_argument("value", nargs="?", default="")

    sp = sub.add_parser("impact", help="Impact analysis: file or symbol → which features and tests are affected")
    sp.add_argument("--path",   default="", help="File path substring to trace (e.g. src/api/)")
    sp.add_argument("--symbol", default="", help="Symbol name to trace (e.g. GetPositions)")

    sp = sub.add_parser("show-blockers", help="Show all BLOCKED_BY chains across enhancements")
    sp.add_argument("--instance", default="", help="Filter to enhancements belonging to this instance")

    sp = sub.add_parser("link-commit", help="Record a git commit SHA as a note linked to an enhancement or defect")
    sp.add_argument("--sha",         required=True, help="Git commit SHA (full or abbreviated)")
    sp.add_argument("--message",     default="",    help="Commit message summary (optional)")
    sp.add_argument("--enhancement", default="",    help="Enhancement ID to link to")
    sp.add_argument("--defect",      default="",    help="Defect ID to link to")
    sp.add_argument("--instance",    default="",    help="Instance name for the note")

    sub.add_parser("health-report",
                   help="Single-screen system-wide health snapshot: defects, ENHs, coverage, warnings, backup")

    sp = sub.add_parser("session-close", help="Composite end-of-session: health check + worklog summary + backup")
    sp.add_argument("--instance",  default="", help="Instance name for work summary (optional)")
    sp.add_argument("--no-backup", action="store_true", dest="no_backup", help="Skip the backup step")

    sp = sub.add_parser("add-standard", help="Add a project-specific SysArchStd node (tier=project)")
    sp.add_argument("--id",           required=True, help="Standard ID, e.g. AS-TEST-WIZ-001")
    sp.add_argument("--title",        required=True, help="Short title")
    sp.add_argument("--description",  required=True, help="Full standard description")
    sp.add_argument("--category",     default="quality", help="Category, e.g. testing, quality, security")
    sp.add_argument("--applies-to",   default="SysUseCase", dest="applies_to",
                    help="Comma-separated node labels, e.g. SysUseCase,SysFeature")
    sp.add_argument("--applies-when", default="", dest="applies_when",
                    help="Condition, e.g. wizardBased=true (optional)")
    sp.add_argument("--evaluation",   default="manual", choices=["automated","manual","ai"],
                    help="Evaluation method: automated (graph query), manual (checklist), ai (LLM assessment)")
    sp.add_argument("--enforcement",  default="mandatory", choices=["mandatory","guidance"],
                    help="mandatory: gates close on fail; guidance: surfaces as suggestion (default: mandatory)")

    sp = sub.add_parser("quality-review", help="Evaluate all applicable standards for an entity or instance")
    sp.add_argument("--entity",   default="", help="Entity ID, e.g. UC-DLG-001 or F-DLG-001")
    sp.add_argument("--instance", default="", help="Instance name — evaluates all entities in instance")
    sp.add_argument("--type",     default="", help="Filter entity type for --instance mode, e.g. SysUseCase")
    sp.add_argument("--ai",       action="store_true", help="Enable AI evaluation of guidance+ai standards (uses Claude Code session tokens or ANTHROPIC_API_KEY)")
    sp.add_argument("--model",    default="haiku", choices=["haiku","sonnet"], help="Model for --ai mode (default: haiku)")
    sp.add_argument("--provider", default="",
                    help="Force a specific AI provider for --ai mode. Overrides SYSEDGE_AI_PROVIDERS.")


    sp = sub.add_parser("scan-go-tests",
                        help="Scan Go *_test.go files and register as component-category SysTest nodes")
    sp.add_argument("--path", default="",
                    help="Root directory to scan (default: sysedge.yaml go_root, else .)")

    # ── scan-tests: register Python test functions (replaces scan-package / scan-all)
    sp = sub.add_parser(
        "scan-tests",
        help="Register test functions from Python/TypeScript test files into SysTest nodes (additive MERGE)",
    )
    grp = sp.add_mutually_exclusive_group(required=False)
    grp.add_argument("--path",   metavar="FILE",   help="Single test file path or SysTestPackage id")
    grp.add_argument("--all",    action="store_true", help="Discover and scan all test files")
    grp.add_argument("--module", metavar="MODULE", help="SysModule id — scans test packages for that module's area")
    sp.add_argument("--root",     default="", metavar="ROOT",
                    help="Filter --all by canonicalRoot (e.g. tests, vitest, go)")
    sp.add_argument("--area",     default="", metavar="AREA",
                    help="Filter --all by area (test directory segment)")
    sp.add_argument("--instance", default="", metavar="INSTANCE",
                    help="Filter --all by instance (e.g. api, web)")
    sp.add_argument("--category", default="",
                    choices=["", "component", "integration", "usecase", "e2e"],
                    help="Override derived category: component | integration | usecase | e2e")
    sp.add_argument("--yes", action="store_true",
                    help="With --all, scan all discovered test directories without prompting")
    sp.add_argument("--analyse", action="store_true",
                    help="Generate an assistant-driven analysis request for unlinked test files instead of calling AI directly")
    sp.add_argument("--max-files", type=int, default=50, dest="max_files",
                    help="Max files to include in one analysis request (default: 50; 0=unlimited)")
    sp.add_argument("--output-request", default="", dest="output_request",
                    help="Write the analysis request JSON to this file (default: print to stdout)")
    sp.add_argument("--apply-ai-results", default="", dest="apply_ai_results",
                    help="Apply AI mapping results from a JSON file produced by the assistant")

    # ── scan-features: auto-create Feature nodes from a diff ─────────────────
    sp = sub.add_parser(
        "scan-features",
        help="Detect new exported symbols since a commit and propose/create Feature nodes",
        description=(
            "Diffs changed code files since --since <git-sha>, extracts new exported symbols "
            "that have no IMPLEMENTS edge, and proposes SysFeature nodes + ENH→EXTENDS→Feature "
            "edges. With --enh, scopes to one ENH and derives the Feature name from its title "
            "(recommended). With --feature, wires symbols to an existing Feature instead of "
            "creating a new one. Writes only after confirmation (or --yes/--dry-run).\n\n"
            "Typical workflow:\n"
            "  scan-features --since <start-sha> --enh ENH-xxx --dry-run\n"
            "  scan-features --since <start-sha> --enh ENH-xxx --yes"
        ),
    )
    sp.add_argument("--since", metavar="SHA",
                    help="Git ref (commit SHA or tag) to diff from. Defaults to HEAD~1.")
    sp.add_argument("--enh", metavar="ENH_ID",
                    help="ENH to scope this run to: one Feature is proposed per --enh call, "
                         "named from the ENH title. Recommended — avoids ambiguous multi-ENH grouping.")
    sp.add_argument("--feature", metavar="F_ID",
                    help="Wire unlinked symbols to this EXISTING Feature instead of creating one "
                         "(requires --enh to set the ENH→EXTENDS edge). Use when the Feature already exists.")
    sp.add_argument("--instance", metavar="INST",
                    help="Filter candidate ENH nodes to this instance (used when --enh is omitted)")
    sp.add_argument("--module", metavar="MODULE",
                    help="Limit to symbols belonging to this SysModule id")
    sp.add_argument("--yes", action="store_true",
                    help="Write proposals without interactive confirmation")
    sp.add_argument("--dry-run", action="store_true",
                    help="Preview proposals without writing anything to the graph")

    # ── scan-code: extract symbols from Go/TypeScript source (replaces scan-code / scan-code-all)
    sp = sub.add_parser(
        "scan-code",
        help="Bulk-register exported symbols from source into SysSymbol nodes with CONTAINS_SYMBOL edges.",
        description=(
            "Scans Go/TypeScript source and upserts SysSymbol nodes with CONTAINS_SYMBOL edges "
            "from the owning module. Does NOT create IMPLEMENTS edges — use link-symbol or "
            "link-endpoint to link specific symbols to features. "
            "Typical workflow: scan-code --all to register all symbols, then link-symbol to "
            "wire individual symbols to features for traceability."
        ),
    )
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--path",   metavar="PATH",   help="File or directory to scan")
    grp.add_argument("--all",    action="store_true",
                     help="Scan discovered code roots (project markers or SysModule.codePaths)")
    grp.add_argument("--module", metavar="MODULE", help="SysModule id — scans its codePaths")
    sp.add_argument("--lang", default="auto",
                    choices=["go", "ts", "py", "java", "cs", "auto"],
                    help="Language filter: go, ts, py, java, cs, or auto (all). Default: auto")
    sp.add_argument("--yes", action="store_true",
                    help="With --all, scan all discovered roots without prompting")

    sp = sub.add_parser("export-neo4j",
                        help="Export SQLite graph to an idempotent Cypher script (SQLite backend only)")
    sp.add_argument("--output", default="sysedge-export.cypher",
                    help="Output file path (default: sysedge-export.cypher)")

    sp = sub.add_parser("add-module-context",
                        help="Store a constraint, concern, decision or outcome note on a module")
    sp.add_argument("--module", required=True, help="Module ID, e.g. M-001")
    sp.add_argument("--text",   required=True, help="Note text")
    sp.add_argument("--kind",   required=True,
                    choices=["constraint", "concern", "decision", "outcome"],
                    help="Note kind: constraint | concern | decision | outcome")
    sp.add_argument("--author", default="session", help="Author name (default: session)")

    sp = sub.add_parser("show-module-context",
                        help="List context notes for a module, newest first, grouped by kind")
    sp.add_argument("--module", required=True, help="Module ID, e.g. M-001")
    sp.add_argument("--kind",
                    choices=["constraint", "concern", "decision", "outcome"],
                    default="", help="Filter by kind (default: show all)")

    sp = sub.add_parser("deliver-brief",
                        help="Assemble a full delivery brief for an ENH or DEF — emits JSON + agent instructions")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--enh", metavar="ENH-xxx", help="Enhancement ID to brief")
    grp.add_argument("--def", dest="defect_id", metavar="DEF-xxx", help="Defect ID to brief")

    p.description += "\n\n" + _grouped_help(sub)
    sub.help = argparse.SUPPRESS  # the grouped list above replaces argparse's flat one
    args = p.parse_args()

    # Zero-Docker backend: SYSEDGE_BACKEND=sqlite or `backend: sqlite` in
    # sysedge.yaml routes the supported commands to sys_edge_sqlite.py.
    backend = (os.environ.get("SYSEDGE_BACKEND")
               or str(_project_config().get("backend", "sqlite"))).strip().lower()
    if backend == "neo4j":
        print(
            "✗ The Neo4j backend requires a commercial licence.\n"
            "  SysEdge Community Edition includes the SQLite backend (set backend: sqlite\n"
            "  in sysedge.yaml, or omit it — SQLite is the default).\n"
            "  For team/enterprise deployments with Neo4j, contact: hudhomer@gmail.com",
            file=sys.stderr,
        )
        sys.exit(1)
    if backend == "sqlite" and args.cmd not in _BACKEND_NEUTRAL:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sys_edge_sqlite
        if args.cmd in sys_edge_sqlite.SUPPORTED:
            sys_edge_sqlite.run(args.cmd, args, _root)
            return
        print(f"✗ {args.cmd} is not supported in the SQLite backend. "
              f"Supported commands: {', '.join(sorted(sys_edge_sqlite.SUPPORTED))}",
              file=sys.stderr)
        sys.exit(1)

    def _dispatch_scan_tests(a):
        # Apply assistant-produced results without needing a scan scope
        if getattr(a, "apply_ai_results", ""):
            _apply_ai_results(a.apply_ai_results)
            return

        if not (a.all or getattr(a, 'module', None) or a.path):
            print("ERROR: scan-tests requires --path, --all, or --module (or --apply-ai-results)",
                  file=sys.stderr)
            sys.exit(2)

        if a.all:
            cmd_scan_all(a)
        elif getattr(a, 'module', None):
            # Look up the module's testPaths and scan matching packages
            drv = _driver()
            with drv.session() as s:
                row = s.run("""
                    MATCH (m:SysModule {id:$id})
                    RETURN coalesce(m.testPaths, []) AS testPaths,
                           coalesce(m.paths, []) AS paths
                """, id=a.module).single()
                if not row:
                    print(f"ERROR: module '{a.module}' not found", file=sys.stderr)
                    drv.close(); sys.exit(1)
                paths = row["testPaths"] or row["paths"] or []
                if not paths:
                    if getattr(a, "yes", False):
                        print(f"ERROR: module '{a.module}' has no testPaths or paths", file=sys.stderr)
                        drv.close(); sys.exit(1)
                    prompted = _prompt_directories(
                        f"Module {a.module} has no registered test paths. "
                        "Enter the test directories to scan:")
                    if not prompted:
                        print(f"ERROR: no test directories provided for {a.module}", file=sys.stderr)
                        drv.close(); sys.exit(1)
                    _persist_module_paths(s, a.module, "testPaths", prompted)
                    paths = prompted
            drv.close()
            # Derive area from module id (MOD-auth → auth, MOD-p8-functions → p8)
            area = a.module.replace("MOD-","").split("-")[0]
            a.area = area
            a.root = ""
            a.instance = ""
            a.yes = True  # do not prompt about multiple discovered test dirs
            cmd_scan_all(a)
        else:
            a.package = a.path
            cmd_scan_package(a)

    def _dispatch_scan_code(a):
        if a.all:
            cmd_scan_code_all(a)
        elif getattr(a, 'module', None):
            # Look up the module's codePaths (falling back to legacy paths) and scan each
            drv = _driver()
            with drv.session() as s:
                row = s.run("""
                    MATCH (m:SysModule {id:$id})
                    RETURN coalesce(m.codePaths, []) AS codePaths,
                           coalesce(m.paths, []) AS paths
                """, id=a.module).single()
                if not row:
                    print(f"ERROR: module '{a.module}' not found", file=sys.stderr)
                    drv.close(); sys.exit(1)
                paths = row["codePaths"] or row["paths"] or []
                if not paths:
                    if getattr(a, "yes", False):
                        print(f"ERROR: module '{a.module}' has no codePaths or paths", file=sys.stderr)
                        drv.close(); sys.exit(1)
                    prompted = _prompt_directories(
                        f"Module {a.module} has no registered code paths. "
                        "Enter the code directories to scan:")
                    if not prompted:
                        print(f"ERROR: no code directories provided for {a.module}", file=sys.stderr)
                        drv.close(); sys.exit(1)
                    _persist_module_paths(s, a.module, "codePaths", prompted)
                    paths = prompted
            drv.close()
            for p in paths:
                a.path = p.rstrip("/")
                if Path(a.path).exists():
                    orig_cmd_scan_code(a)
                else:
                    print(f"  (skip {p} — not found)")
        else:
            orig_cmd_scan_code(a)

    dispatch = {
        "init":           cmd_init,
        "seed":           cmd_seed,
        "briefing":       cmd_briefing,
        "coverage":       cmd_briefing,
        "link-test":      cmd_link_test,
        "test-gaps":      cmd_test_gaps,
        "worklog":        cmd_worklog,
        "link-feature":   cmd_link_feature,
        "link-defect":    cmd_link_defect,
        "create-defect":  cmd_link_defect,
        "close-defect":       cmd_close_defect,
        "claim-defect":       cmd_claim_defect,
        "unclaim-defect":     cmd_unclaim_defect,
        "update-enhancement": cmd_update_enhancement,
        "update-feature":     cmd_update_feature,
        "update-usecase":     cmd_update_usecase,
        "update-story":       cmd_update_story,
        "create-enhancement": cmd_create_enhancement,
        "link-enhancement":   cmd_link_enhancement,
        "unlink-enhancement": cmd_unlink_enhancement,
        "close-enhancement":  cmd_close_enhancement,
        "reconcile-done":     cmd_reconcile_done,
        "start-enhancement":    cmd_start_enhancement,
        "approve-enhancement":  cmd_approve_enhancement,
        "abandon-enhancement":  cmd_abandon_enhancement,
        "reopen-enhancement":   cmd_reopen_enhancement,
        "wontfix-enhancement":  cmd_wontfix_enhancement,
        "defer-enhancement":    cmd_defer_enhancement,
        "show-enhancement":     cmd_show_enhancement,
        "list-defects":         cmd_list_defects,
        "list-enhancements":    cmd_list_enhancements,
        "show-usecase":       cmd_show_usecase,
        "list-usecases":      cmd_list_usecases,
        "show-defect":        cmd_show_defect,
        "show-feature":       cmd_show_feature,
        "show-story":         cmd_show_story,
        "update-defect":      cmd_update_defect,
        "add-note":           cmd_add_note,
        "show-notes":         cmd_show_notes,
        "expire-note":        cmd_expire_note,
        "link-usecase":       cmd_link_usecase,
        "unlink-usecase":     cmd_unlink_usecase,
        "link-story":          cmd_link_story,
        "show-feature-tests":  cmd_show_feature_tests,
        "retire-feature":      cmd_retire_feature,
        "link-blocks":         cmd_link_blocks,
        "unlink-blocks":       cmd_unlink_blocks,
        "migrate-binary":      cmd_migrate_binary,
        "coverage-report":     cmd_coverage_report,
        "coverage-review":     cmd_coverage_review,
        "audit-status":        cmd_audit_status,
        "audit-test":          cmd_audit_test,
        "preview-import":  cmd_preview_import,
        "commit-import":   cmd_commit_import,
        "create-usecase":  cmd_create_usecase,
        "create-feature":  cmd_create_feature,
        "create-adr":      cmd_create_adr,
        "merge-nodes":     cmd_merge_nodes,
        "analyse":         cmd_analyse,
        "export":          cmd_export,
        "create-story":    cmd_create_story,
        "actor-coverage":  cmd_actor_coverage,
        "uc-state-check":  cmd_uc_state_check,
        "enh-align":       cmd_enh_align,
        "link-endpoint":  cmd_link_endpoint,
        "link-symbol":    cmd_link_symbol,
        "refresh-lines":  cmd_refresh_lines,
        "link-chapter":         cmd_link_chapter,
        "link-scenario-story":  cmd_link_scenario_story,
        "link-training":        cmd_link_training,
        "record-run":     cmd_record_run,
        "run-history":    cmd_run_history,
        "prune-runs":          cmd_prune_runs,
        "prune-stale-tests":   cmd_prune_stale_tests,
        "check-test-links":    cmd_check_test_links,
        "reset":               cmd_reset,
        "config":              cmd_config,
        "changes":             cmd_changes,
        "set-external-ref":    cmd_set_external_ref,
        "baseline":            cmd_baseline,
        "trace-matrix":        cmd_trace_matrix,
        "reqif":               cmd_reqif,
        "scan-endpoints": cmd_scan_endpoints,
        "test-status":    cmd_test_status,
        "backup":         cmd_backup,

        "scan-go-tests":      cmd_scan_go_tests,
        "reclassify-tests":   cmd_reclassify_tests,
        "scan-tests":         _dispatch_scan_tests,
        "scan-code":          _dispatch_scan_code,
        "scan-features":      cmd_scan_features,
        "features":       cmd_features,
        "status":         cmd_status,
        "full-inventory": cmd_full_inventory,
        "search":         cmd_search,
        "stories":        cmd_stories,
        "scenarios":      cmd_scenarios,
        "seed-standards": cmd_seed_standards,
        "health-report":      cmd_health_report,
        "session-close":      cmd_session_close,
        "link-commit":        cmd_link_commit,
        "show-blockers":      cmd_show_blockers,
        "impact":             cmd_impact,
        "add-standard":      cmd_add_standard,
        "quality-review":    cmd_quality_review,
        "export-neo4j":      None,  # SQLite-only; load the output with cypher-shell
    }
    # Validate --instance against the graph before any command runs.
    # 'init' and 'seed' are exempt — they may run before SysModule nodes exist.
    _SKIP_INSTANCE_VALIDATION = {"init", "seed"}
    _inst_arg = getattr(args, "instance", "") or ""
    if _inst_arg and args.cmd not in _SKIP_INSTANCE_VALIDATION:
        _vdrv = _driver()
        with _vdrv.session() as _vs:
            _validate_instance(_inst_arg, _vs)
        _vdrv.close()
    for _name in _AUDITED_COMMANDS:
        if _name in dispatch:
            dispatch[_name] = _audited(_name, dispatch[_name])
    try:
        dispatch[args.cmd](args)
    except SystemExit:
        raise
    except Exception as e:
        if "ServiceUnavailable" in type(e).__name__ or "Connection refused" in str(e):
            print(f"\n⚠  Graph database unavailable ({_neo4j_uri()}) — start Docker and retry.\n",
                  file=sys.stderr)
            sys.exit(2)
        if "AuthError" in type(e).__name__ or "Unauthorized" in type(e).__name__:
            print(f"\n⚠  Neo4j authentication failed — check SYSEDGE_NEO4J_USER / SYSEDGE_NEO4J_PASSWORD.\n",
                  file=sys.stderr)
            sys.exit(2)
        import traceback as _tb
        print(f"\n✗  {args.cmd} failed: {e}", file=sys.stderr)
        _tb.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
