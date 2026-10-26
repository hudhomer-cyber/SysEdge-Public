---
name: C-sysedge-feature-graph
description: The SysEdge working loop — read and write the requirements traceability graph (user stories → use cases → features → code symbols, endpoints and tests, plus enhancements, defects and notes). Use at the start of every session in a SysEdge project, when writing tests, adding endpoints or symbols, discovering or fixing a defect, starting or closing an enhancement, and before claiming work complete. Triggers on /C-sysedge-feature-graph, "briefing", "worklog", "log a defect" or "link this test".
---

# /C-sysedge-feature-graph

## Overview

The SysEdge graph (Neo4j, `Sys*` labels) is the authoritative record of features, use cases, user stories, test coverage, code symbols, endpoints, defects and enhancements. It replaces hand-maintained code maps and feature lists.

**Key rule:** the graph tracks what exists. Read it to orient; write it as you work.

**Connection settings** are read from the environment or from `.env` in the project root: `SYSEDGE_NEO4J_URI` (default `bolt://localhost:7688`), `SYSEDGE_NEO4J_USER` and `SYSEDGE_NEO4J_PASSWORD` (required). The legacy `NEO4J_*` names still work but print a deprecation notice; `NEO4J_AUTH` is never read. `SYSEDGE_PROJECT_ROOT` overrides the project root and `SYSEDGE_REPO` the repository id stamped on test paths.


---

## ⛔ HARD RULES — no exceptions

**These rules exist because sessions have caused data loss and graph corruption.**

| Prohibited action | Why |
|---|---|
| `sys_edge.py reset` | Wipes every SysEdge node. It runs only in an interactive terminal, and only the user (the `graph` role) runs it — never an agent session |
| `sys_edge.py seed <file>` without `--instance` | Unfiltered seeds touch every instance. They are allowed only into an empty graph (first run); restores use `--full-restore` and belong to the `graph` role |
| Any `DELETE` / `DETACH DELETE` Cypher | Removes shared data permanently (the CLI's driver refuses it) |
| `docker restart sysedge-neo4j` | Disrupts every running session |
| **Debugging a graph database connection failure** | The `graph` role owns the infrastructure; other sessions wait for the user to resolve it |
| **Fixing a defect without logging it first** | `link-defect` runs before any code change — log first, fix second |
| `git commit` | Only the user commits |
| `git push`, `git reset`, `git stash` | `git stash` silently captures other sessions' uncommitted work and removes it from their files; push and reset are destructive or shared-state operations |
| Working outside your instance scope | A `billing` session does not touch `auth` code, graph nodes or files |
| **Writing new scripts for graph operations** | `sys_edge.py` is the only way to write the graph — no `/tmp/update_graph.py`, `fix_links.py`, `bulk_import.py` or the Neo4j driver |

**If `sys_edge.py` has no command for what you need:** stop, describe the need to the user in one sentence, and wait. The commands are the contract.

**If your graph data looks stale or missing:** restore only your own nodes with an instance-filtered seed (it backs up first):

```bash
ls -t data/sys-backup-*.json | head -1
python3 scripts/sys_edge.py seed data/sys-backup-<date>.json --instance <your-instance>
```

**If you think graph data is missing, it almost certainly is not.** Neo4j's label index can go stale. Check with a query that bypasses it (the password comes from the container's own environment):

```bash
docker exec sysedge-neo4j bash -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" "MATCH (n) WHERE \"SysModule\" IN labels(n) RETURN count(n)"'
```

If it returns non-zero, the data exists: tell the user what you saw and stop.

**If you must delete a single item:** ask the user first; one node at a time, never in bulk.

---

## Instance scope — stay in your lane

Each session owns the modules of exactly one instance. Do not read or write nodes, enhancements or defects of another instance without the user's instruction. Your CLAUDE.md names your instance; list every instance in the project with:

```bash
python3 scripts/sys_edge.py status
```

**Recommended roles** (instances are roles, not people — one person may run several, one active session per instance at a time):

| Role | Typical instance | Owns |
|---|---|---|
| Architecture | `architect` | New user stories, complex use cases, ADRs, architecture standards, feature-map integrity |
| Coordination | `master` | Story maintenance, end-to-end tests, shared patterns, dev scripts |
| Graph operations | `graph` | Graph health, backups, seeds and restores, the graph database |
| Deployment | `deploy` | Docker, CI/CD, runbooks, infrastructure |
| Feature domains | `billing`, `auth`, … | A full vertical slice: backend, frontend and tests |

