---
name: K-sysedge-quality-review
description: Check a story, use case, feature or module against every applicable SysEdge architecture standard, with automated traceability checks and optional AI dimensions. Use before refining a story or use case, during architecture review, or to find the quality gaps that block coverage reviews and test audits. Triggers on /K-sysedge-quality-review or "quality-review UC-...".
---

# /K-sysedge-quality-review

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
python3 scripts/sys_edge.py quality-review --entity UC-BIL-001
python3 scripts/sys_edge.py quality-review --instance billing --type SysUseCase

# Add the AI-evaluated standards (Kit); --model haiku|sonnet, --provider restricts the provider
python3 scripts/sys_edge.py quality-review --entity US-012 --ai
python3 scripts/sys_edge.py quality-review --entity F-BIL-001 --ai --provider claude
```

## AI modes and data egress

SysEdge has two ways to run an AI review (`audit-test`, `coverage-review`, `quality-review --ai`, `scan-tests --analyse`):

| Mode | How | What leaves your machine |
|---|---|---|
| **Assistant-driven** (recommended) | `--analyse --output-request <file>` writes a request; you (this Kimi session) do the analysis and save the result; `--apply-results <file>` (or `--apply-ai-results` for scan-tests) writes it back | Nothing beyond what this Kimi Code CLI session already sends |
| **Direct** | The command calls an AI provider itself: the first available of the `claude`, `kimi`, `gemini`, `qwen` and `codex` CLIs, then the `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` and `DASHSCOPE_API_KEY` APIs | Graph content (use-case text, test file paths and source, standards) is sent to that provider |

- Restrict direct mode to approved providers with `SYSEDGE_AI_PROVIDERS` (comma-separated, e.g. `SYSEDGE_AI_PROVIDERS=claude`) or per command with `--provider <name>`. Provider names: `claude`, `kimi`, `gemini`, `qwen`, `codex`, `anthropic`, `openai`, `dashscope`.
- When the project's data must not leave the session, use the assistant-driven mode only.

## Output

A per-standard report: PASS / FAIL for automated checks, MANUAL for standards a person must judge, and AI-GOOD / AI-WEAK / AI-POOR with suggestions for AI-evaluated standards.

## Next steps

Update the entity (`update-story`, `update-usecase`, `update-feature`) and re-run. Add project-specific standards with `add-standard`.
