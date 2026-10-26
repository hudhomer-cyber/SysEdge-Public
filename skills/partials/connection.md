**Backend:** SysEdge defaults to SQLite (`data/sysedge.sqlite3` in the project root). To use Neo4j instead, set `backend: neo4j` in `sysedge.yaml` and provide `SYSEDGE_NEO4J_URI`, `SYSEDGE_NEO4J_USER` and `SYSEDGE_NEO4J_PASSWORD`. `SYSEDGE_PROJECT_ROOT` overrides the project root and `SYSEDGE_REPO` the repository id stamped on test paths.

