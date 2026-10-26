---
name: C-sysedge-analyse-graph
description: Analyse the SysEdge graph for structural problems — merge candidates (near-duplicate features, use cases or stories), split candidates (features carrying too many use cases) and orphans (nodes missing their expected links) — and propose the exact fix commands. Use after a large import, seed or scan, before a release review, or at the start of a graph cleanup session. Triggers on /C-sysedge-analyse-graph, "analyse the graph", "find duplicates" or "find orphans".
---

# /C-sysedge-analyse-graph

Run the three `analyse` checks, add your own semantic judgement, and report findings with the exact command to fix each one.


**Interface:**
```
/C-sysedge-analyse-graph [--instance <name>]
          [--types features,use-cases,stories]
          [--merge-threshold 0.80]
          [--max-responsibilities 3]
          [--ignore-file .sysedge-ignore]
```

Defaults: all types, threshold 0.80, max-responsibilities 3, ignore file `.sysedge-ignore`.

---

## Hard rules

| Rule | Why |
|---|---|
| Read-only by default | The analysis only reads the graph. The skill writes only on the user's explicit confirmation: a `merge-nodes` run, a linking command, or a line appended to `.sysedge-ignore` |
| Never run `merge-nodes` without confirmation | A merge transfers every edge and **deletes** the absorbed node |
| Check the instance before any merge | The CLI does not refuse cross-instance merges, so the skill must (see Step 8) |
| Honour the ignore file | Dismissed findings stay dismissed |
| No raw Cypher, no new scripts | Every read and write goes through `sys_edge.py` |

---

## Three analyses

### 1. Merge candidates

Pairs of same-type nodes (`SysFeature`, `SysUseCase`, `SysUserStory`) that may be duplicates. The CLI pre-filters pairs by word overlap of their names (`overlap`, Jaccard of name tokens, 0–1) and returns both names and descriptions. You then judge semantic similarity by reading them: do they describe the same thing from different angles, or genuinely different things?

**Score:** `0.5 × overlap + 0.5 × semantic_similarity` (your judgement, 0–1). Report pairs whose score is at least `--merge-threshold`. Because the CLI only returns pairs whose word overlap reaches its own `--threshold`, run it with a lower threshold (0.5) so near-duplicates worded differently still reach you.

Recommendation per pair: keep the node with more links or the more specific name; absorb the other.

### 2. Split candidates

Features linked to more than `--max-responsibilities` use cases. For each, read the use-case titles, group them into coherent clusters, and propose a sub-feature name per cluster (verb–object form, e.g. "Issue invoices", "Collect payments"). Clusters that all serve one user goal are not a split candidate: say so.

### 3. Orphans

| JSON key (`orphans`) | Meaning | Suggested fix |
|---|---|---|
| `Stories without UC` | `SysUserStory` with no `REALIZED_BY` use case | `link-usecase --id <UC> --story <US>` for an existing UC, or `create-usecase --id <UC> --instance <inst> --title "…" --story <US>` |
| `Use cases without features` | `SysUseCase` with no `REQUIRES` feature | `link-usecase --id <UC> --feature <F>` |
| `Features without module` | `SysFeature` not `PROVIDES`-linked from a module | Re-run `create-feature --id <F> --module <MOD>` with the feature's current `--name`, `--status` and `--description` (it re-sets those fields, so read them first with `show-feature`), or add `moduleId` to the feature in the seed and re-seed with `--instance` |
| `Modules without features` | `SysModule` providing no feature | `create-feature --id <F> --name "…" --module <MOD>` |
| `ADRs without addressed standards` | `SysArchDecision` with no `ADDRESSES` edge | Re-run `create-adr --id <ADR> --addresses <STD>` with all of the ADR's current values (`--title`, `--status`, `--date`, `--decision`, `--context`, `--consequences`): it re-sets every field. Ask the user first |
| `Architecture standards with no addressing decision` | Project standard no ADR addresses | Record a decision with `create-adr --id <new ADR> --title "…" --addresses <STD>`, or dismiss it |

Stories linked straight to a feature can also be wired with `link-story --story <US> --features <F>`; that adds `REQUIRES`, not a use case, so the story stays in `Stories without UC`.

---

## Ignore file format

`.sysedge-ignore` at the repo root (committed). Each non-blank, non-comment line dismisses one finding:

```
merge: F-BIL-001 F-BIL-002   # dismissed 2026-05-22 — separate features by intent
split: F-USER-ADMIN          # kept whole intentionally
orphan: US-LEGACY-001        # retained for audit trail, will not be linked
```

- Lines beginning with `#` are comments.
- `merge: <id-A> <id-B>` suppresses that pair (either order); `split: <id>` suppresses a split candidate; `orphan: <id>` suppresses an orphan.
- The inline `# reason` is required: say why and when.

---

## Execution steps

### Step 1 — Parse arguments

Extract `--instance`, `--types` (subset of `features,use-cases,stories`), `--merge-threshold`, `--max-responsibilities` and `--ignore-file` from the user's message. If the user names an instance you don't recognise, list the instances with `python3 scripts/sys_edge.py status` and ask.

### Step 2 — Read the ignore file

```bash
cat .sysedge-ignore 2>/dev/null || echo "(no ignore file found)"
```

Parse the lines into `ignored_merges` (id pairs), `ignored_splits` and `ignored_orphans`. Warn about malformed lines and skip them. A missing file is not an error.

### Step 3 — Run the CLI analyses

All three accept `--instance <name>` (omit it for the whole graph) and `--json`:

