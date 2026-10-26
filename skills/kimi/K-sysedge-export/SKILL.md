---
name: K-sysedge-export
description: Export a slice of the SysEdge graph as a Markdown (or JSON) document — user-story catalogue, use-case specification, application or technical architecture, test coverage, test summary, or the full detailed design with traceability matrix. Use when someone needs a readable requirements, architecture or test document generated from the graph. Triggers on /K-sysedge-export, "export the stories" or "generate the design document".
---

# /K-sysedge-export

Render a graph slice as a document with `sys_edge.py export`. The CLI renders the whole document; the skill picks the arguments, protects existing files and summarises the result. Nothing is written to the graph.


**Interface:**
```
/K-sysedge-export --type <stories|use-cases|application-arch|technical-arch|test-coverage|test-summary|detailed-design>
          [--instance <name>] [--module <module-id>] [--output <path>] [--format md|json]
```

Default output: `docs/exports/{type}-{scope}-{YYYY-MM-DD}.md`, where `{scope}` is the module id if `--module` is given, else the instance, else `all`. With `--format json` the file is written next to it with a `.json` suffix. Default format: `md`.

---

## Hard rules

| Rule | Why |
|---|---|
| Read-only | `export` only reads the graph |
| Use the CLI's output as-is | The CLI renders the document; do not re-render or rewrite it |
| Confirm before overwriting | Ask before replacing an existing file |

---

## Export types

| Type | Contents | Scope flags |
|---|---|---|
| `stories` | Each user story: actor, goal, benefit, narrative, acceptance criteria | `--instance` (stories linked to the instance's features), `--module` |
| `use-cases` | Each use case: description, preconditions, main flow, postconditions, linked stories and features | `--instance` |
| `application-arch` | Modules → features → use cases → stories ("what does the system do?") | `--instance` |
| `technical-arch` | Architecture decisions (by status) and standards ("how is it built?") | none (always the whole system) |
| `test-coverage` | Features and the tests that verify them, by tier | `--instance` |
| `test-summary` | Test counts by package and tier | `--instance` (matched against the test package's area) |
| `detailed-design` | The full traceability design: design by module (feature → use case → story with tests), traceability matrix, coverage gaps, test-link integrity, orphan nodes | `--instance`, `--module` |

`detailed-design` is the only type that lists orphans (section 5) and test-link problems (section 4, repair with `python3 cli/sys_edge.py check-test-links --fix`).

`--module` scopes only `stories` and `detailed-design`, and takes precedence over `--instance` there. For the other types it changes only the default file name, so scope them with `--instance` and leave `--module` out.

---

## Execution steps

### Step 1 — Parse arguments

Extract `--type` (required — ask if missing), `--instance`, `--module`, `--output` and `--format` from the user's message. If an instance or module name is unknown, list instances with `python3 cli/sys_edge.py status` or a module's features with `python3 cli/sys_edge.py features --module <id>`, and ask.

### Step 2 — Compute the output path and check it

Use `--output` if given, otherwise the default above (with `.json` for `--format json`). If the file exists:

```
Output file already exists: docs/exports/stories-billing-2026-05-22.md
Overwrite? [y/N]
```

On "no", suggest a new path (e.g. add `-v2`) and use it with `--output`.

### Step 3 — Run the export

```bash
python3 cli/sys_edge.py export --type <type> [--instance <inst>] [--module <module-id>] \
  --output <final path> --format md
```

Use `--format json` when JSON was requested: the file then holds `{"type": "<type>", "content": "<the rendered Markdown>"}`, plus `"data"` (counts, nodes with links, orphans, gaps, test links) for `detailed-design` only.

If the command exits non-zero, show stderr and stop: exit code 2 means the graph database is unavailable.

### Step 4 — Summarise

Read the file the CLI wrote and report what it contains. If the main sections are empty, say so and suggest causes (no nodes linked in that scope, wrong module id), but keep the file — an empty export is a valid result.

```
Exported stories for billing → docs/exports/stories-billing-2026-05-22.md (14 stories)
```

For `detailed-design` the CLI prints counts (modules, features, use cases, stories, tests, unresolved tests, orphans); repeat them.

---

## Example interactions

**User:** `/K-sysedge-export --type stories --instance billing`

```bash
python3 cli/sys_edge.py export --type stories --instance billing \
  --output docs/exports/stories-billing-2026-05-22.md --format md
```

**User:** `/K-sysedge-export --type stories --instance billing --module MOD-invoicing` → the module wins: `docs/exports/stories-MOD-invoicing-2026-05-22.md`.

**User:** `/K-sysedge-export --type application-arch --module MOD-billing` → `application-arch` cannot be scoped to a module. Say so, and offer `--instance <the module's instance>` instead (`docs/exports/application-arch-billing-2026-05-22.md`).

**User:** `/K-sysedge-export --type detailed-design --instance billing`

```bash
python3 cli/sys_edge.py export --type detailed-design --instance billing \
  --output docs/exports/detailed-design-billing-2026-05-22.md --format md
```

```
Exported detailed design to docs/exports/detailed-design-billing-2026-05-22.md
  Modules: 3    Features: 12    Use cases: 9    Stories: 14    Tests: 41
  Unresolved tests: 2 (listed in section 4 — run check-test-links --fix)
  Orphans: 3 (section 5)
```

**User:** `/K-sysedge-export --type technical-arch --format json` → `docs/exports/technical-arch-all-2026-05-22.json` containing `{"type", "content"}`.

---

## Error reference

| Error | Action |
|---|---|
| Exit code 1 | Show stderr; do not write a file yourself |
| Exit code 2 | Graph database unavailable — ask the user to start it |
| `--type` missing | Ask before running anything |
| Unknown instance | List instances with `status`, ask again |
