---
name: K-sysedge-init
description: Bootstrap SysEdge on a project — scan the working directory, propose systems, domains, modules (with code and test paths) and features, write a seed JSON, and load it into an empty graph. Use once when adding SysEdge to a new repository, or when the user asks to initialise, bootstrap or seed the SysEdge graph without writing JSON by hand. Triggers on /K-sysedge-init.
---

# /K-sysedge-init

Scan the current repository, detect its structure, and generate a SysEdge seed file. You (Kimi) do the analysis in this session; nothing is sent to another service.

---

## What it produces

`data/sys-init-YYYY-MM-DD.json` with:
- one `SysSystem` and a few `SysDomain` nodes (one per layer: backend, frontend, infrastructure, …)
- `SysModule` nodes with `instance`, `codePaths` and `testPaths`
- `SysFeature` nodes (status `Implemented` for what already exists)
- optional `SysDirCategory` mappings for test-tier classification

**Connection settings** are read from the environment or from `.env` in the project root: `SYSEDGE_NEO4J_URI` (default `bolt://localhost:7688`), `SYSEDGE_NEO4J_USER` and `SYSEDGE_NEO4J_PASSWORD` (required). The legacy `NEO4J_*` names still work but print a deprecation notice; `NEO4J_AUTH` is never read. `SYSEDGE_PROJECT_ROOT` overrides the project root and `SYSEDGE_REPO` the repository id stamped on test paths.


---

## Execution steps

Run these in order and read each result before the next step.

### Step 1 — Check the graph

```bash
python3 cli/sys_edge.py init      # constraints, indexes and schema version — safe to re-run
python3 cli/sys_edge.py status    # shows existing instances; an empty graph has none
```

Exit code 2 means the graph database is unreachable or the password is missing: stop and ask the user to start Neo4j and set `SYSEDGE_NEO4J_PASSWORD`.

If `status` already lists instances, the graph is not empty: tell the user, and in Step 6 seed only the new instance(s) with `--instance`.

### Step 2 — Detect project type and top-level layout

```bash
find . -maxdepth 3 \( -name go.mod -o -name package.json -o -name pyproject.toml \
       -o -name Cargo.toml -o -name pom.xml -o -name "*.csproj" \) \
       -not -path "*/node_modules/*" -not -path "./.git/*" | sort
ls -la
```

### Step 3 — Map source packages

List the directories that hold source files, two or three levels deep, skipping vendored and build output:

```bash
find . -type f \( -name "*.go" -o -name "*.ts" -o -name "*.tsx" -o -name "*.py" \
       -o -name "*.java" -o -name "*.cs" -o -name "*.rs" \) \
       -not -path "*/node_modules/*" -not -path "*/dist/*" -not -path "*/build/*" \
       -not -path "*/vendor/*" -not -path "./.git/*" -not -name "*_test.go" \
  | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn | head -60
```

For each candidate module directory, skim one or two entry files (handlers, routes, services, page components) to understand what it does.

### Step 4 — Map tests

```bash
find . -type d \( -name "test" -o -name "tests" -o -name "__tests__" -o -name "e2e" -o -name "spec" \) \
       -not -path "*/node_modules/*" -not -path "./.git/*" | sort
find . -name "*_test.go" -not -path "*/vendor/*" | wc -l
find . \( -name "test_*.py" -o -name "*_test.py" \) | wc -l
find . \( -name "*.spec.ts" -o -name "*.test.ts" -o -name "*.spec.tsx" -o -name "*.test.tsx" \) -not -path "*/node_modules/*" | wc -l
```

### Step 5 — Generate the seed JSON

```json
{
  "systems": [{"id": "SYS-001", "name": "<project name from go.mod / package.json / directory>"}],
  "domains": [
    {"id": "DOM-backend",  "name": "Backend",  "systemId": "SYS-001"},
    {"id": "DOM-frontend", "name": "Frontend", "systemId": "SYS-001"}
  ],
  "modules": [
    {
      "id": "MOD-billing",
      "name": "Billing",
      "instance": "billing",
      "domainId": "DOM-backend",
      "description": "Invoices, payments and refunds",
      "type": "service",
      "codePaths": ["src/billing/"],
      "testPaths": ["tests/billing/"]
    }
  ],
  "features": [
    {"id": "F-BIL-001", "name": "Issue invoices", "moduleId": "MOD-billing",
     "status": "Implemented", "surfaceType": "api",
     "description": "Create an invoice for a completed order and send it to the customer"}
  ],
  "dirCategories": [
    {"id": "DIRCAT-unit", "dirPrefix": "src/", "filePattern": "_test.go",
     "testCategory": "component", "description": "Go unit tests next to the code"},
    {"id": "DIRCAT-integration", "dirPrefix": "tests/", "filePattern": "test_",
     "testCategory": "integration", "description": "Python integration tests"}
  ],
  "userStories": [], "useCases": [], "testPackages": [], "tests": [], "symbols": [], "endpoints": []
}
```

Rules:
- **Module** id `MOD-<area>`; 5–15 modules for a typical project, never one per file. `codePaths` and `testPaths` are repo-relative directory prefixes; `scan-code`, `scan-tests` and the coverage views use them.
- **Instance**: the session role that owns the module, named after the domain (`billing`, `auth`, `dashboard`), not the technology layer. One instance can own several modules.
- **Feature** id `F-<SHORT>-NNN`; 3–10 user-visible capabilities per module. `status` is one of `Proposed`, `InProgress`, `Implemented`, `Deprecated`, `Superseded` — use `Implemented` for existing code. `surfaceType` (optional: `ui`, `api`, `middleware`, `protocol`, `config`, `script`) controls which test tiers coverage reports expect.
- Leave stories, use cases, tests, symbols and endpoints empty: they come from imports and scans.

Show the proposed modules and features to the user as a table and adjust before writing.

### Step 6 — Write, seed and verify

Write the JSON to `data/sys-init-<date>.json`, then:

```bash
python3 cli/sys_edge.py seed data/sys-init-<date>.json --dry-run     # counts only, nothing written
python3 cli/sys_edge.py seed data/sys-init-<date>.json               # allowed unfiltered only while the graph is empty
```

If the graph was not empty, seed each new instance instead: `python3 cli/sys_edge.py seed data/sys-init-<date>.json --instance billing`. Never use `--full-restore` here: it replaces every SysEdge node.

```bash
python3 cli/sys_edge.py scan-code --all --yes
python3 cli/sys_edge.py scan-tests --all --yes
python3 cli/sys_edge.py scan-go-tests                  # only if the project has Go tests
python3 cli/sys_edge.py briefing --instance <first-instance>
```


```bash
python3 cli/sys_edge.py seed-standards
```

Show the briefing to the user so they can check the structure, and take a first backup: `python3 cli/sys_edge.py backup`.

---

## Tips

- A rough seed in five minutes beats a perfect one in a day; the graph is additive.
- Symbols found by `scan-code` are linked to modules, not features: link the ones that implement a feature with `link-symbol` / `link-endpoint` as you work.
- Next: `/K-sysedge-feature-graph` for the day-to-day workflow; with the Kit, `/K-sysedge-import-stories` and `/K-sysedge-import-use-cases` load requirements documents.