**Vertical slices:** an instance should own a use case completely — handler, UI component, API tests and UI flow test. Name instances after domains (`billing`, `auth`), not layers (`backend`, `frontend`).

**Use-case responsibilities:** each instance derives and owns the use cases of its modules and writes their UI flow tests. Complex or cross-instance stories and use cases go to the `architect` instance as an enhancement.

---

## Session start

After reading CLAUDE.md and your instance's instructions, run all three:

```bash
python3 scripts/sys_edge.py briefing  --instance <your-instance>   # coverage, defects, enhancements
python3 scripts/sys_edge.py worklog   --instance <your-instance>   # what to work on
python3 scripts/sys_edge.py test-gaps --instance <your-instance>   # features missing component / integration / use-case tests
```

The worklog shows, in order: active notes → open defects → enhancements. The notes are your instance's memory from earlier sessions: read them first.

**To see all open work across every instance in the project** (blockers, cross-instance dependencies, planning):

```bash
python3 scripts/sys_edge.py full-inventory                          # all open defects + ENHs grouped by instance (summary table + detail)
python3 scripts/sys_edge.py full-inventory --system <system-id>     # scoped to one system/repo — use this in a multi-repo graph
```

Use `full-inventory` before planning sessions or when you need to understand where blockers are across the whole project. Use `worklog --instance <name>` for your own session's work queue.

| Exit code | Meaning | Action |
|---|---|---|
| `0` | Success | Continue |
| `2` | Graph database unavailable (unreachable, authentication failed, or password not set) | Stop — see below |

**If a command prints `⚠  Graph database unavailable` (exit code 2):** tell the user "The SysEdge graph database is unavailable — please start it and let me know when it's up", then stop. Do not investigate Docker, the network or the credentials; the `graph` role owns that.

---

## Complete list of allowed graph operations


### Read / orient
```bash
python3 scripts/sys_edge.py health-report                              # one screen: defects, enhancements, coverage, warnings, last backup
python3 scripts/sys_edge.py full-inventory                             # all open work across all instances — blockers and cross-instance view
python3 scripts/sys_edge.py full-inventory --system <system-id>        # limit to one system/repo
python3 scripts/sys_edge.py full-inventory --instance <name>           # summary table for all instances + drill-down detail for one
python3 scripts/sys_edge.py status                                     # every instance: enhancements, defects, coverage
python3 scripts/sys_edge.py briefing   --instance <name> --compact     # hide fully-passing modules
python3 scripts/sys_edge.py worklog    --instance <name> --all         # also ENHs linked outside your modules
python3 scripts/sys_edge.py worklog    --instance <name> --since 2026-05-20
python3 scripts/sys_edge.py test-gaps  --instance <name> --tier component
python3 scripts/sys_edge.py features   --module MOD-billing            # features of a module
python3 scripts/sys_edge.py show-feature  --id F-BIL-001               # tests, use cases, defects, symbols
python3 scripts/sys_edge.py show-feature-tests --feature F-BIL-001     # linked tests + unlinked tests in the module's area
python3 scripts/sys_edge.py show-usecase  --id UC-BIL-001              # flows, tests, story links
python3 scripts/sys_edge.py list-usecases --instance <name>            # or --module MOD-billing, or --story US-012
python3 scripts/sys_edge.py show-story    --id US-012                  # story + linked use cases
python3 scripts/sys_edge.py stories                                    # all user stories
python3 scripts/sys_edge.py stories --gap                              # stories with no use case
python3 scripts/sys_edge.py show-enhancement --id ENH-024
python3 scripts/sys_edge.py show-defect   --id DEF-042
python3 scripts/sys_edge.py search --query "invoice"                   # free text across features, ENHs, defects, UCs, stories
python3 scripts/sys_edge.py coverage-report --instance <name>          # 4-tier V-model gaps: story → UC → feature → test
python3 scripts/sys_edge.py impact --path src/billing/handlers.go      # features and tests that reference a file
python3 scripts/sys_edge.py impact --symbol CreateInvoice              # … or a symbol
python3 scripts/sys_edge.py show-blockers                              # BLOCKED_BY chains across enhancements
python3 scripts/sys_edge.py scenarios                                  # scenarios and training programmes
python3 scripts/sys_edge.py test-status  --instance <name>             # last test runs
python3 scripts/sys_edge.py audit-status --instance <name> --days 30   # audit-test / coverage-review staleness
```

