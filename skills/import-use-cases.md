---
name: {{NAME}}
description: Import use cases from a Markdown, TXT or DOCX document into the SysEdge graph as SysUseCase nodes (with flows, links to stories and features), with a preview and an explicit confirmation before anything is written. Use when the user gives a path to a use-case or functional-specification document and wants its use cases in the graph. Triggers on /{{SELF}}, "import these use cases" or "load the use cases into SysEdge".
---

# /{{SELF}}

Parse a use-case document, write a `sysedge.import.v1` file, preview it with `preview-import`, let the user confirm each decision, commit with `commit-import`, then add the flows with `update-usecase`. All graph writes go through the CLI.

{{>kit}}

**Command interface:** `/{{SELF}} <path> [--instance <name>] [--link-stories] [--link-features] [--dry-run]`

---

## Hard rules

| Prohibited | Why |
|---|---|
| Writing to Neo4j directly (raw Cypher, the Neo4j driver, new scripts) | All writes go through `preview-import` / `commit-import` and the `update-*` / `link-*` commands |
| Committing with no instance | The instance must be resolved first |
| Committing without showing the preview | The user must see and confirm every decision |
| Reusing an existing use-case id for a new use case | `commit-import` writes by id: an existing use case with that id would be overwritten |
| Importing ADR sections or user stories as use cases | ADRs are recorded with `create-adr`; stories are imported with `/{{SKILL:import-stories}}` |

---

## Step 1 — Parse the invocation

- `<path>` — required. If missing, ask: "Please provide a path to the use case document."
- `--instance <name>` — the instance that will own the use cases. To see the project's instances, run `{{CLI}} status`.
- `--link-stories` — keep story references, so `commit-import` adds `REALIZED_BY` edges from existing stories.
- `--link-features` — keep feature references, so `commit-import` adds `REQUIRES` edges to existing features.
- `--dry-run` — run everything up to and including `preview-import`, but do not commit.

## Step 2 — Read the document

Read `.md` / `.txt` files directly. For `.docx`: `python3 -c "import docx,sys; print('\n'.join(p.text for p in docx.Document(sys.argv[1]).paragraphs))" <path>` (needs `python-docx`; otherwise ask the user to convert the file to Markdown). Compute the hash for the envelope with `sha256sum <path>`.

## Step 3 — Classify each section

| Signal | Classification |
|---|---|
| "Status:" and "Decision:" headings | ADR — skip, warning `adr-skipped` |
| "As a … I want …" phrasing | User story — skip, warning `story-skipped` (suggest `/{{SKILL:import-stories}}`) |
| Named actor performing steps with system responses, goal-oriented | Use case — extract |
| Goal-oriented but no clear actor/system steps | Ambiguous — extract with `_ambiguous: true` and `_confidence` ≤ 0.5 |
| Table of contents, glossary, overview prose | Skip silently |

## Step 4 — Extract the use cases

Write `/tmp/sysedge-import-use-cases-<timestamp>.json` (`<timestamp>` from `date +%s`):

```json
{
  "schema": "sysedge.import.v1",
  "kind": "use-cases",
  "instance": "billing",
  "source": {"path": "/abs/path/use-cases.md", "sha256": "<hash>", "extracted_at": "2026-05-22T14:45:56Z"},
  "entities": [
    {
      "id_hint": "UC-BIL-001",
      "name": "issue-invoice",
      "title": "Finance clerk issues an invoice",
      "instance": "billing",
      "description": "A finance clerk raises an invoice for a completed order and sends it to the customer.",
      "priority": "P1",
      "preconditions": ["Clerk is signed in with billing:write", "The order is complete"],
      "main_flow": ["1. Clerk opens the order.", "2. Clerk chooses Issue invoice.", "3. System creates the invoice and emails it."],
      "alternate_flows": [{"name": "Customer has no email", "steps": ["3a. System marks the invoice for postal delivery."]}],
      "postconditions": ["The invoice exists with status Sent."],
      "authorized_roles": ["Finance clerk"],
      "failure_scenarios": ["Order not complete", "Email bounces"],
      "story_refs": ["US-BIL-001"],
      "feature_refs": ["F-BIL-001"],
      "_confidence": 0.9,
      "_ambiguous": false,
      "_source_span": {"start_line": 14, "end_line": 52}
    }
  ],
  "warnings": [{"ref": "UC-BIL-001", "code": "ambiguous", "message": "…"}]
}
```

Field rules:
- `id_hint` — **always set it**; it becomes the node id (without it the CLI invents `UC-IMPORT-NNN`). Use the document's `UC-` id if present, else `UC-<AREA>-NNN`. Check that a new id is free: `{{CLI}} show-usecase --id <id_hint>` must report "not found".
- `name` — a lowercase, hyphenated slug (`issue-invoice`). `preview-import` treats a use case with the same `name` in the same instance as a duplicate.
- `title` — at most 120 characters. `description` — one to three sentences.
- `priority` — `P0–P3`; omit if absent (the CLI defaults to `P1`).
- `preconditions`, `main_flow`, `alternate_flows`, `postconditions`, `authorized_roles`, `failure_scenarios` — lists (`[]` if absent). `commit-import` does not write these; Step 8 does.
- `story_refs` — existing story ids, only with `--link-stories`; `feature_refs` — existing feature ids, only with `--link-features`; otherwise `[]`.
- `_confidence` — 0.85–1.0 complete; 0.65–0.84 missing pre- or postconditions; 0.5–0.64 prose flow without steps; below 0.5 ambiguous. Below 0.5, `preview-import` marks the entity `skip` (`low-conf`).

