---
name: {{NAME}}
description: Register source-code symbols (functions, handlers, components) in the SysEdge graph. Use after seeding a new project, after a large refactor, or before a coverage or quality review, when the graph's SysSymbol nodes are missing or out of date. Triggers on /{{SELF}} or "scan the code into SysEdge".
---

# /{{SELF}}

Scan the project source and register `SysSymbol` nodes under their owning `SysModule` (`CONTAINS_SYMBOL` edges). The scan is additive and safe to re-run.

`scan-code` itself is a free CLI command

## When to use

- After seeding a new project (`/{{SKILL:init}}`).
- After significant refactoring or renaming.
- Before `/{{SKILL:coverage-review}}` or `/{{SKILL:quality-review}}`, so the graph reflects the current codebase.

## Prerequisites

- `{{CLI}} init` has been run and the project is seeded with modules (ideally with `codePaths`).

{{>connection}}

## Usage

```bash
# Discover project roots (go.mod, package.json, pyproject.toml, ...) or module codePaths and list them
{{CLI}} scan-code --all

# Scan everything discovered, without prompting
{{CLI}} scan-code --all --yes

# Scan one module's registered codePaths
{{CLI}} scan-code --module MOD-billing

# Scan one file or directory; restrict the language
{{CLI}} scan-code --path src/billing --lang go
```

## What it does

1. Finds code roots: `--path`, the module's `codePaths`, or project-root markers (`go.mod`, `package.json`, `pyproject.toml`, `Cargo.toml`, `pom.xml`, `*.csproj`).
2. Parses exported symbols (Go, TypeScript, Python, Java, C#) and upserts `SysSymbol` nodes.
3. Links each symbol to its module with `CONTAINS_SYMBOL` (the module is inferred from `codePaths`).

It does **not** create `IMPLEMENTS` edges to features. Link the symbols that implement a feature explicitly:

```bash
{{CLI}} link-symbol --feature F-BIL-001 --file src/billing/handlers.go --symbol CreateInvoice
{{CLI}} link-endpoint --feature F-BIL-001 --method POST --path /api/invoices --binary api
```

## Output

A tree of scanned files with the number of symbols registered in each.

## Next steps

Run `/{{SKILL:scan-tests}}`, then `/{{SKILL:coverage-review}}` or `/{{SKILL:quality-review}}`.
