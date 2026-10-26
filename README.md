# SysEdge 1.4.0

**V-model traceability, architecture standards, and use-case quality for Claude Code and Kimi Code multi-agent teams.**

When multiple AI agent sessions work on the same codebase simultaneously, they burn tokens re-reading source files, duplicate each other's work, and leave coverage gaps no one notices. SysEdge gives every session a shared, live graph of what exists, what's tested, and what's pending — updated by the sessions themselves as they work.

Everything is open source (MIT). No licence key, no paid tier, no Docker required.

---

## What it looks like

```
$ python3 scripts/sys_edge.py briefing --instance api

  MOD-auth           Auth & Session       12/12 ✓
                       ✓cmp 12/12  ✓int 12/12  ~uc  8/12  ✗e2e 0/12
  MOD-orders         Orders API           9/9   ✓
                       ✓cmp  9/9   ✓int  9/9   ✓uc  9/9   ✗e2e 0/9

  ENHANCEMENTS (2) — PROPOSED
    ENH-14  [Must]   Add rate limiting to /auth/login  → F-AUTH-003
    ENH-15  [Should] Pagination on /orders endpoint    → F-ORD-002

$ python3 scripts/sys_edge.py start-enhancement --id ENH-14 --instance api
✓ ENH-14 in-progress — other sessions can see this is being built
```

---

## Quick start

**Prerequisites:** Python 3.10+

```bash
git clone https://github.com/hudhomer-cyber/SysEdge-Public
cd SysEdge-Public
pip install -r requirements.txt
python3 scripts/sys_edge.py init       # creates data/sysedge.sqlite3
python3 scripts/sys_edge.py status
```

SysEdge creates `data/sysedge.sqlite3` in your project root and is ready immediately.

To use Neo4j instead (optional — for teams already running it):
```bash
export SYSEDGE_NEO4J_URI=bolt://localhost:7687
export SYSEDGE_NEO4J_PASSWORD=yourpassword
# or set backend: neo4j in sysedge.yaml
```

See [INSTALL.md](INSTALL.md) for Neo4j setup and migration details.

---

## Core features

### V-model traceability

The graph connects every level of the V-model:

```
SysUserStory  →  SysUseCase  →  SysFeature  →  SysTest (T1/T2/T3/T4)
```

| Spec level | Test tier | Technology |
|---|---|---|
| User Story | `e2e` | Playwright — cross-tool journey |
| Use Case | `usecase` | Playwright — single UC flow |
| Feature / Module | `integration` | pytest / Jest API tests |
| Symbol / Routine | `component` | Go test, vitest, pytest unit |

Coverage is computed per feature, per module, and across the whole instance. Every session sees the same picture.

### Architecture standards

Load your architecture standards from YAML and SysEdge tracks compliance across the graph:

```bash
python3 scripts/sys_edge.py seed-standards examples/arch-standards.yaml
python3 scripts/sys_edge.py quality-review --instance api
```

