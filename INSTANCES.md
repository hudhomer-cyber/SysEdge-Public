# SysEdge — Recommended Agent Instance Topology

A SysEdge "instance" is a parallel AI-agent session with a defined scope: which code it owns, which graph nodes it writes, which enhancements it implements. SysEdge coordinates across instances via the shared graph — every instance reads the same worklog, briefing, and test-gap reports.

This document describes the standard roles, their responsibilities, and how to name and scope feature instances for your project.

---

## The three permanent roles

Every SysEdge-enabled project needs these three, regardless of size. They do not own application code — they own the system that builds the application.

> **Role names.** This guide calls the graph-owning role `graph`. Some projects (including the repository SysEdge is developed in) call it `sys-edge`; the name is just the `--instance` value, so pick one and use it consistently.

### `architect`
> *What should be built and why.*

- Authors new User Stories (complex or cross-instance ones)
- Designs Use Cases that span multiple modules
- Records Architecture Decision Records (ADRs)
- Maintains feature-map integrity — ensures new code has graph nodes and traceability edges
- Reviews architecture standard compliance (health check gaps)
- Does **not** write application code

Session start:
```bash
python3 cli/sys_edge.py worklog --instance architect
python3 cli/sys_edge_health.py
python3 cli/sys_edge.py stories
```

### `master`
> *What's working and what's been shipped.*

- Maintains existing User Stories (narratives, acceptance criteria, REALIZED_BY links)
- Owns cross-instance integration and E2E tests (the top tier of the V-model)
- Maintains shared patterns (`shared/patterns/*.md`)
- Keeps startup scripts and dev tooling in sync as the team grows
- Coordinates cross-instance work — never implements it directly
- Does **not** write feature code for specific modules

Session start:
```bash
python3 cli/sys_edge.py worklog --instance master
python3 cli/sys_edge.py test-gaps --instance master
```

### `graph`
> *The health and integrity of the graph itself.*

- Runs scheduled backups (`backup`) and health checks (`cli/sys_edge_health.py`)
- Runs `seed-standards` when the standards YAML changes *(Kit)*
- Re-scans code and tests after large refactors (`scan-code --all`, `scan-tests --all`, `check-test-links`)
- Investigates and resolves graph anomalies (stale paths, orphan nodes)
- **Only role that may ask for `reset` or `seed <backup> --full-restore`** — both wipe every instance's nodes, and the user runs them in their own terminal (agents are denied them by the Kit's deny rules)

Session start:
```bash
python3 cli/sys_edge_health.py
python3 cli/sys_edge.py worklog --instance graph
```

Note: the `graph` instance usually has no feature modules of its own; its enhancements and defects are filed with `--instance graph`.

---

## Feature instances

Feature instances own application code. One instance per major domain — **not** one per file or one per technology layer.

### Sizing guide

| Codebase size | Recommended instances | Total with core roles |
|---|---|---|
| Solo / small team (1–3 devs) | 2–3 feature instances | 5–6 total |
| Medium team (4–8 devs) | 4–6 feature instances | 7–9 total |
| Large codebase (8+ devs) | 6–10 feature instances | 9–13 total |

Beyond ~12 instances the coordination overhead from the graph compounds. At that point, split the codebase into separate repositories each with their own graph.

### Vertical slice ownership — the core principle

**An instance should own a Use Case completely.** A UC requires specific features, which require specific code, which is verified by specific tests. When one instance owns all of that — backend handler, frontend component, API test, and UI flow test — the traceability chain is clean and the instance can build and verify the UC independently.

This means the natural instance boundary is a **vertical slice** (one domain, full stack), not a **horizontal layer** (all backends, or all frontends):

```
  ✓ Vertical slice — delegation instance owns everything for delegation:
      backend/internal/delegation/handlers.go   → F-DLG-001
      frontend/src/features/delegation/          → F-DLG-UI-001
      tests/test_delegation_api.py              → integration test
      tests/test_delegation_ui.py               → UC flow test (Playwright)
      UC-MGT-004: Manager creates delegation    → owned by delegation instance

  ✗ Horizontal split — UC spans two instances, nobody owns it cleanly:
      api instance:  F-DLG-001 (backend)
      ui  instance:  F-DLG-UI-001 (frontend)
      UC-MGT-004 requires both — which instance writes the Playwright test?
```

**When splitting by layer IS appropriate:**

- **API-only / headless services** — no frontend exists, so the UC is entirely backend. A `data-pipeline` or `worker` instance is naturally a backend slice.
- **Multiple frontends** (mobile + web + CLI) — the backend domain instance owns the canonical UC and API features. Each frontend *may* get its own instance if it's large enough, but the backend UC is the authoritative definition; the frontend instance writes UI-specific tests against that UC.
- **Shared infrastructure** — a `platform` or `infra` instance can own cross-cutting concerns (auth middleware, observability, shared components) that aren't tied to a domain UC.

### Naming convention

Name instances after **domains**, not technologies or layers:

| Good | Avoid | Why |
|---|---|---|
| `delegation` | `backend` | Domain, not layer |
| `dashboard` | `frontend` | Domain, not layer |
| `auth` | `security` | Specific capability |
| `billing` | `stripe` | Domain, not vendor |
| `notifications` | `email` | Domain, not channel |
| `orders` | `database` | Domain, not storage tech |