### Update existing nodes
```bash
python3 scripts/sys_edge.py update-feature --id F-BIL-001 --description "Create and send an invoice for a completed order"
python3 scripts/sys_edge.py update-feature --id F-BIL-001 --status Implemented   # Proposed | InProgress | Implemented | Deprecated | Superseded

python3 scripts/sys_edge.py update-usecase --id UC-BIL-001 \
  --preconditions "Clerk has billing:write; the order is complete" \
  --main-flow "1. Open the order 2. Choose Issue invoice 3. System sends it" \
  --authorized-roles "Finance clerk,Admin" \
  --failure-scenarios "Order incomplete; email bounces"
# authorizedRoles and failureScenarios feed AS-REQ-002 / AS-REQ-003 in coverage-review

python3 scripts/sys_edge.py update-story --id US-012 --goal "download any past invoice" \
  --acceptance-criteria "Given a paid invoice, when I click Download, then a PDF opens"

python3 scripts/sys_edge.py update-defect --id DEF-042 --description "Root cause: … Fix: …"
```

### Link tests, stories and commits
```bash
python3 scripts/sys_edge.py link-feature --feature F-BIL-001 \
  --tests "tests/billing/test_invoices.py::TestInvoiceCreate,test_invoices.py::TestInvoiceSend"
python3 scripts/sys_edge.py link-story  --story US-012 --features F-BIL-001,F-BIL-002   # story REQUIRES features
python3 scripts/sys_edge.py link-commit --sha abc1234 --enhancement ENH-024             # or --defect DEF-042
```

`--tests` entries are matched against `SysTest` ids: with `::` the id must end with the pattern (a repo-relative path or the file basename both work); without `::` it is a substring match, e.g. a directory. Never pass an absolute path. A pattern that matches nothing prints `0 matches`.

### Scan and register (additive — safe to re-run)
```bash
python3 scripts/sys_edge.py scan-tests --module MOD-billing        # Python / TypeScript tests of a module
python3 scripts/sys_edge.py scan-tests --path tests/billing/test_invoices.py
python3 scripts/sys_edge.py scan-tests --all --yes
python3 scripts/sys_edge.py scan-go-tests                          # Go *_test.go files (not scan-tests)
python3 scripts/sys_edge.py scan-code  --module MOD-billing        # symbols (CONTAINS_SYMBOL only; link features with link-symbol)
python3 scripts/sys_edge.py scan-code  --path src/billing
python3 scripts/sys_edge.py scan-endpoints                         # Go route registrations vs the graph (missing / stale / permission mismatch)
python3 scripts/sys_edge.py scan-endpoints --apply
python3 scripts/sys_edge.py check-test-links --verbose             # test paths that no longer resolve in this repo
python3 scripts/sys_edge.py check-test-links --fix --yes           # repair or prune them
```

### Test runs and maintenance
```bash
python3 scripts/sys_edge.py record-run --package tests/billing --passed 42 --failed 0 --skipped 2 --duration 18.4
python3 scripts/sys_edge.py record-run --package tests/billing --results-json /tmp/report.json   # per-test results (pytest-json-report)
python3 scripts/sys_edge.py run-history --package tests/billing     # pass-rate trend, flaky tests
python3 scripts/sys_edge.py run-history --instance <name>
python3 scripts/sys_edge.py prune-runs --days 30 --keep 5 --dry-run
python3 scripts/sys_edge.py prune-stale-tests --dry-run             # stale VERIFIES edges to renamed or deleted tests
```

### Endpoints and symbols
```bash
python3 scripts/sys_edge.py link-endpoint --feature F-BIL-001 --method POST --path /api/invoices \
  --binary api --permission billing:write
python3 scripts/sys_edge.py link-symbol --feature F-BIL-001 --file src/billing/handlers.go --symbol CreateInvoice
python3 scripts/sys_edge.py migrate-binary --from api --to worker --features F-BIL-004   # omit --features to move every endpoint/symbol
```

