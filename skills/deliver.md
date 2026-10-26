---
name: {{NAME}}
description: Implement a feature or fix a defect using a scoped agent. Calls `deliver-brief` to assemble the full module context, acceptance criteria, and stored module notes from the SysEdge graph, then forks an agent to implement and verifies test links updated. Use when you have an ENH or DEF ID ready. Triggers on /{{SELF}} or "deliver ENH-..." or "fix DEF-...".
argument-hint: "--enh ENH-xxx | --def DEF-xxx"
---

# /{{SELF}}

Implement one enhancement or fix one defect. Calls `deliver-brief` to assemble the full delivery context from the SysEdge graph — including stored module notes, linked features, use cases, and known issues — then forks an agent to implement it, and verifies coverage updated.

{{>kit}}

## When to use

- You have an ENH or DEF ID from the graph and want to delegate implementation.
- You want the delivery agent to see accumulated module context (constraints, concerns, decisions) without manual copy-paste.
- You want coverage to be verified after delivery without running it manually.

## Prerequisites

- SysEdge SQLite store exists. Run `{{CLI}} status` to confirm.
- The ENH or DEF node exists in the graph.
- For ENH: linked to at least one feature via `link-enhancement`.
- For DEF: linked to at least one feature via `link-defect`.

## Procedure

### 1. Assemble the delivery brief

```bash
# For an enhancement:
{{CLI}} deliver-brief --enh ENH-xxx

# For a defect:
{{CLI}} deliver-brief --def DEF-xxx
```

This command reads from the graph and emits:
- A structured JSON block (between `---DELIVER-BRIEF-JSON-START---` and `---DELIVER-BRIEF-JSON-END---`)
- A human-readable "Instructions for the delivery agent" section with module context notes, feature scope, known issues, and acceptance criteria

Parse the JSON from the markers. The instructions section becomes the agent's prompt context.

If the brief shows no linked features, stop and link them first:
```bash
{{CLI}} link-enhancement --id ENH-xxx --feature F-xxx
# or
{{CLI}} link-defect --id DEF-xxx --feature F-xxx
```

If the brief shows no module context, consider adding notes before delivering:
```bash
{{CLI}} add-module-context --module M-xxx --kind constraint --text "always validate at the boundary"
{{CLI}} add-module-context --module M-xxx --kind concern --text "token refresh races under load"
```

### 2. Fork the delivery agent

Pass the full "Instructions for the delivery agent" section as the agent's prompt. The agent should:

1. Read the module context notes (`[constraint]`, `[concern]`, `[decision]`, `[outcome]` items) — these are standing rules that apply to all work in this module
2. Implement the change scoped to the listed features only — do not expand scope
3. Do not break any use cases listed in the scope block
4. Add or update tests to cover any missing tiers
5. Run existing tests to confirm no regression
6. Report: files changed, tests added, any failures

Do not proceed to step 3 if the fork reports test failures.

### 3. Verify coverage updated

```bash
{{CLI}} show-feature-tests --feature F-xxx
```

If all missing tiers are now covered:
```bash
{{CLI}} update-feature --id F-xxx --status Implemented
```

### 4. Close the work item

```bash
# For an enhancement:
{{CLI}} close-enhancement ENH-xxx

# For a defect:
{{CLI}} close-defect --id DEF-xxx
```

### 5. Report

Emit a single summary:

```
Delivered: ENH-xxx — <title>
  Files changed: <n>
  Tests added:   <n> (<tiers>)
  Coverage:      <before> → <after>
  Status:        <Implemented | InProgress (gaps remain)>
```

If status is still `InProgress`, list the remaining gaps and the exact `{{CLI}}` command to re-run delivery targeting the missing tier.

## Adding module context over time

Module context notes accumulate across deliveries and are included in every future `deliver-brief` for that module. Add them whenever you learn something the next delivery agent should know:

```bash
# A rule the module always follows:
{{CLI}} add-module-context --module M-xxx --kind constraint --text "..."

# A known issue or risk to watch for:
{{CLI}} add-module-context --module M-xxx --kind concern --text "..."

# An architectural decision already made:
{{CLI}} add-module-context --module M-xxx --kind decision --text "..."

# What happened in a previous delivery:
{{CLI}} add-module-context --module M-xxx --kind outcome --text "..."

# Review accumulated context:
{{CLI}} show-module-context --module M-xxx
```

## Example

```bash
/{{SELF}} --enh ENH-007
/{{SELF}} --def DEF-042
```

## Notes

- `deliver-brief` reads `EXTENDS` edges for ENH and `AFFECTS` edges for DEF to find features.
- Module context is keyed by module, not by instance — notes survive instance renames.
- The delivery agent must not modify features outside the brief's scope list.
- For a defect with no linked feature: `{{CLI}} link-defect --id DEF-xxx --feature F-xxx` first.
