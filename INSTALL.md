# Installing SysEdge 1.4.0

## Quick install (SQLite — no infrastructure required)

**Prerequisites:** Python 3.10+

```bash
git clone https://github.com/hudhomer-cyber/SysEdge-Public
cd SysEdge-Public
pip install -r requirements.txt
python3 scripts/sys_edge.py init
python3 scripts/sys_edge.py status
```

SysEdge creates `data/sysedge.sqlite3` in your project root. No Docker, no Neo4j, no licence key required.

---

## Configuration

`sysedge.yaml` in your project root (create it or copy from this repo):

```yaml
model: full           # full V-model: stories → UCs → features → tests
instances: [myapp]    # one entry per deployable unit / domain
backend: sqlite       # sqlite (default) or neo4j
```

---

## Using with Neo4j (optional)

If your team already runs Neo4j and wants a shared central graph, set environment variables:

```bash
export SYSEDGE_NEO4J_URI=bolt://localhost:7687
export SYSEDGE_NEO4J_PASSWORD=yourpassword
export SYSEDGE_NEO4J_USER=neo4j          # default
```

Or add `backend: neo4j` to `sysedge.yaml`. Then:

```bash
python3 scripts/sys_edge.py init
python3 scripts/sys_edge.py status
```

For a new Neo4j instance with Docker:

```bash
docker run -d --name sysedge-neo4j \
  -e NEO4J_AUTH=neo4j/yourpassword \
  -p 127.0.0.1:7687:7687 \
  -v sysedge-neo4j-data:/data \
  --restart unless-stopped \
  neo4j:5-community
```

---

## Migrating from Neo4j to SQLite

**Via JSON backup (recommended):**

```bash
# 1. Export from Neo4j
SYSEDGE_BACKEND=neo4j python3 scripts/sys_edge.py backup --file graph.json

# 2. Switch to SQLite in sysedge.yaml (backend: sqlite)
# 3. Import into SQLite
python3 scripts/sys_edge.py seed graph.json
```

**Via Cypher script (for Neo4j tooling):**

```bash
python3 scripts/sys_edge.py export-neo4j --output graph.cypher
# Then: cypher-shell -u neo4j -p <password> -f graph.cypher
```

---

## Install skills for your AI agent

**Claude Code:**

```bash
for skill in skills/claude/*; do
  mkdir -p ".claude/skills/$(basename "$skill")"
  cp "$skill/SKILL.md" ".claude/skills/$(basename "$skill")/"
done
```

**Kimi Code:**

```bash
for skill in skills/kimi/*; do
  mkdir -p ".kimi/skills/$(basename "$skill")"
  cp "$skill/SKILL.md" ".kimi/skills/$(basename "$skill")/"
done
```

---

## Load architecture standards (optional)

```bash
python3 scripts/sys_edge.py seed-standards examples/arch-standards.yaml
```

Loads 43 industry standards across Security, Development, Execution, Operations and Infrastructure, plus 28 quality dimensions used by `coverage-review` and `audit-test`.

---

## Multiple repositories sharing one graph

Test nodes store repo-relative paths and a `repo` tag (from git `origin`, or set `SYSEDGE_REPO`). Several repos can share one SQLite file or Neo4j instance — tests tagged with another repo are excluded from this repo's exports and `check-test-links`.

---

## Uninstall

```bash
# Remove the SQLite database
rm -rf data/sysedge.sqlite3

# Remove installed skills
rm -rf .claude/skills/C-sysedge-* .kimi/skills/K-sysedge-*
```

For Neo4j: `docker rm -f sysedge-neo4j && docker volume rm sysedge-neo4j-data`