Run `link-endpoint` as soon as an endpoint exists; unlinked endpoints make the briefing look falsely clean.

### Enhancements
```bash
python3 scripts/sys_edge.py create-enhancement --title "Send invoice reminders" --instance billing \
  --priority Should --description "Full prose description" --feature F-BIL-001
python3 scripts/sys_edge.py update-enhancement --id ENH-024 --description "Revised prose" --priority Must
python3 scripts/sys_edge.py update-enhancement --id ENH-024 --instance auth            # reassign to another instance
python3 scripts/sys_edge.py link-enhancement   --id ENH-024 --feature F-BIL-001,F-BIL-002
python3 scripts/sys_edge.py link-blocks        --id ENH-042 --blocked-by ENH-020
python3 scripts/sys_edge.py start-enhancement  --id ENH-024 --instance billing         # claim it before writing code
python3 scripts/sys_edge.py approve-enhancement --id ENH-024                           # proposed → approved (architect)
python3 scripts/sys_edge.py abandon-enhancement --id ENH-024                           # release a claim
python3 scripts/sys_edge.py defer-enhancement   --id ENH-024 --reason "after the payments rework"
python3 scripts/sys_edge.py wontfix-enhancement --id ENH-024 --reason "superseded by ENH-031"
python3 scripts/sys_edge.py close-enhancement   --id ENH-024 --instance billing
python3 scripts/sys_edge.py close-enhancement   --id ENH-024 --instance billing --graph-only   # no code changes
python3 scripts/sys_edge.py close-enhancement   --id ENH-024 --instance billing --test-only    # tests only
```

**Always run `start-enhancement` before starting work** — it stops two sessions building the same thing.

**Before `close-enhancement`, confirm each item applies or explicitly doesn't** (the CLI prints this checklist; `--skip-checklist` only when already verified):
1. `link-endpoint` for every new API endpoint
2. `link-symbol` for every new code symbol
3. Use case → feature edges wired (`link-usecase`)
4. Tests linked to features (`link-feature`)
5. User stories still describe what was built

### Defects

**`link-defect` runs BEFORE any code fix.** If you must investigate first, log a placeholder and rename it later:

```bash
python3 scripts/sys_edge.py link-defect --feature F-BIL-001 --title "TBD — investigating" --severity medium --instance billing
python3 scripts/sys_edge.py link-defect --id DEF-042 --feature F-BIL-001 --title "Invoice total ignores discounts" \
  --severity high --instance billing --description "Repro: … Root cause: …"
python3 scripts/sys_edge.py update-defect  --id DEF-042 --title "Invoice total ignores line discounts"
python3 scripts/sys_edge.py claim-defect   --id DEF-042 --instance billing   # mark in progress
python3 scripts/sys_edge.py unclaim-defect --id DEF-042
python3 scripts/sys_edge.py close-defect   --id DEF-042
```

`--id` is optional (auto-assigned). `--description` holds repro steps, root cause and investigation notes for the next session.

### User stories and use cases
```bash
python3 scripts/sys_edge.py create-story --id US-012 --title "Customer downloads an invoice" \
  --actor "Customer" --user USR-customer --goal "download any past invoice as a PDF" \
  --benefit "file it with my accounts" --priority Must --features F-BIL-002

# Create a use case (--title required), or update an existing one (only given fields change)
python3 scripts/sys_edge.py create-usecase --id UC-BIL-001 --instance billing --title "Finance clerk issues an invoice" \
  --description "…" --priority P1 --story US-012 --feature F-BIL-001,F-BIL-002

# Add links to an existing use case: (uc)-[:REQUIRES]->(feature), (story)-[:REALIZED_BY]->(uc)
python3 scripts/sys_edge.py link-usecase --id UC-BIL-001 --feature F-BIL-003 --story US-013
python3 scripts/sys_edge.py link-usecase --id UC-BIL-001 --tests "tests/billing/test_invoices_ui.py::TestIssueInvoice"
python3 scripts/sys_edge.py unlink-usecase --id UC-BIL-001 --feature F-BIL-099      # or --story US-OLD-001
```

