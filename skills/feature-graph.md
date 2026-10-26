---
name: {{NAME}}
description: The SysEdge working loop — read and write the requirements traceability graph (user stories → use cases → features → code symbols, endpoints and tests, plus enhancements, defects and notes). Use at the start of every session in a SysEdge project, when writing tests, adding endpoints or symbols, discovering or fixing a defect, starting or closing an enhancement, and before claiming work complete. Triggers on /{{SELF}}, "briefing", "worklog", "log a defect" or "link this test".
---

# /{{SELF}}

## Overview

The SysEdge graph (Neo4j, `Sys*` labels) is the authoritative record of features, use cases, user stories, test coverage, code symbols, endpoints, defects and enhancements. It replaces hand-maintained code maps and feature lists.

**Key rule:** the graph tracks what exists. Read it to orient; write it as you work.

{{>connection}}

---

## ⛔ HARD RULES — no exceptions

**These rules exist because sessions have caused data loss and graph corruption.**

| Prohibited action | Why |
|---|---|
| `sys_edge.py reset` | Wipes every SysEdge node. It runs only in an interactive terminal, and only the user (the `{{GRAPH_ROLE}}` role) runs it — never an agent session |
| `sys_edge.py seed <file>` without `--instance` | Unfiltered seeds touch every instance. They are allowed only into an empty graph (first run); restores use `--full-restore` and belong to the `{{GRAPH_ROLE}}` role |
{{LOCAL:hard-rules}}
| Any `DELETE` / `DETACH DELETE` Cypher | Removes shared data permanently (the CLI's driver refuses it) |
| `docker restart sysedge-neo4j` | Disrupts every running session |
| **Debugging a graph database connection failure** | The `{{GRAPH_ROLE}}` role owns the infrastructure; other sessions wait for the user to resolve it |
| **Fixing a defect without logging it first** | `link-defect` runs before any code change — log first, fix second |
| `git commit` | Only the user commits |
| `git push`, `git reset`, `git stash` | `git stash` silently captures other sessions' uncommitted work and removes it from their files; push and reset are destructive or shared-state operations |
| Working outside your instance scope | A `billing` session does not touch `auth` code, graph nodes or files |
| **Writing new scripts for graph operations** | `sys_edge.py` is the only way to write the graph — no `/tmp/update_graph.py`, `fix_links.py`, `bulk_import.py` or the Neo4j driver |

**If `sys_edge.py` has no command for what you need:** stop, describe the need to the user in one sentence, and wait. The commands are the contract.

**If your graph data looks stale or missing:** restore only your own nodes with an instance-filtered seed (it backs up first):

```bash
ls -t data/sys-backup-*.json | head -1
{{CLI}} seed data/sys-backup-<date>.json --instance <your-instance>
```

**If you think graph data is missing, it almost certainly is not.** Neo4j's label index can go stale. Check with a query that bypasses it (the password comes from the container's own environment):

```bash
docker exec sysedge-neo4j bash -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" "MATCH (n) WHERE \"SysModule\" IN labels(n) RETURN count(n)"'
```

If it returns non-zero, the data exists: tell the user what you saw and stop.

**If you must delete a single item:** ask the user first; one node at a time, never in bulk.

---

## Instance scope — stay in your lane

Each session owns the modules of exactly one instance. Do not read or write nodes, enhancements or defects of another instance without the user's instruction. Your {{AGENT_FILE}} names your instance; list every instance in the project with:

```bash
{{CLI}} status
```

**Recommended roles** (instances are roles, not people — one person may run several, one active session per instance at a time):

| Role | Typical instance | Owns |
|---|---|---|
| Architecture | `architect` | New user stories, complex use cases, ADRs, architecture standards, feature-map integrity |
| Coordination | `master` | Story maintenance, end-to-end tests, shared patterns, dev scripts |
| Graph operations | `{{GRAPH_ROLE}}` | Graph health, backups, seeds and restores, the graph database |
| Deployment | `deploy` | Docker, CI/CD, runbooks, infrastructure |
| Feature domains | `billing`, `auth`, … | A full vertical slice: backend, frontend and tests |

**Vertical slices:** an instance should own a use case completely — handler, UI component, API tests and UI flow test. Name instances after domains (`billing`, `auth`), not layers (`backend`, `frontend`).