**Instance** (envelope `instance`, and each entity's `instance`): the `--instance` flag, else document metadata (`instance:` front matter or `<!-- instance: … -->`), else a path segment that matches an instance listed by `status`, else `null` (blocks the commit until the user names one).

## Step 5 — Quality gates

Add a `warnings` entry for each finding; never drop a section silently.

| Condition | Warning | Proposed decision |
|---|---|---|
| `_confidence < 0.5` | `low-confidence` | `skip` (the CLI does this) |
| Empty `main_flow` | `missing-main-flow` | `skip` |
| `_ambiguous: true` | `ambiguous` | ask the user |
| Duplicate or missing `name` within the file | `slug-conflict` | derive a new slug from the title |
| `id_hint` already exists | `id-in-use` | choose a free id |
| Title longer than 120 characters | `title-truncated` | truncate |
| Envelope `instance` is `null` | — | block the commit |

## Step 6 — Preview

```bash
{{CLI}} preview-import /tmp/sysedge-import-use-cases-<timestamp>.json
```

The CLI prints a table and writes **`/tmp/sysedge-import-use-cases-<timestamp>-decisions.json`**, a list of `{"ref", "action", "status", "match_id", "entity"}` entries:

| CLI `status` | Meaning | CLI `action` |
|---|---|---|
| `new` | No use case with this `name` in the instance | `create` |
| `low-conf` | `_confidence` below 0.5 | `skip` |
| `duplicate` | Same `name` and instance as `match_id` | `skip` |

(If the CLI's hint line prints `--file`, ignore it: the import file is a positional argument.)

Show one table combining the CLI's status and action with your warnings and the skipped sections:

```
## Import Preview — use-cases.md
Instance: billing      ADR sections skipped: 1      Story sections skipped: 0

| # | ID | Name | Title | Steps (main/alt) | Conf. | CLI status | Decision | Warnings |
|---|----|------|-------|------------------|-------|------------|----------|----------|
| 1 | UC-BIL-001 | issue-invoice | Finance clerk issues an invoice | 3 / 1 | 0.90 | new | create | — |
| 2 | UC-BIL-002 | refund-payment | Clerk refunds a payment | 4 / 0 | 0.61 | new | ask | ambiguous |
| 3 | UC-BIL-003 | dunning | Overdue reminder | 0 / 0 | 0.30 | low-conf | skip | missing-main-flow |
```

If the instance is unresolved, say so prominently and list the instances from `{{CLI}} status`.

## Step 7 — Confirm, settle decisions, commit

Ask once: `Proceed with import? [y = commit / e = edit / n = abort]`

- **e** — the user edits the import file (then re-run Step 6, which rewrites the decisions file) or the `action` values in the decisions file.
- **n** — stop; both files stay in `/tmp`. `--dry-run` stops here too: "Dry run complete. No data was written."
- An unresolved instance blocks **y**.

For use cases, `commit-import` writes only `create` entries; any other action is counted as skipped. Set each entry to `create` or `skip` (resolve every ambiguous one with the user) using your file-editing tool. To change an existing use case instead, `skip` it here and use `update-usecase` / `link-usecase` in Step 8.

```bash
{{CLI}} commit-import /tmp/sysedge-import-use-cases-<timestamp>.json \
  --decisions /tmp/sysedge-import-use-cases-<timestamp>-decisions.json
```

`create` writes id, name, title, instance, description and priority, `REALIZED_BY` edges from `story_refs` and `REQUIRES` edges to `feature_refs`. The CLI prints one `✓ <id>` line per use case and `committed=… skipped=… failed=…`. Report individual failures and carry on.

## Step 8 — Write what commit-import does not

For each committed use case, add the flows and the fields `coverage-review` reads (lists joined into one string; number the steps):

```bash
{{CLI}} update-usecase --id UC-BIL-001 \
  --preconditions "Clerk is signed in with billing:write; the order is complete" \
  --main-flow "1. Clerk opens the order. 2. Clerk chooses Issue invoice. 3. System creates the invoice and emails it." \
  --postconditions "The invoice exists with status Sent." \
  --authorized-roles "Finance clerk" \
  --failure-scenarios "Order not complete; email bounces; 3a. no email: postal delivery"
```

Put alternate flows in `--main-flow` (after the main steps) or `--failure-scenarios`. Add links found later with `{{CLI}} link-usecase --id UC-BIL-001 --feature F-BIL-002 --story US-BIL-003`.

Summarise:

```
## Import Complete
Created: 1   Skipped: 2
Created: UC-BIL-001 issue-invoice — flows written with update-usecase
Skipped: UC-BIL-002 (ambiguous — user chose skip), UC-BIL-003 (no main flow)

Next steps:
- Add a main flow to UC-BIL-003 in the document and re-import it
- Link use cases to features: {{CLI}} link-usecase --id UC-BIL-001 --feature F-BIL-001
- Check for duplicates: /{{SKILL:analyse-graph}} --types use-cases
```

---

## Do not

- Write to Neo4j directly, or create a script to do the import.
- Commit without showing the preview, or with an unresolved instance.
- Expect `review`, `update` or `merge-into` to write a use case — only `create` does.
- Import ADR or user-story sections as use cases.
- Invent fields outside the extraction schema.
- Run `git commit`, `reset`, or `seed` without `--instance`.