### Features (architect)
```bash
python3 scripts/sys_edge.py create-feature --id F-BIL-005 --name "Invoice audit trail" --module MOD-billing \
  --description "Who issued, changed or voided an invoice, and when" --status Proposed
python3 scripts/sys_edge.py retire-feature --id F-BIL-OLD --reason "merged into F-BIL-005"   # Superseded; excluded from gap reports
```

### Architecture decisions (architect) —
```bash
python3 scripts/sys_edge.py create-adr --id ADR-004 --title "Invoices are immutable once sent" \
  --status adopted --date 2026-04-01 \
  --decision "Corrections are credit notes, never edits" \
  --context "Auditors need a stable record" --consequences "Credit-note flow required" \
  --addresses STD-AUDIT-001                 # optional; --supersedes ADR-002
```

### Quality standards
```bash
python3 scripts/sys_edge.py seed-standards                                   # load the bundled architecture standards
python3 scripts/sys_edge.py quality-review --entity UC-BIL-001               # automated TRC-* checks + the AI commands to run
python3 scripts/sys_edge.py quality-review --instance billing --type SysUseCase
python3 scripts/sys_edge.py quality-review --entity UC-BIL-001 --ai          # AI-evaluated standards
python3 scripts/sys_edge.py add-standard --id AS-TEST-WIZ-002 --title "Wizard UC: back-navigation coverage" \
  --description "…" --applies-to SysUseCase --applies-when "wizardBased=true" --evaluation manual
```

`quality-review` runs the traceability checks (TRC-US/UC/FT-*) as graph queries and prints the exact `audit-test` / `coverage-review` commands for the AI dimensions. Standards added with `add-standard` are project-tier.

### Test and requirement audits —
```bash
python3 scripts/sys_edge.py coverage-review --uc UC-BIL-001                  # is the use case specified well enough? (AS-REQ)
python3 scripts/sys_edge.py coverage-review --us US-012
python3 scripts/sys_edge.py audit-test --uc UC-BIL-001 --preflight           # which test file would be audited
python3 scripts/sys_edge.py audit-test --uc UC-BIL-001                       # AS-TEST-UC
python3 scripts/sys_edge.py audit-test --feature F-BIL-001                   # AS-TEST-COMP (integration tier)
python3 scripts/sys_edge.py audit-test --us US-012                           # AS-TEST-US (e2e tier)
python3 scripts/sys_edge.py audit-test --uc UC-BIL-001 --analyse --output-request /tmp/audit-req.json   # assistant-driven
python3 scripts/sys_edge.py audit-test --apply-results /tmp/audit-res.json
```

Recommended per use case: `coverage-review` → write tests → run them → `audit-test` → `record-run`. Results are stamped on the node (`auditTestLastRun`, `coverageReviewLastRun`, …); check staleness with `audit-status`. The Kit's `/C-sysedge-audit-test` and `/C-sysedge-coverage-review` skills walk through both.

### Graph analysis, export and import —
```bash
python3 scripts/sys_edge.py analyse --orphans --instance <name>             # also --merge, --split; add --json for machine-readable output
python3 scripts/sys_edge.py merge-nodes --type SysFeature --keep F-BIL-001 --absorb F-BIL-004 --strategy union   # deletes the absorbed node
python3 scripts/sys_edge.py export --type stories --instance <name>         # docs/exports/stories-<name>-<date>.md; --format md|json
python3 scripts/sys_edge.py preview-import /tmp/import.json                 # writes /tmp/import-decisions.json
python3 scripts/sys_edge.py commit-import /tmp/import.json --decisions /tmp/import-decisions.json
```

Use `/C-sysedge-analyse-graph`, `/C-sysedge-export`, `/C-sysedge-import-stories` and `/C-sysedge-import-use-cases` for these.

### Session close and backups
```bash
python3 scripts/sys_edge.py session-close --instance <name>      # health report + work summary + backup
python3 scripts/sys_edge.py session-close --no-backup
python3 scripts/sys_edge.py backup                               # data/sys-backup-<date>.json
```

### Notes (instance memory)
```bash
python3 scripts/sys_edge.py add-note --instance billing --body "Kept refunds in the invoice handler for now" --expires 2026-06-15
python3 scripts/sys_edge.py show-notes --instance billing
python3 scripts/sys_edge.py expire-note --id NOTE-001
```