{{LOCAL:instances}}
**Use-case responsibilities:** each instance derives and owns the use cases of its modules and writes their UI flow tests. Complex or cross-instance stories and use cases go to the `architect` instance as an enhancement.

---

## Session start

After reading {{AGENT_FILE}} and your instance's instructions, run all three:

```bash
{{CLI}} briefing  --instance <your-instance>   # coverage, defects, enhancements
{{CLI}} worklog   --instance <your-instance>   # what to work on
{{CLI}} test-gaps --instance <your-instance>   # features missing component / integration / use-case tests
```

The worklog shows, in order: active notes → open defects → enhancements. The notes are your instance's memory from earlier sessions: read them first.

**To see all open work across every instance in the project** (blockers, cross-instance dependencies, planning):

```bash
{{CLI}} full-inventory                          # all open defects + ENHs grouped by instance (summary table + detail)
{{CLI}} full-inventory --system <system-id>     # scoped to one system/repo — use this in a multi-repo graph
```

Use `full-inventory` before planning sessions or when you need to understand where blockers are across the whole project. Use `worklog --instance <name>` for your own session's work queue.

{{LOCAL:project-worklog}}

| Exit code | Meaning | Action |
|---|---|---|
| `0` | Success | Continue |
| `1` | Command error (`sys_edge.py`), or warnings found (`sys_edge_health.py`) | Read the message. Health warnings are normal: continue and prioritise them |
| `2` | Graph database unavailable (unreachable, authentication failed, or password not set) | Stop — see below |

**If a command prints `⚠  Graph database unavailable` (exit code 2):** tell the user "The SysEdge graph database is unavailable — please start it and let me know when it's up", then stop. Do not investigate Docker, the network or the credentials; the `{{GRAPH_ROLE}}` role owns that.

---

## Complete list of allowed graph operations


### Read / orient
```bash
{{CLI}} health-report                              # one screen: defects, enhancements, coverage, warnings, last backup
{{CLI}} full-inventory                             # all open work across all instances — blockers and cross-instance view
{{CLI}} full-inventory --system <system-id>        # limit to one system/repo
{{CLI}} full-inventory --instance <name>           # summary table for all instances + drill-down detail for one
{{CLI}} status                                     # every instance: enhancements, defects, coverage
{{CLI}} briefing   --instance <name> --compact     # hide fully-passing modules
{{CLI}} worklog    --instance <name> --all         # also ENHs linked outside your modules
{{CLI}} worklog    --instance <name> --since 2026-05-20
{{CLI}} test-gaps  --instance <name> --tier component
{{CLI}} features   --module MOD-billing            # features of a module
{{CLI}} show-feature  --id F-BIL-001               # tests, use cases, defects, symbols
{{CLI}} show-feature-tests --feature F-BIL-001     # linked tests + unlinked tests in the module's area
{{CLI}} show-usecase  --id UC-BIL-001              # flows, tests, story links
{{CLI}} list-usecases --instance <name>            # or --module MOD-billing, or --story US-012
{{CLI}} show-story    --id US-012                  # story + linked use cases
{{CLI}} stories                                    # all user stories
{{CLI}} stories --gap                              # stories with no use case
{{CLI}} show-enhancement --id ENH-024
{{CLI}} show-defect   --id DEF-042
{{CLI}} search --query "invoice"                   # free text across features, ENHs, defects, UCs, stories
{{CLI}} coverage-report --instance <name>          # 4-tier V-model gaps: story → UC → feature → test
{{CLI}} impact --path src/billing/handlers.go      # features and tests that reference a file
{{CLI}} impact --symbol CreateInvoice              # … or a symbol
{{CLI}} show-blockers                              # BLOCKED_BY chains across enhancements
{{CLI}} scenarios                                  # scenarios and training programmes
{{CLI}} test-status  --instance <name>             # last test runs
{{CLI}} audit-status --instance <name> --days 30   # audit-test / coverage-review staleness
python3 {{CLI_DIR}}/sys_edge_health.py --instance <name>   # structural health check (exit 1 = warnings)
```