```bash
python3 scripts/sys_edge.py analyse --merge --threshold 0.5 --json > /tmp/sysedge-merge.json
python3 scripts/sys_edge.py analyse --split --max-responsibilities 3 --json > /tmp/sysedge-split.json
python3 scripts/sys_edge.py analyse --orphans --json > /tmp/sysedge-orphans.json
```

JSON shape (each run fills its own key; the others stay empty):

```json
{
  "orphans": {"Stories without UC": [{"id": "US-007", "title": "…"}], "Use cases without features": [], "…": []},
  "merge":   [{"type": "SysFeature", "overlap": 0.83,
               "a": {"id": "F-BIL-001", "name": "…", "description": "…"},
               "b": {"id": "F-BIL-004", "name": "…", "description": "…"}}],
  "split":   [{"id": "F-USER-ADMIN", "name": "…", "uc_count": 6,
               "use_cases": [{"id": "UC-UA-001", "title": "…"}]}]
}
```

For `Use cases without features` the `title` field holds the use case's instance. If a command exits non-zero, show its stderr and continue with the others; exit code 2 means the graph database is unavailable (stop and ask the user to start it).

### Step 4 — Assess merge candidates

For each `merge` entry: read both names and descriptions, assign `semantic_similarity`, compute the score, drop pairs below `--merge-threshold` or in `ignored_merges`, apply `--types`, and sort by score.

### Step 5 — Assess split candidates

For each `split` entry not in `ignored_splits`: cluster its `use_cases` and propose sub-feature names.

### Step 6 — Filter orphans

Drop ids in `ignored_orphans`; apply `--types` (stories → `Stories without UC`, use-cases → `Use cases without features`, features → `Features without module`). Module, ADR and standard orphans are always shown.

### Step 7 — Render the report

````markdown
# Graph Analysis Report
> Instance: <inst|all>   Types: <types>   Date: YYYY-MM-DD

## Summary
| Analysis | Findings | Suppressed by ignore file |
|---|---|---|
| Merge candidates | N | N |
| Split candidates | N | N |
| Orphan nodes | N | N |

## Merge Candidates

### 1. F-BIL-001 ↔ F-BIL-004   score 0.86
**F-BIL-001** — *Issue invoices* — "Creates and sends invoices to customers…"
**F-BIL-004** — *Create and send customer invoices* — "Lets finance staff raise invoices…"
- Word overlap 0.80 · semantic similarity 0.92
- Recommendation: keep F-BIL-001 (more links), absorb F-BIL-004

```bash
python3 scripts/sys_edge.py merge-nodes --type SysFeature --keep F-BIL-001 --absorb F-BIL-004 --strategy union
```

## Split Candidates

### F-USER-ADMIN — User administration (6 UCs)
- Cluster A, *Manage user identity*: UC-UA-001, UC-UA-002, UC-UA-003
- Cluster B, *Manage user permissions*: UC-UA-004, UC-UA-005, UC-UA-006

```bash
python3 scripts/sys_edge.py create-feature --id F-UA-IDN --name "Manage user identity" --module MOD-auth
python3 scripts/sys_edge.py create-feature --id F-UA-ACC --name "Manage user permissions" --module MOD-auth
python3 scripts/sys_edge.py link-usecase --id UC-UA-004 --feature F-UA-ACC
python3 scripts/sys_edge.py unlink-usecase --id UC-UA-004 --feature F-USER-ADMIN
```

## Orphan Nodes
### Stories without UC (N)
| ID | Title | Suggested fix |
|---|---|---|
| US-007 | Customer downloads an invoice | `link-usecase --id UC-BIL-003 --story US-007` |

## Suppressed by ignore file
| Type | Nodes | Reason |
|---|---|---|
````

`--type` for `merge-nodes` is the node label from the JSON `type` field: `SysFeature`, `SysUseCase` or `SysUserStory`.

### Step 8 — Act only on confirmation

Ask which findings to act on or dismiss. Before showing a merge as ready to run, **check the instance of both nodes**:

```bash
python3 scripts/sys_edge.py show-usecase --id UC-A     # prints [instance]
python3 scripts/sys_edge.py show-story   --id US-A     # prints [instance] when set
python3 scripts/sys_edge.py show-feature --id F-A      # prints the owning Module; compare the modules' instances
```

If the instances differ (or a feature's modules belong to different instances), do not offer the merge: "Cross-instance merges are not allowed — both nodes must belong to the same instance. File an enhancement for the instance that owns them instead." If the instance cannot be determined, say so and let the user decide.

Then confirm:

```
About to run:
  python3 scripts/sys_edge.py merge-nodes --type SysFeature --keep F-BIL-001 --absorb F-BIL-004 --strategy union
This transfers every edge from F-BIL-004 to F-BIL-001 and deletes F-BIL-004.
Strategy union merges list properties (prefer-keep / prefer-absorb choose scalar values).
Confirm? [yes/no]
```

Run it only after an explicit "yes". For dismissals, append lines to `.sysedge-ignore` with today's date and the user's reason (or `<reason needed>`), and suggest re-running `/C-sysedge-analyse-graph`.

---

## Error reference

| Error | Action |
|---|---|
| One `analyse` run exits 1 | Show stderr, continue with the others |
| Exit code 2 | Graph database unavailable — ask the user to start it, stop |
| Malformed `.sysedge-ignore` line | Warn, skip the line, continue |
| User asks for a cross-instance merge | Refuse (Step 8) |
