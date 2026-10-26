---
name: C-sysedge-scan-code
description: Register source-code symbols (functions, handlers, components) in the SysEdge graph. Use after seeding a new project, after a large refactor, or before a coverage or quality review, when the graph's SysSymbol nodes are missing or out of date. Triggers on /C-sysedge-scan-code or "scan the code into SysEdge".
---

# /C-sysedge-scan-code

Scan the project source and register `SysSymbol` nodes under their owning `SysModule` (`CONTAINS_SYMBOL` edges). The scan is additive and safe to re-run.

`scan-code` itself is a free CLI command

## When to use

- After seeding a new project (`/C-sysedge-init`).
- After significant refactoring or renaming.
- Before `/C-sysedge-coverage-review` or `/C-sysedge-quality-review`, so the graph reflects the current codebase.

## Prerequisites

- `python3 scripts/sys_edge.py init` has been run and the project is seeded with modules (ideally with `codePaths`).

**Connection settings** are read from the environment or from `.env` in the project root: `SYSEDGE_NEO4J_URI` (default `bolt://localhost:7688`), `SYSEDGE_NEO4J_USER` and `SYSEDGE_NEO4J_PASSWORD` (required). The legacy `NEO4J_*` names still work but print a deprecation notice; `NEO4J_AUTH` is never read. `SYSEDGE_PROJECT_ROOT` overrides the project root and `SYSEDGE_REPO` the repository id stamped on test paths.


## Usage

```bash
# Discover project roots (go.mod, package.json, pyproject.toml, ...) or module codePaths and list them
python3 scripts/sys_edge.py scan-code --all

# Scan everything discovered, without prompting
python3 scripts/sys_edge.py scan-code --all --yes

# Scan one module's registered codePaths
python3 scripts/sys_edge.py scan-code --module MOD-billing

# Scan one file or directory; restrict the language
python3 scripts/sys_edge.py scan-code --path src/billing --lang go
```

## What it does

1. Finds code roots: `--path`, the module's `codePaths`, or project-root markers (`go.mod`, `package.json`, `pyproject.toml`, `Cargo.toml`, `pom.xml`, `*.csproj`).
2. Parses exported symbols (Go, TypeScript, Python, Java, C#) and upserts `SysSymbol` nodes.
3. Links each symbol to its module with `CONTAINS_SYMBOL` (the module is inferred from `codePaths`).

It does **not** create `IMPLEMENTS` edges to features. Link the symbols that implement a feature explicitly:

```bash
python3 scripts/sys_edge.py link-symbol --feature F-BIL-001 --file src/billing/handlers.go --symbol CreateInvoice
python3 scripts/sys_edge.py link-endpoint --feature F-BIL-001 --method POST --path /api/invoices --binary api
```

## Output

A tree of scanned files with the number of symbols registered in each.

## Next steps

Run `/C-sysedge-scan-tests`, then `/C-sysedge-coverage-review` or `/C-sysedge-quality-review`.
