---
name: {{NAME}}
description: Review whether a user story or use case is specified well enough to build and test, against the SysEdge AS-REQ standards, and record the verdict in the graph. Use before writing tests for a new story or use case, during refinement, or when requirements change in a PR. Triggers on /{{SELF}} or "review coverage for US-.../UC-...".
---

# /{{SELF}}

Review a `SysUserStory` or `SysUseCase` against the AS-REQ standards (actor completeness, authorization, exceptions, ontology alignment, testability, scope, traceability) and stamp the result on the node (`coverageReviewLastRun`, `coverageReviewResult`).

{{>kit}}

## When to use

- Before writing tests for a new story or use case.
- During refinement, to find gaps in requirements.
- In PR review, when requirements change.

## Prerequisites

- The story / UC nodes exist; code and tests have been scanned (`/{{SKILL:scan-code}}`, `/{{SKILL:scan-tests}}`).
- `seed-standards` has been run.

## Assistant-driven workflow (recommended)

```bash
# 1. Emit the request (one subject)
{{CLI}} coverage-review --uc UC-BIL-001 --analyse --output-request /tmp/cov-req.json
#    or every story of an instance, 5 subjects per request
{{CLI}} coverage-review --instance billing --analyse --batch-size 5 --output-request /tmp/cov-req.json

# 2. In this {{AGENT}} session: read the request, review each subject against each standard,
#    and save JSON matching the request's responseFormat to /tmp/cov-res.json

# 3. Apply the result; --file-proposals also files a SysProposal for every WARN or FAIL dimension
{{CLI}} coverage-review --apply-results /tmp/cov-res.json --file-proposals
```

## Direct AI workflow

The command calls an AI provider itself (see "AI modes and data egress").

```bash
{{CLI}} coverage-review --us US-012                 # one story (AS-REQ review)
{{CLI}} coverage-review --uc UC-BIL-001             # one use case (AS-REQ review)
{{CLI}} coverage-review --instance billing          # every story of the instance (AS-REQ batch)
```

**Chain review** (no `--us` / `--uc`): `--scope us|uc|both` with `--module MOD-…` or `--story US-…` checks whether the use cases cover each story (level 1) and the features cover each use case (level 2). Its verdicts are **PASS / PARTIAL / GAP / INSUFFICIENT**, and `--file-proposals` there files a proposal for every GAP.

```bash
{{CLI}} coverage-review --module MOD-billing --scope both --file-proposals
```

{{>ai-egress}}

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

Fix WARN / FAIL items with `update-usecase` / `update-story`, re-run, then write the tests and run `/{{SKILL:audit-test}}`.
