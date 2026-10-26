---
name: {{NAME}}
description: Check a story, use case, feature or module against every applicable SysEdge architecture standard, with automated traceability checks and optional AI dimensions. Use before refining a story or use case, during architecture review, or to find the quality gaps that block coverage reviews and test audits. Triggers on /{{SELF}} or "quality-review UC-...".
---

# /{{SELF}}

Evaluate an entity (`SysUserStory`, `SysUseCase`, `SysFeature` or `SysModule`) against all applicable `SysArchStd` nodes.

- Without `--ai` (free): the automated traceability checks (`TRC-*`) run as graph queries, manual standards are listed, and the exact `audit-test` / `coverage-review` commands for the AI dimensions are printed.


## When to use

- Before refining a story or use case.
- During architecture review.
- To find quality gaps that block coverage reviews or test audits.

## Prerequisites

- The entity exists in the graph and `seed-standards` has been run.

## Usage

```bash
# Automated checks for one entity, or for every use case of an instance
{{CLI}} quality-review --entity UC-BIL-001
{{CLI}} quality-review --instance billing --type SysUseCase

# Add the AI-evaluated standards (Kit); --model haiku|sonnet, --provider restricts the provider
{{CLI}} quality-review --entity US-012 --ai
{{CLI}} quality-review --entity F-BIL-001 --ai --provider claude
```

{{>ai-egress}}

## Output

A per-standard report: PASS / FAIL for automated checks, MANUAL for standards a person must judge, and AI-GOOD / AI-WEAK / AI-POOR with suggestions for AI-evaluated standards.

## Next steps

Update the entity (`update-story`, `update-usecase`, `update-feature`) and re-run. Add project-specific standards with `add-standard`.
