---
name: {{NAME}}
description: Register test files and test functions in the SysEdge graph and map unlinked tests to use cases, stories and features. Use after adding or renaming tests, after /{{SKILL:scan-code}}, or before a test audit or coverage review when test coverage in the graph looks wrong. Triggers on /{{SELF}} or "scan the tests into SysEdge".
---

# /{{SELF}}

Scan the test suite and register `SysTestPackage` and `SysTest` nodes. Optionally, map tests that have no traceability link to UC / US / feature nodes, with you ({{AGENT}}) doing the semantic matching.

`scan-tests` itself is a free CLI command

## When to use

- After `/{{SKILL:scan-code}}`.
- After adding, moving or renaming test files.
- Before `/{{SKILL:audit-test}}` or `/{{SKILL:coverage-review}}`, so coverage is accurate.

## Prerequisites

- `{{CLI}} init` has been run; modules have `testPaths` (the legacy `paths` property also works).

{{>connection}}

## Usage

```bash
# Discover test directories and list them
{{CLI}} scan-tests --all

# Scan everything discovered, without prompting
{{CLI}} scan-tests --all --yes

# One module, one instance, or one file
{{CLI}} scan-tests --module MOD-billing
{{CLI}} scan-tests --all --instance billing --yes
{{CLI}} scan-tests --path tests/billing/test_invoices.py

# Go tests (*_test.go) use a separate scanner
{{CLI}} scan-go-tests

# Check that registered test paths still resolve in this repo (dry run, then repair)
{{CLI}} check-test-links --verbose
{{CLI}} check-test-links --fix --yes
```

## What it does

1. Discovers test files from `SysModule.testPaths` and common locations (`tests/`, `__tests__/`, `e2e/`, `*.spec.*`, `*_test.*`, ...).
2. Creates `SysTestPackage` and `SysTest` nodes. Test paths are stored repo-relative and tagged with the repository id (`SYSEDGE_REPO` overrides it).
3. Links tests to their package with `CONTAINS_TEST`. Link tests to features with `link-feature --feature <F-id> --tests <pattern>`, or use `--analyse` below.

## Assistant-driven semantic mapping (`--analyse`)

Test files rarely name the UC or feature they verify. `--analyse` writes a request for **you** to map unlinked tests; SysEdge calls no external AI in this mode.

```bash
# 1. Emit the request (candidate UC/US/feature nodes, unlinked test files, their source and a prompt)
{{CLI}} scan-tests --module MOD-billing --analyse --max-files 10 --output-request /tmp/scan-req.json

# 2. In this {{AGENT}} session: read /tmp/scan-req.json, do the mapping, and save the JSON result
#    in the format the request describes to /tmp/scan-res.json

# 3. Apply the result
{{CLI}} scan-tests --apply-ai-results /tmp/scan-res.json
```

## Output

A tree of test directories and files with the number of test functions registered. In `--analyse` mode it writes a request instead of graph edges.

## Next steps

Run `/{{SKILL:audit-test}}` on a use case's tests, or `/{{SKILL:coverage-review}}` to check requirement coverage.