Keep names to one lowercase word. Two words joined with a hyphen is fine (`manage-reports`, `partner-contracts`) but avoid underscores and camelCase.

### What a feature instance owns

Each feature instance should have:

1. **A set of `SysModule` nodes** — the code directories it owns
2. **`instances/<name>/CLAUDE.md`** (Claude Code) **or `instances/<name>/KIMI.md`** (Kimi Code CLI) — scope definition, session protocol, code paths

All enhancements and defects are tracked in the graph — use `sys_edge.py worklog --instance <name>` to see them. Do not create ENHANCE.md or DEFECTS.md files.

The scope table in that file is the contract. It is a **convention** the agent follows — the CLI does not enforce instance scope on writes (only `seed --instance` filters, and `close-enhancement --instance` warns):

```markdown
## Scope

| Area | Paths | Access |
|---|---|---|
| API handlers | `backend/internal/api/` | Read/Write |
| API tests | `backend/tests/api/` | Read/Write |
| Frontend API client | `src/features/api/` | Read/Write |
| Other modules | everywhere else | Read-only |
```

### Use Cases

Each feature instance **derives and owns its own Use Cases** for the modules it owns. Simple, single-module UCs are written by the instance. Cross-instance or architecturally significant UCs are designed by `architect` and handed to the owning instance.

Each feature instance also **writes the UI flow tests** for its UCs — see `patterns/uc-ui-testing.md`.

---

## The `deploy` / `ops` instance (optional)

If your project has non-trivial infrastructure — container builds, CI/CD pipelines, Caddy/nginx config, cloud provisioning — add a `deploy` instance:

- Owns Dockerfiles, docker-compose files, CI YAML, Caddyfile
- Maintains runbooks (`docs/runbooks/`)
- Manages deployment scripts and environment configuration
- Implements operations-standard gap enhancements (e.g. zero-downtime deployment)

For simpler projects, `master` can absorb deploy responsibilities.

---

## Templates

Use the file extension that matches your agent: `instances/<name>/CLAUDE.md` for Claude Code, `instances/<name>/KIMI.md` for Kimi Code CLI.

### `instances/<name>/CLAUDE.md`

```markdown
# Claude Instance: <Name>

> Owns: <one-line description of what this instance is responsible for>

## Scope

| Area | Paths | Access |
|------|-------|--------|
| <module name> | `<path/to/code/>` | Read/Write |
| All other code | everywhere else | Read-only |

## Responsibilities

- <bullet list of 3–5 clear responsibilities>

## Session Start

Invoke `/C-sysedge-feature-graph` for the command reference, then run:

    python3 cli/sys_edge.py briefing  --instance <name>
    python3 cli/sys_edge.py worklog   --instance <name>
    python3 cli/sys_edge.py test-gaps --instance <name>

## Session End

1. Run `close-enhancement` for completed work
2. Run `link-endpoint` + `link-symbol` for any new API routes or code symbols added this session
```

### `instances/<name>/KIMI.md`

```markdown
# Kimi Instance: <Name>

> Owns: <one-line description of what this instance is responsible for>

## Scope

| Area | Paths | Access |
|------|-------|--------|
| <module name> | `<path/to/code/>` | Read/Write |
| All other code | everywhere else | Read-only |

## Responsibilities

- <bullet list of 3–5 clear responsibilities>

## Session Start

Invoke `/K-sysedge-feature-graph` for the command reference, then run:

    python3 cli/sys_edge.py briefing  --instance <name>
    python3 cli/sys_edge.py worklog   --instance <name>
    python3 cli/sys_edge.py test-gaps --instance <name>

## Session End

1. Run `close-enhancement` for completed work
2. Run `link-endpoint` + `link-symbol` for any new API routes or code symbols added this session
```

---

## Example topology: SaaS product with 6 feature instances

Vertical slices — each feature instance owns its domain's backend, frontend and tests:

```
architect      — US design, ADRs, traceability
master         — US maintenance, E2E tests, shared patterns
graph          — SysEdge health, backup, tooling

orders         — order API, order UI, order tests
  MOD-orders, MOD-orders-ui

catalog        — products, inventory, search
  MOD-inventory, MOD-catalog-ui

customers      — accounts, profiles, admin screens
  MOD-customers, MOD-admin-ui

auth           — authentication, authorisation, sessions
  MOD-auth, MOD-permissions

billing        — payments, invoicing, subscriptions
  MOD-billing, MOD-invoices

deploy         — Docker, CI/CD, Caddy, runbooks
  MOD-infra, MOD-deploy
```

Total: 9 instances. Each has a `CLAUDE.md` or `KIMI.md`. The graph coordinates without the instances needing to talk to each other directly.

---

## Anti-patterns to avoid

| Anti-pattern | Problem |
|---|---|
| One instance per file | Too many instances; coordination overhead exceeds benefit |
| `backend` and `frontend` as the only feature instances | Too coarse; instances become giant and unfocused |
| Instance named after a person | Instances represent roles, not people |
| Feature instances that own `shared/` code | Shared code is read-only for feature instances; changes go via master or architect |
| Cross-instance code edits ("while I'm here") | Breaks scope contracts; the owning instance doesn't know a change was made |
| Filing one enhancement that spans several instances | Create one enhancement per owning instance (`create-enhancement --instance <owner>`) and mention the related IDs in each description — the graph, not markdown files, holds the links |
