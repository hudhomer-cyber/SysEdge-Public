## AI modes and data egress

SysEdge has two ways to run an AI review (`audit-test`, `coverage-review`, `quality-review --ai`, `scan-tests --analyse`):

| Mode | How | What leaves your machine |
|---|---|---|
| **Assistant-driven** (recommended) | `--analyse --output-request <file>` writes a request; you (this {{AGENT}} session) do the analysis and save the result; `--apply-results <file>` (or `--apply-ai-results` for scan-tests) writes it back | Nothing beyond what this {{AGENT_TOOL}} session already sends |
| **Direct** | The command calls an AI provider itself: the first available of the `claude`, `kimi`, `gemini`, `qwen` and `codex` CLIs, then the `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` and `DASHSCOPE_API_KEY` APIs | Graph content (use-case text, test file paths and source, standards) is sent to that provider |

- Restrict direct mode to approved providers with `SYSEDGE_AI_PROVIDERS` (comma-separated, e.g. `SYSEDGE_AI_PROVIDERS=claude`) or per command with `--provider <name>`. Provider names: `claude`, `kimi`, `gemini`, `qwen`, `codex`, `anthropic`, `openai`, `dashscope`.
- When the project's data must not leave the session, use the assistant-driven mode only.
