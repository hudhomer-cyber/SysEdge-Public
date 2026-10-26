---
name: C-sysedge-audit-test
description: Audit the tests behind a use case, feature or user story against the SysEdge AS-TEST architecture standards and record the verdict in the graph. Use after writing or changing tests, before closing an enhancement, or when a use case's coverage looks weak. Triggers on /C-sysedge-audit-test or "audit the tests for UC-...".
---

# /C-sysedge-audit-test

Review the test file that verifies a use case, feature or user story against the AS-TEST standards, then stamp the result on the node (`auditTestLastRun`, `auditTestResult`).


## When to use

- After writing or modifying a test.
- Before closing an enhancement or merging a branch.
- When investigating why a use case has weak coverage.

## Prerequisites

- The UC / feature / story node exists, and its tests have been registered with `/C-sysedge-scan-tests`.
- `seed-standards` has been run (the AS-TEST standards are read from the graph).

## Tiers

| Subject | Flag | Default `--tier` | Standards applied |
|---|---|---|---|
| Use case | `--uc UC-…` (or `--ucs A,B` for a batch) | `usecase` | `AS-TEST-UC-*` |
| Feature | `--feature F-…` | `integration` | `AS-TEST-COMP-*` (component and integration tests; there is no separate `component` tier) |
| User story | `--us US-…` | `e2e` | `AS-TEST-US-*` |

## Assistant-driven workflow (recommended)

SysEdge builds the request from the graph, this Claude session runs the analysis, and `--apply-results` writes the findings back. No graph content is sent to another AI provider.

```bash
# 0. Optional: check which test file will be used (auto-discovered from the graph)
python3 cli/sys_edge.py audit-test --uc UC-BIL-001 --preflight

# 1. Emit the request
python3 cli/sys_edge.py audit-test --uc UC-BIL-001 --analyse --output-request /tmp/audit-req.json

# 2. In this Claude session: read /tmp/audit-req.json, audit the test against each standard,
#    and save JSON matching the request's responseFormat to /tmp/audit-res.json

# 3. Apply the result
python3 cli/sys_edge.py audit-test --apply-results /tmp/audit-res.json
```

Override the auto-discovered file with `--file <path>`; override the tier with `--tier usecase|integration|e2e`.

## Direct AI workflow

The command calls an AI provider itself (see "AI modes and data egress").

```bash
python3 cli/sys_edge.py audit-test --uc UC-BIL-001
python3 cli/sys_edge.py audit-test --ucs UC-BIL-001,UC-BIL-002 --model haiku      # batch; exits 1 if any FAIL
python3 cli/sys_edge.py audit-test --feature F-BIL-001 --file tests/billing/test_invoices.py
python3 cli/sys_edge.py audit-test --us US-012 --provider claude
```

## AI modes and data egress

SysEdge has two ways to run an AI review (`audit-test`, `coverage-review`, `quality-review --ai`, `scan-tests --analyse`):

| Mode | How | What leaves your machine |
|---|---|---|
| **Assistant-driven** (recommended) | `--analyse --output-request <file>` writes a request; you (this Claude session) do the analysis and save the result; `--apply-results <file>` (or `--apply-ai-results` for scan-tests) writes it back | Nothing beyond what this Claude Code session already sends |
| **Direct** | The command calls an AI provider itself: the first available of the `claude`, `kimi`, `gemini`, `qwen` and `codex` CLIs, then the `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` and `DASHSCOPE_API_KEY` APIs | Graph content (use-case text, test file paths and source, standards) is sent to that provider |

- Restrict direct mode to approved providers with `SYSEDGE_AI_PROVIDERS` (comma-separated, e.g. `SYSEDGE_AI_PROVIDERS=claude`) or per command with `--provider <name>`. Provider names: `claude`, `kimi`, `gemini`, `qwen`, `codex`, `anthropic`, `openai`, `dashscope`.
- When the project's data must not leave the session, use the assistant-driven mode only.

## What it checks

- **Use-case tier (`AS-TEST-UC-*`):** element inventory, role visibility, happy path, validation and error paths, equivalence and boundary cases, semantic correctness, derivation from the specification.
- **Component / integration tier (`AS-TEST-COMP-*`):** isolation, clear mocks, contract assertions, error propagation.
- **E2E tier (`AS-TEST-US-*`):** actor journey, observable benefit, use-case composition, scope boundary, cross-instance hand-off, traceability with execution status.

## Output

PASS / WARN / FAIL per standard, with an action for each WARN or FAIL, and an overall verdict per subject.

## Result JSON format

```json
{
  "subjects": [
    {
      "subjectId": "UC-BIL-001",
      "subjectType": "SysUseCase",
      "tier": "usecase",
      "overall": "FAIL",
      "dimensions": [
        {
          "id": "AS-TEST-UC-003",
          "status": "FAIL",
          "finding": "No test exercises the validation-error path.",
          "action": "Add a test that submits invalid input and asserts the error response."
        }
      ]
    }
  ]
}
```

## Next steps

Fix the failing items and re-run. Record the test run with `python3 cli/sys_edge.py record-run --package <path> --passed N --failed N`, and check staleness with `python3 cli/sys_edge.py audit-status --instance <name>`.