Standards define requirements adequacy dimensions — completeness, testability, ambiguity, boundary conditions — and the AI quality review checks each use case against them without making external API calls (see [Session-driven AI](#session-driven-ai) below).

### Use-case quality

SysEdge includes three AI-assisted quality commands, all session-driven:

| Command | What it checks |
|---|---|
| `coverage-review --instance X` | Which features lack stories, UCs, or tests — and why |
| `quality-review --instance X` | Each UC against the loaded architecture standards |
| `uc-state-check --uc UC-X` | Postconditions of one UC align with preconditions of related UCs |
| `enh-align --enh ENH-X` | Enhancement description aligns with the features it touches |
| `audit-test --feature F-X` | Test adequacy — are the right scenarios covered? |

### Session-driven AI

SysEdge does not call any external AI API. Instead, quality commands gather the relevant graph data, emit a structured JSON block plus instructions, and the Claude Code or Kimi Code session acts as the AI. This means:

- No API key required
- No extra cost beyond your normal session
- The AI sees your full graph context, not just a summary
- Results are written back with `--apply-results`

```bash
python3 scripts/sys_edge.py coverage-review --instance api
# → emits JSON + instructions for the session to process
# session analyses and emits recommendations
python3 scripts/sys_edge.py coverage-review --instance api --apply-results results.json
# → writes findings back to the graph
```

### Agent delivery with module memory

SysEdge stores accumulated knowledge about each module in the graph and uses it when briefing a delivery agent:

```bash
# Build up module memory over time
python3 scripts/sys_edge.py add-module-context --module M-001 \
  --kind constraint --text "all DB writes go through the repository layer"
python3 scripts/sys_edge.py add-module-context --module M-001 \
  --kind concern --text "token refresh races under concurrent load — see DEF-045"

# When ready to implement an enhancement
python3 scripts/sys_edge.py deliver-brief --enh ENH-007
```

`deliver-brief` reads the enhancement, its linked features, use cases, stories, open defects, and all module context notes, then emits a structured JSON brief and agent instructions in one shot. The `/deliver` skill forks an agent with this brief so the main session stays clean.

---

## Skills

Install skills into your project once, then invoke them by name in any session:

```bash
# Claude Code
for skill in skills/claude/*; do
  mkdir -p ".claude/skills/$(basename "$skill")"
  cp "$skill/SKILL.md" ".claude/skills/$(basename "$skill")/"
done

# Kimi Code
for skill in skills/kimi/*; do
  mkdir -p ".kimi/skills/$(basename "$skill")"
  cp "$skill/SKILL.md" ".kimi/skills/$(basename "$skill")/"
done
```

Or in a Claude Code session: `/C-sysedge-init` — scans your project, detects structure, writes a seed file ready to load.

| Skill | Invoke | What it does |
|---|---|---|
| `init` | `/C-sysedge-init` | Scan project, generate seed file |
| `feature-graph` | `/C-sysedge-feature-graph` | Build or extend the feature graph for a module |
| `coverage-review` | `/C-sysedge-coverage-review` | Session-driven coverage analysis |
| `quality-review` | `/C-sysedge-quality-review` | Session-driven architecture standards review |
| `audit-test` | `/C-sysedge-audit-test` | Deep test adequacy review for a feature |
| `analyse-graph` | `/C-sysedge-analyse-graph` | Find orphans, merge/split candidates |
| `export` | `/C-sysedge-export` | Export stories, design docs, traceability matrix |
| `import-stories` | `/C-sysedge-import-stories` | Import user stories from Markdown or JSON |
| `import-use-cases` | `/C-sysedge-import-use-cases` | Import use cases from Markdown or JSON |
| `scan-code` | `/C-sysedge-scan-code` | Scan source files and link symbols to features |
| `scan-tests` | `/C-sysedge-scan-tests` | Scan test files and link tests to features |

---

## Key commands

All commands: `python3 scripts/sys_edge.py <command> --help`

| Command | What it does |
|---|---|
| `status` | Cross-instance summary dashboard |
| `briefing --instance X` | Coverage by module, open enhancements, defects |
| `worklog --instance X` | Prioritised work queue for this session |
| `test-gaps --instance X` | Missing test tiers per feature |
| `create-enhancement --title "..." --priority Must` | File new work item |
| `start-enhancement --id ENH-X` | Mark in-progress — visible to all sessions |
| `close-enhancement --id ENH-X` | Mark done with pre-close checklist |
| `deliver-brief --enh ENH-X` | Structured JSON brief for agent-driven delivery |
| `add-module-context --module M-X --kind constraint --text "..."` | Store coding constraint for a module |
| `show-module-context --module M-X` | List accumulated module context notes |
| `coverage-review --instance X` | Session-driven AI coverage review |
| `quality-review --instance X` | Session-driven AI standards review |
| `audit-test --feature F-X` | Test adequacy deep dive |
| `seed-standards <file>` | Load architecture standards from YAML |
| `export --type detailed-design --instance X` | Full design doc with traceability matrix |
| `export-neo4j --output graph.cypher` | Cypher migration script for Neo4j |
| `backup` / `seed <file>` | Export / import the full graph |
| `check-test-links [--fix]` | Verify test paths; fix stale links |

---

## Instance topology

Every project needs three permanent roles plus feature instances per domain:

```
architect   — user story design, ADRs, architecture standards
master      — story maintenance, E2E tests, shared patterns
graph       — SysEdge health, backups, seed operations

orders      — order API, order UI, order tests
auth        — authentication, permissions
billing     — payments, invoicing
deploy      — Docker, CI/CD, runbooks
```

See [INSTANCES.md](INSTANCES.md) for naming conventions, scope templates, and anti-patterns.

---

## Backends

| | SQLite (default) | Neo4j (optional) |
|---|---|---|
| Needs | Nothing — Python stdlib | A running Neo4j server |
| Best for | Single machine, local dev, CI | Teams sharing a central graph |
| Select | `backend: sqlite` in `sysedge.yaml` | `backend: neo4j` + env vars |

Moving between them: `backup` on one backend → `seed <file>` on the other. Or `export-neo4j` to generate an idempotent Cypher script.

---

## Language support

| Language | Graph features | Code scan | Test scan |
|---|---|---|---|
| Python | ✓ Full | ✓ Auto (AST) | ✓ `scan-tests` |
| TypeScript | ✓ Full | ✓ Auto (regex) | ✓ `scan-tests` |
| Go | ✓ Full | ✓ Auto (regex) | `scan-go-tests --path <dir>` |
| Java / C# | ✓ Full | ✓ Auto (regex) | Manual `link-feature` |
| Rust / other | ✓ Full | Manual `link-symbol` | Manual `link-feature` |

---

## Safety model

- An unfiltered `seed` is refused once the graph has data — use `--instance <name>` to scope it.
- `reset` needs an interactive terminal and a typed confirmation, and takes a backup first.
- Every `seed` takes an automatic backup before writing.

---

MIT · Built at [OrgEdge](https://www.org-edge.com) · [sysedge@org-edge.com](mailto:sysedge@org-edge.com)