### Update existing nodes
```bash
{{CLI}} update-feature --id F-BIL-001 --description "Create and send an invoice for a completed order"
{{CLI}} update-feature --id F-BIL-001 --status Implemented   # Proposed | InProgress | Implemented | Deprecated | Superseded

{{CLI}} update-usecase --id UC-BIL-001 \
  --preconditions "Clerk has billing:write; the order is complete" \
  --main-flow "1. Open the order 2. Choose Issue invoice 3. System sends it" \
  --authorized-roles "Finance clerk,Admin" \
  --failure-scenarios "Order incomplete; email bounces"
# authorizedRoles and failureScenarios feed AS-REQ-002 / AS-REQ-003 in coverage-review

{{CLI}} update-story --id US-012 --goal "download any past invoice" \
  --acceptance-criteria "Given a paid invoice, when I click Download, then a PDF opens"

{{CLI}} update-defect --id DEF-042 --description "Root cause: … Fix: …"
```

### Link tests, stories and commits
```bash
{{CLI}} link-feature --feature F-BIL-001 \
  --tests "tests/billing/test_invoices.py::TestInvoiceCreate,test_invoices.py::TestInvoiceSend"
{{CLI}} link-story  --story US-012 --features F-BIL-001,F-BIL-002   # story REQUIRES features
{{CLI}} link-commit --sha abc1234 --enhancement ENH-024             # or --defect DEF-042
```

`--tests` entries are matched against `SysTest` ids: with `::` the id must end with the pattern (a repo-relative path or the file basename both work); without `::` it is a substring match, e.g. a directory. Never pass an absolute path. A pattern that matches nothing prints `0 matches`.

### Scan and register (additive — safe to re-run)
```bash
{{CLI}} scan-tests --module MOD-billing        # Python / TypeScript tests of a module
{{CLI}} scan-tests --path tests/billing/test_invoices.py
{{CLI}} scan-tests --all --yes
{{CLI}} scan-go-tests                          # Go *_test.go files (not scan-tests)
{{CLI}} scan-code  --module MOD-billing        # symbols (CONTAINS_SYMBOL only; link features with link-symbol)
{{CLI}} scan-code  --path src/billing
{{CLI}} scan-endpoints                         # Go route registrations vs the graph (missing / stale / permission mismatch)
{{CLI}} scan-endpoints --apply
{{CLI}} check-test-links --verbose             # test paths that no longer resolve in this repo
{{CLI}} check-test-links --fix --yes           # repair or prune them
```

### Test runs and maintenance
```bash
{{CLI}} record-run --package tests/billing --passed 42 --failed 0 --skipped 2 --duration 18.4
{{CLI}} record-run --package tests/billing --results-json /tmp/report.json   # per-test results (pytest-json-report)
{{CLI}} run-history --package tests/billing     # pass-rate trend, flaky tests
{{CLI}} run-history --instance <name>
{{CLI}} prune-runs --days 30 --keep 5 --dry-run
{{CLI}} prune-stale-tests --dry-run             # stale VERIFIES edges to renamed or deleted tests
```

### Endpoints and symbols
```bash
{{CLI}} link-endpoint --feature F-BIL-001 --method POST --path /api/invoices \
  --binary api --permission billing:write
{{CLI}} link-symbol --feature F-BIL-001 --file src/billing/handlers.go --symbol CreateInvoice
{{CLI}} migrate-binary --from api --to worker --features F-BIL-004   # omit --features to move every endpoint/symbol
```

Run `link-endpoint` as soon as an endpoint exists; unlinked endpoints make the briefing look falsely clean.