- `SysNote` — in-session context, rationale, reminders for the next session (shown at the top of `worklog`).

### Scenarios and training
```bash
python3 scripts/sys_edge.py link-chapter  --chapter SCN-001-CH-02 --feature F-BIL-001
python3 scripts/sys_edge.py link-training --module TRN-FIN-03 --feature F-BIL-001
```

---

## AI modes and data egress

SysEdge has two ways to run an AI review (`audit-test`, `coverage-review`, `quality-review --ai`, `scan-tests --analyse`):

| Mode | How | What leaves your machine |
|---|---|---|
| **Assistant-driven** (recommended) | `--analyse --output-request <file>` writes a request; you (this Claude session) do the analysis and save the result; `--apply-results <file>` (or `--apply-ai-results` for scan-tests) writes it back | Nothing beyond what this Claude Code session already sends |
| **Direct** | The command calls an AI provider itself: the first available of the `claude`, `kimi`, `gemini`, `qwen` and `codex` CLIs, then the `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` and `DASHSCOPE_API_KEY` APIs | Graph content (use-case text, test file paths and source, standards) is sent to that provider |

- Restrict direct mode to approved providers with `SYSEDGE_AI_PROVIDERS` (comma-separated, e.g. `SYSEDGE_AI_PROVIDERS=claude`) or per command with `--provider <name>`. Provider names: `claude`, `kimi`, `gemini`, `qwen`, `codex`, `anthropic`, `openai`, `dashscope`.
- When the project's data must not leave the session, use the assistant-driven mode only.

---

## Writing tests

SysEdge follows the V-model:

| Tier | `testType` | Owned by | Verifies |
|---|---|---|---|
| End-to-end (story journeys) | `e2e` | `master` | `SysUserStory` |
| UI flow (one use case) | `usecase` | owning instance | `SysUseCase` → `SysFeature` |
| Integration / component API | `integration` | owning instance | `SysFeature` |
| Unit (function / handler) | `component` | owning instance | `SysSymbol` |

Write `usecase`, `integration` and `component` tests for your modules, and link each new test immediately:

```bash
python3 scripts/sys_edge.py link-feature --feature F-BIL-001 --tests "tests/billing/test_invoices.py::TestInvoiceCreate::test_discounts"
```

## After writing code — test discipline

Run the tests in this order after every change. **Do not close an enhancement or defect until this sequence has run** — a passing build is not a passing test suite.

1. **Unit tests** for every package you touched.
2. **Frontend unit tests** (frontend changes).
3. **Integration tests** for the features you changed.
4. **UI flow tests** for every affected use case; for user-visible changes, also a manual smoke test of the main path.
5. **Regression** — the broader suite. Slow tests may be skipped during development, but run the full suite once before closing.
6. **Record the run** — pass or fail:
   ```bash
   python3 scripts/sys_edge.py record-run --package tests/billing --passed N --failed N --skipped N --duration N
   ```
7. **Audit test quality** before closing (): `python3 scripts/sys_edge.py audit-test --uc UC-BIL-001`.

---

## Before claiming work complete

```bash
```

- [ ] Every applicable test tier ran in this session, and `record-run` recorded the final run
- [ ] New endpoints and symbols linked (`link-endpoint`, `link-symbol`); new tests linked (`link-feature`)
- [ ] Defects you fixed were logged before the fix

```bash
python3 scripts/sys_edge.py close-enhancement --id ENH-xxx --instance <name>
python3 scripts/sys_edge.py close-defect --id DEF-xxx
```

Both print a checklist — answer each item before ending the session.

---

## Graph IDs reference

| Node | ID format | Example |
|---|---|---|
| Module | `MOD-<area>` | `MOD-billing` |
| Feature | `F-<SHORT>-NNN` | `F-BIL-001` |
| Use case | `UC-<SHORT>-NNN` | `UC-BIL-001` |
| User story | `US-NNN` or `US-<SHORT>-NNN` | `US-012` |
| Enhancement / defect / note | auto-assigned | `ENH-024`, `DEF-042`, `NOTE-001` |

Look up module ids with `python3 scripts/sys_edge.py status` and `python3 scripts/sys_edge.py briefing --instance <name>`; feature ids with `python3 scripts/sys_edge.py features --module <MOD-id>`.

---

## Visualiser —

```bash
```
