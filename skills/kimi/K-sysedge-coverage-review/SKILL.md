---
name: K-sysedge-coverage-review
description: Review whether a user story or use case is specified well enough to build and test, against the SysEdge AS-REQ standards, and record the verdict in the graph. Use before writing tests for a new story or use case, during refinement, or when requirements change in a PR. Triggers on /K-sysedge-coverage-review or "review coverage for US-.../UC-...".
---

# /K-sysedge-coverage-review

Review a `SysUserStory` or `SysUseCase` against the AS-REQ standards (actor completeness, authorization, exceptions, ontology alignment, testability, scope, traceability) and stamp the result on the node (`coverageReviewLastRun`, `coverageReviewResult`).


## When to use

- Before writing tests for a new story or use case.
- During refinement, to find gaps in requirements.
- In PR review, when requirements change.

## Prerequisites

- The story / UC nodes exist; code and tests have been scanned (`/K-sysedge-scan-code`, `/K-sysedge-scan-tests`).
- `seed-standards` has been run.

## Assistant-driven workflow (recommended)

```bash
# 1. Emit the request (one subject)
python3 cli/sys_edge.py coverage-review --uc UC-BIL-001 --analyse --output-request /tmp/cov-req.json
#    or every story of an instance, 5 subjects per request
python3 cli/sys_edge.py coverage-review --instance billing --analyse --batch-size 5 --output-request /tmp/cov-req.json

# 2. In this Kimi session: read the request, review each subject against each standard,
#    and save JSON matching the request's responseFormat to /tmp/cov-res.json

# 3. Apply the result; --file-proposals also files a SysProposal for every WARN or FAIL dimension
python3 cli/sys_edge.py coverage-review --apply-results /tmp/cov-res.json --file-proposals
```

## Direct AI workflow

The command calls an AI provider itself (see "AI modes and data egress").

```bash
python3 cli/sys_edge.py coverage-review --us US-012                 # one story (AS-REQ review)
python3 cli/sys_edge.py coverage-review --uc UC-BIL-001             # one use case (AS-REQ review)
python3 cli/sys_edge.py coverage-review --instance billing          # every story of the instance (AS-REQ batch)
```

**Chain review** (no `--us` / `--uc`): `--scope us|uc|both` with `--module MOD-…` or `--story US-…` checks whether the use cases cover each story (level 1) and the features cover each use case (level 2). Its verdicts are **PASS / PARTIAL / GAP / INSUFFICIENT**, and `--file-proposals` there files a proposal for every GAP.

```bash
python3 cli/sys_edge.py coverage-review --module MOD-billing --scope both --file-proposals
```

## AI modes and data egress

SysEdge has two ways to run an AI review (`audit-test`, `coverage-review`, `quality-review --ai`, `scan-tests --analyse`):

| Mode | How | What leaves your machine |
|---|---|---|
| **Assistant-driven** (recommended) | `--analyse --output-request <file>` writes a request; you (this Kimi session) do the analysis and save the result; `--apply-results <file>` (or `--apply-ai-results` for scan-tests) writes it back | Nothing beyond what this Kimi Code CLI session already sends |
| **Direct** | The command calls an AI provider itself: the first available of the `claude`, `kimi`, `gemini`, `qwen` and `codex` CLIs, then the `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` and `DASHSCOPE_API_KEY` APIs | Graph content (use-case text, test file paths and source, standards) is sent to that provider |

- Restrict direct mode to approved providers with `SYSEDGE_AI_PROVIDERS` (comma-separated, e.g. `SYSEDGE_AI_PROVIDERS=claude`) or per command with `--provider <name>`. Provider names: `claude`, `kimi`, `gemini`, `qwen`, `codex`, `anthropic`, `openai`, `dashscope`.
- When the project's data must not leave the session, use the assistant-driven mode only.

## What it checks

| Standard | Focus |
|---|---|
| AS-REQ-001 | Actor completeness |
| AS-REQ-002 | Authorization boundaries (reads the UC's `authorizedRoles`) |
| AS-REQ-003 | Exception and error coverage (reads `failureScenarios`) |
| AS-REQ-004 | Ontology alignment |
| AS-REQ-005 | Testability of acceptance criteria |
| AS-REQ-006 | Scope definition |
| AS-REQ-007 | Bidirectional traceability with execution |

## Output

AS-REQ reviews (`--us`, `--uc`, `--instance`, and the assistant-driven mode) report **PASS / WARN / FAIL** per standard with a recommendation for each WARN or FAIL.

## Result JSON format

```json
{
  "subjects": [
    {
      "subjectId": "UC-BIL-001",
      "subjectType": "SysUseCase",
      "overall": "WARN",
      "dimensions": [
        {
          "id": "AS-REQ-003",
          "status": "WARN",
          "finding": "No failure scenario for a declined card.",
          "recommendation": "Add a failure scenario with update-usecase --failure-scenarios."
        }
      ]
    }
  ]
}
```

## Next steps

Fix WARN / FAIL items with `update-usecase` / `update-story`, re-run, then write the tests and run `/K-sysedge-audit-test`.
