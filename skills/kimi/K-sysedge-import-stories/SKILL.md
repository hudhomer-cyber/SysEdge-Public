---
name: K-sysedge-import-stories
description: Import user stories from a Markdown, TXT or DOCX document into the SysEdge graph as SysUserStory nodes, with a preview and an explicit confirmation before anything is written. Use when the user gives a path to a requirements or user-story document and wants its stories in the graph. Triggers on /K-sysedge-import-stories, "import these stories" or "load the user stories into SysEdge".
---

# /K-sysedge-import-stories

Parse a user-story document, write a `sysedge.import.v1` file, preview it with `preview-import`, let the user confirm each decision, and commit with `commit-import`. All graph writes go through the CLI.


**Command interface:** `/K-sysedge-import-stories <path> [--instance <name>] [--link-features] [--dry-run]`

---

## Hard rules

| Prohibited | Why |
|---|---|
| Writing to Neo4j directly (raw Cypher, the Neo4j driver, new scripts) | All writes go through `preview-import` / `commit-import` and the `update-*` / `link-*` commands |
| Committing with no instance | The instance must be resolved first |
| Committing without showing the preview | The user must see and confirm every decision |
| Reusing an existing story id for a new story | `commit-import` writes by id: an existing story with that id would be overwritten |
| Importing ADR sections ("Status:" + "Decision:" headings) as stories | They are architecture decisions; record them with `create-adr` |

---

## Step 1 — Parse the invocation

- `<path>` — required. If missing, ask: "Please provide a path to the user story document."
- `--instance <name>` — the instance that will own the stories. To see the project's instances, run `python3 cli/sys_edge.py status`.
- `--link-features` — keep feature references, so `commit-import` adds `REQUIRES` edges to existing features.
- `--dry-run` — run everything up to and including `preview-import`, but do not commit.

## Step 2 — Read the document

Read `.md` / `.txt` files directly. For `.docx`: `python3 -c "import docx,sys; print('\n'.join(p.text for p in docx.Document(sys.argv[1]).paragraphs))" <path>` (needs `python-docx`; otherwise ask the user to convert the file to Markdown). Compute the hash for the envelope with `sha256sum <path>`.

## Step 3 — Extract the stories

A **user story** is a passage with "As a … I want … (so that …)" phrasing, a numbered or headed block with an actor and a goal, or an explicit "User Story" heading.

Write `/tmp/sysedge-import-stories-<timestamp>.json` (`<timestamp>` from `date +%s`):

```json
{
  "schema": "sysedge.import.v1",
  "kind": "stories",
  "instance": "billing",
  "source": {"path": "/abs/path/stories.md", "sha256": "<hash>", "extracted_at": "2026-05-22T14:32:03Z"},
  "entities": [
    {
      "id_hint": "US-BIL-001",
      "title": "Customer downloads an invoice",
      "actor": "Customer",
      "goal": "download any past invoice as a PDF",
      "benefit": "I can file it with my accounts",
      "narrative": "As a Customer, I want to download any past invoice as a PDF, so that I can file it with my accounts.",
      "priority": "P1",
      "acceptanceCriteria": ["Given a paid invoice, when I click Download, then a PDF opens"],
      "feature_refs": ["F-BIL-002"],
      "_confidence": 0.9,
      "_source_span": {"start_line": 12, "end_line": 28}
    }
  ],
  "warnings": [{"ref": "US-BIL-001", "code": "missing-ac", "message": "…"}]
}
```

Field rules:
- `id_hint` — **always set it**; without it the CLI invents `US-IMPORT-NNN`. Use the document's id if it has one, else `US-<AREA>-NNN`. Check that a new id is free: `python3 cli/sys_edge.py show-story --id <id_hint>` must report "not found".
- `title` — heading or first sentence, without the "As a …" preamble, at most 120 characters.
- `actor`, `goal`, `benefit` — the "As a", "I want" and "so that" clauses (`benefit` may be `""`).
- `narrative` — the canonical sentence; omit "so that" when there is no benefit.
- `priority` — map `Must/Should/Could/Won't`, `Critical/High/Medium/Low` or `P0–P3` to `P0–P3`; omit if absent (the CLI defaults to `P1`).
- `acceptanceCriteria` — Given/When/Then items, or `[]`. `commit-import` does not write them; Step 8 does.
- `feature_refs` — feature ids or names mentioned in the story, only with `--link-features`; otherwise `[]`.
- `_confidence` — 0.85–1.0 complete with ACs; 0.6–0.84 missing benefit or ACs; 0.3–0.59 missing actor or goal; below 0.3 ambiguous. Below 0.5, `preview-import` marks the entity `skip` (`low-conf`).

**Instance** (envelope `instance`): the `--instance` flag, else document metadata (`instance:` front matter or `<!-- instance: … -->`), else a path segment that matches an instance listed by `status`, else `null` (blocks the commit until the user names one).

## Step 4 — Quality gates

Add a `warnings` entry for each finding; never drop a story silently.