### Enhancements
```bash
{{CLI}} create-enhancement --title "Send invoice reminders" --instance billing \
  --priority Should --description "Full prose description" --feature F-BIL-001
{{CLI}} update-enhancement --id ENH-024 --description "Revised prose" --priority Must
{{CLI}} update-enhancement --id ENH-024 --instance auth            # reassign to another instance
{{CLI}} link-enhancement   --id ENH-024 --feature F-BIL-001,F-BIL-002
{{CLI}} link-blocks        --id ENH-042 --blocked-by ENH-020
{{CLI}} start-enhancement  --id ENH-024 --instance billing         # claim it before writing code
{{CLI}} approve-enhancement --id ENH-024                           # proposed → approved (architect)
{{CLI}} abandon-enhancement --id ENH-024                           # release a claim
{{CLI}} defer-enhancement   --id ENH-024 --reason "after the payments rework"
{{CLI}} wontfix-enhancement --id ENH-024 --reason "superseded by ENH-031"
{{CLI}} close-enhancement   --id ENH-024 --instance billing
{{CLI}} close-enhancement   --id ENH-024 --instance billing --graph-only   # no code changes
{{CLI}} close-enhancement   --id ENH-024 --instance billing --test-only    # tests only
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
{{CLI}} link-defect --feature F-BIL-001 --title "TBD — investigating" --severity medium --instance billing
{{CLI}} link-defect --id DEF-042 --feature F-BIL-001 --title "Invoice total ignores discounts" \
  --severity high --instance billing --description "Repro: … Root cause: …"
{{CLI}} update-defect  --id DEF-042 --title "Invoice total ignores line discounts"
{{CLI}} claim-defect   --id DEF-042 --instance billing   # mark in progress
{{CLI}} unclaim-defect --id DEF-042
{{CLI}} close-defect   --id DEF-042
```

`--id` is optional (auto-assigned). `--description` holds repro steps, root cause and investigation notes for the next session.

### User stories and use cases
```bash
{{CLI}} create-story --id US-012 --title "Customer downloads an invoice" \
  --actor "Customer" --user USR-customer --goal "download any past invoice as a PDF" \
  --benefit "file it with my accounts" --priority Must --features F-BIL-002

# Create a use case (--title required), or update an existing one (only given fields change)
{{CLI}} create-usecase --id UC-BIL-001 --instance billing --title "Finance clerk issues an invoice" \
  --description "…" --priority P1 --story US-012 --feature F-BIL-001,F-BIL-002

# Add links to an existing use case: (uc)-[:REQUIRES]->(feature), (story)-[:REALIZED_BY]->(uc)
{{CLI}} link-usecase --id UC-BIL-001 --feature F-BIL-003 --story US-013
{{CLI}} link-usecase --id UC-BIL-001 --tests "tests/billing/test_invoices_ui.py::TestIssueInvoice"
{{CLI}} unlink-usecase --id UC-BIL-001 --feature F-BIL-099      # or --story US-OLD-001
```

### Features (architect)
```bash
{{CLI}} create-feature --id F-BIL-005 --name "Invoice audit trail" --module MOD-billing \
  --description "Who issued, changed or voided an invoice, and when" --status Proposed
{{CLI}} retire-feature --id F-BIL-OLD --reason "merged into F-BIL-005"   # Superseded; excluded from gap reports
```

### Architecture decisions (architect) —
```bash
{{CLI}} create-adr --id ADR-004 --title "Invoices are immutable once sent" \
  --status adopted --date 2026-04-01 \
  --decision "Corrections are credit notes, never edits" \
  --context "Auditors need a stable record" --consequences "Credit-note flow required" \
  --addresses STD-AUDIT-001                 # optional; --supersedes ADR-002
```

### Quality standards
```bash
{{CLI}} seed-standards                                   # load the bundled architecture standards
{{CLI}} quality-review --entity UC-BIL-001               # automated TRC-* checks + the AI commands to run
{{CLI}} quality-review --instance billing --type SysUseCase
{{CLI}} quality-review --entity UC-BIL-001 --ai          # AI-evaluated standards
{{CLI}} add-standard --id AS-TEST-WIZ-002 --title "Wizard UC: back-navigation coverage" \
  --description "…" --applies-to SysUseCase --applies-when "wizardBased=true" --evaluation manual
```

`quality-review` runs the traceability checks (TRC-US/UC/FT-*) as graph queries and prints the exact `audit-test` / `coverage-review` commands for the AI dimensions. Standards added with `add-standard` are project-tier.

### Test and requirement audits —
```bash
{{CLI}} coverage-review --uc UC-BIL-001                  # is the use case specified well enough? (AS-REQ)
{{CLI}} coverage-review --us US-012
{{CLI}} audit-test --uc UC-BIL-001 --preflight           # which test file would be audited
{{CLI}} audit-test --uc UC-BIL-001                       # AS-TEST-UC
{{CLI}} audit-test --feature F-BIL-001                   # AS-TEST-COMP (integration tier)
{{CLI}} audit-test --us US-012                           # AS-TEST-US (e2e tier)
{{CLI}} audit-test --uc UC-BIL-001 --analyse --output-request /tmp/audit-req.json   # assistant-driven
{{CLI}} audit-test --apply-results /tmp/audit-res.json
```

