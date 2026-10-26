---
name: K-sysedge-scan-tests
description: Register test files and test functions in the SysEdge graph and map unlinked tests to use cases, stories and features. Use after adding or renaming tests, after /K-sysedge-scan-code, or before a test audit or coverage review when test coverage in the graph looks wrong. Triggers on /K-sysedge-scan-tests or "scan the tests into SysEdge".
---

# /K-sysedge-scan-tests

Scan the test suite and register `SysTestPackage` and `SysTest` nodes. Optionally, map tests that have no traceability link to UC / US / feature nodes, with you (Kimi) doing the semantic matching.

`scan-tests` itself is a free CLI command

## When to use

- After `/K-sysedge-scan-code`.
- After adding, moving or renaming test files.
- Before `/K-sysedge-audit-test` or `/K-sysedge-coverage-review`, so coverage is accurate.

## Prerequisites

- `python3 cli/sys_edge.py init` has been run; modules have `testPaths` (the legacy `paths` property also works).

**Connection settings** are read from the environment or from `.env` in the project root: `SYSEDGE_NEO4J_URI` (default `bolt://localhost:7688`), `SYSEDGE_NEO4J_USER` and `SYSEDGE_NEO4J_PASSWORD` (required). The legacy `NEO4J_*` names still work but print a deprecation notice; `NEO4J_AUTH` is never read. `SYSEDGE_PROJECT_ROOT` overrides the project root and `SYSEDGE_REPO` the repository id stamped on test paths.


## Usage

```bash
# Discover test directories and list them
python3 cli/sys_edge.py scan-tests --all

# Scan everything discovered, without prompting
python3 cli/sys_edge.py scan-tests --all --yes

# One module, one instance, or one file
python3 cli/sys_edge.py scan-tests --module MOD-billing
python3 cli/sys_edge.py scan-tests --all --instance billing --yes
python3 cli/sys_edge.py scan-tests --path tests/billing/test_invoices.py

# Go tests (*_test.go) use a separate scanner
python3 cli/sys_edge.py scan-go-tests

# Check that registered test paths still resolve in this repo (dry run, then repair)
python3 cli/sys_edge.py check-test-links --verbose
python3 cli/sys_edge.py check-test-links --fix --yes
```

## What it does

1. Discovers test files from `SysModule.testPaths` and common locations (`tests/`, `__tests__/`, `e2e/`, `*.spec.*`, `*_test.*`, ...).
2. Creates `SysTestPackage` and `SysTest` nodes. Test paths are stored repo-relative and tagged with the repository id (`SYSEDGE_REPO` overrides it).
3. Links tests to their package with `CONTAINS_TEST`. Link tests to features with `link-feature --feature <F-id> --tests <pattern>`, or use `--analyse` below.

## Assistant-driven semantic mapping (`--analyse`)

Test files rarely name the UC or feature they verify. `--analyse` writes a request for **you** to map unlinked tests; SysEdge calls no external AI in this mode.

```bash
# 1. Emit the request (candidate UC/US/feature nodes, unlinked test files, their source and a prompt)
python3 cli/sys_edge.py scan-tests --module MOD-billing --analyse --max-files 10 --output-request /tmp/scan-req.json

# 2. In this Kimi session: read /tmp/scan-req.json, do the mapping, and save the JSON result
#    in the format the request describes to /tmp/scan-res.json

# 3. Apply the result
python3 cli/sys_edge.py scan-tests --apply-ai-results /tmp/scan-res.json
```

## Output

A tree of test directories and files with the number of test functions registered. In `--analyse` mode it writes a request instead of graph edges.

## Next steps

Run `/K-sysedge-audit-test` on a use case's tests, or `/K-sysedge-coverage-review` to check requirement coverage.