| Condition | Warning | Proposed decision |
|---|---|---|
| `_confidence < 0.5` | `low-confidence` | `skip` (the CLI does this) |
| No actor | `missing-actor` | ask the user before committing |
| No acceptance criteria | `missing-ac` | `create`, then add ACs later |
| Title longer than 120 characters | `title-truncated` | truncate |
| `id_hint` already exists | `id-in-use` | choose a free id |
| Envelope `instance` is `null` | — | block the commit |

## Step 5 — Preview

```bash
python3 cli/sys_edge.py preview-import /tmp/sysedge-import-stories-<timestamp>.json
```

The CLI compares each entity with the graph, prints a table, and writes the decisions file next to the import file: **`/tmp/sysedge-import-stories-<timestamp>-decisions.json`**, a list of

```json
{"ref": "US-BIL-001", "action": "create", "status": "new", "match_id": null, "entity": { … }}
```

| CLI `status` | Meaning | CLI `action` |
|---|---|---|
| `new` | No match in the graph | `create` |
| `low-conf` | `_confidence` below 0.5 | `skip` |
| `duplicate` | A story with the same title exists (`match_id`) | `skip` |
| `near-dup` | Same actor and ≥ 70 % title-word overlap with `match_id` | `review` |

(If the CLI's hint line prints `--file`, ignore it: the import file is a positional argument.)

Show the user one table that combines the CLI's status and action with your warnings:

```
## Import Preview — stories.md
Instance: billing        Source: /abs/path/stories.md

| # | ID | Title | Actor | ACs | Conf. | CLI status | Decision | Warnings |
|---|----|-------|-------|-----|-------|------------|----------|----------|
| 1 | US-BIL-001 | Customer downloads an invoice | Customer | 1 | 0.90 | new | create | — |
| 2 | US-BIL-002 | Finance exports invoices | Finance | 0 | 0.72 | new | create | missing-ac |
| 3 | US-BIL-003 | Customer gets an invoice | Customer | 2 | 0.88 | near-dup ← US-007 | review | — |
| 4 | US-BIL-004 | Token refresh | — | 0 | 0.40 | low-conf | skip | low-confidence, missing-actor |
```

If the instance is unresolved, say so prominently and list the instances from `python3 cli/sys_edge.py status`.

## Step 6 — Confirm

Ask once: `Proceed with import? [y = commit / e = edit / n = abort]`

- **e** — the user edits either the import file (then re-run Step 5, which rewrites the decisions file) or the `action` values in the decisions file (then go straight to Step 7).
- **n** — stop; both files stay in `/tmp`.
- `--dry-run` — stop here: "Dry run complete. No data was written."
- An unresolved instance blocks **y**.

## Step 7 — Settle every decision, then commit

`commit-import` writes a story only for `create` and `merge-into`; every other action (`skip`, `review`, `update`) is counted as skipped. Before committing, edit the decisions file so each entry is one of:

| Action | Effect |
|---|---|
| `create` | Writes the story with id `entity.id_hint` (title, actor, goal, benefit, narrative, priority, instance) and `REQUIRES` edges to `entity.feature_refs` |
| `merge-into` | Same write, used to update an existing story: set `entity.id_hint` to the story being updated (usually `match_id`). Its title, actor, goal, benefit, narrative and priority are overwritten, so confirm with the user first |
| `skip` | Nothing is written |

Resolve every `review` with the user (`create`, `merge-into` or `skip`). Edit the decisions file with your file-editing tool; do not write a script. Then:

```bash
python3 cli/sys_edge.py commit-import /tmp/sysedge-import-stories-<timestamp>.json \
  --decisions /tmp/sysedge-import-stories-<timestamp>-decisions.json
```

The CLI prints one `✓ <id>` line per story and `committed=… skipped=… failed=…`. Report individual failures and carry on; re-running the commit is safe for stories that were written.

## Step 8 — Write what commit-import does not

For each committed story with acceptance criteria:

```bash
python3 cli/sys_edge.py update-story --id US-BIL-001 \
  --acceptance-criteria "Given a paid invoice, when I click Download, then a PDF opens"
```

(Separate several criteria with "; ".) Link stories to features found after the import with `python3 cli/sys_edge.py link-story --story US-BIL-002 --features F-BIL-003`, and to use cases with `python3 cli/sys_edge.py link-usecase --id UC-BIL-001 --story US-BIL-002`.

Summarise:

```
## Import Complete
Created: 2   Merged: 0   Skipped: 2
Created: US-BIL-001 Customer downloads an invoice · US-BIL-002 Finance exports invoices (no ACs yet)
Skipped for review: US-BIL-003 (near-duplicate of US-007)

Next steps:
- Add acceptance criteria to US-BIL-002 (update-story --acceptance-criteria)
- Compare US-BIL-003 with US-007 (`/K-sysedge-analyse-graph --types stories`)
- Check the instance view: python3 cli/sys_edge.py briefing --instance billing
```

---

## Do not

- Write to Neo4j directly, or create a script to do the import.
- Commit without showing the preview, or with an unresolved instance.
- Leave `review` actions in the decisions file and expect them to be written.
- Merge near-duplicates without the user's explicit choice.
- Treat ADR sections as stories.
- Run `git commit`, `reset`, or `seed` without `--instance`.