Recommended per use case: `coverage-review` → write tests → run them → `audit-test` → `record-run`. Results are stamped on the node (`auditTestLastRun`, `coverageReviewLastRun`, …); check staleness with `audit-status`.{{#product}} The Kit's `/{{SKILL:audit-test}}` and `/{{SKILL:coverage-review}}` skills walk through both.{{/product}}

### Graph analysis, export and import —
```bash
{{CLI}} analyse --orphans --instance <name>             # also --merge, --split; add --json for machine-readable output
{{CLI}} merge-nodes --type SysFeature --keep F-BIL-001 --absorb F-BIL-004 --strategy union   # deletes the absorbed node
{{CLI}} export --type stories --instance <name>         # docs/exports/stories-<name>-<date>.md; --format md|json
{{CLI}} preview-import /tmp/import.json                 # writes /tmp/import-decisions.json
{{CLI}} commit-import /tmp/import.json --decisions /tmp/import-decisions.json
```

Use `/{{SKILL:analyse-graph}}`, `/{{SKILL:export}}`, `/{{SKILL:import-stories}}` and `/{{SKILL:import-use-cases}}` for these.

### Session close and backups
```bash
{{CLI}} session-close --instance <name>      # health report + work summary + backup
{{CLI}} session-close --no-backup
{{CLI}} backup                               # data/sys-backup-<date>.json
```

### Notes (instance memory)
```bash
{{CLI}} add-note --instance billing --body "Kept refunds in the invoice handler for now" --expires 2026-06-15
{{CLI}} show-notes --instance billing
{{CLI}} expire-note --id NOTE-001
```

- `SysNote` — in-session context, rationale, reminders for the next session (shown at the top of `worklog`).

### Scenarios and training
```bash
{{CLI}} link-chapter  --chapter SCN-001-CH-02 --feature F-BIL-001
{{CLI}} link-training --module TRN-FIN-03 --feature F-BIL-001
```

---

{{>ai-egress}}

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
{{CLI}} link-feature --feature F-BIL-001 --tests "tests/billing/test_invoices.py::TestInvoiceCreate::test_discounts"
```

{{LOCAL:testing}}
{{#product}}
## After writing code — test discipline

Run the tests in this order after every change. **Do not close an enhancement or defect until this sequence has run** — a passing build is not a passing test suite.

1. **Unit tests** for every package you touched.
2. **Frontend unit tests** (frontend changes).
3. **Integration tests** for the features you changed.
4. **UI flow tests** for every affected use case; for user-visible changes, also a manual smoke test of the main path.
5. **Regression** — the broader suite. Slow tests may be skipped during development, but run the full suite once before closing.
6. **Record the run** — pass or fail:
   ```bash
   {{CLI}} record-run --package tests/billing --passed N --failed N --skipped N --duration N
   ```
7. **Audit test quality** before closing ([Kit]): `{{CLI}} audit-test --uc UC-BIL-001`.
{{/product}}

---

## Before claiming work complete

```bash
python3 {{CLI_DIR}}/sys_edge_health.py --instance <your-instance>   # coverage gaps, stale links, missing edges
```

- [ ] Every applicable test tier ran in this session, and `record-run` recorded the final run
- [ ] New endpoints and symbols linked (`link-endpoint`, `link-symbol`); new tests linked (`link-feature`)
- [ ] Defects you fixed were logged before the fix

```bash
{{CLI}} close-enhancement --id ENH-xxx --instance <name>
{{CLI}} close-defect --id DEF-xxx
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

Look up module ids with `{{CLI}} status` and `{{CLI}} briefing --instance <name>`; feature ids with `{{CLI}} features --module <MOD-id>`.

{{LOCAL:ids}}
---

## Visualiser{{#product}} —{{/product}}

```bash
python3 {{CLI_DIR}}/sys_edge_viz.py   # http://localhost:8888
```
