"""
Dependency Graph Engine

Builds and queries the project dependency graph.
Detects which artifacts depend on which, and calculates blast radius of changes.
"""

import os
import re
import json
import hashlib
from pathlib import Path
from typing import Optional
from ..core import (
    get_db, init_db, Artifact, Dependency, Change,
    row_to_artifact, row_to_change,
)


class Graph:
    """Dependency graph for project artifacts."""

    def __init__(self):
        init_db()

    # ── Artifact CRUD ──────────────────────────────────────────────────────

    def register_artifact(self, path: str, artifact_type: str = "file",
                          owner: str = "", team: str = "",
                          description: str = "", metadata: dict = None) -> Artifact:
        """Register or update an artifact."""
        db = get_db()
        artifact_id = hashlib.sha256(path.encode()).hexdigest()[:16]
        now = time.time()

        # Compute hash if file exists
        file_hash = ""
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()[:16]
            except (OSError, PermissionError):
                pass

        db.execute("""
            INSERT INTO artifacts (id, path, artifact_type, owner, team, description,
                                  last_modified, last_hash, metadata, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(path) DO UPDATE SET
                artifact_type=excluded.artifact_type,
                owner=excluded.owner,
                team=excluded.team,
                description=excluded.description,
                last_modified=excluded.last_modified,
                last_hash=excluded.last_hash,
                metadata=excluded.metadata,
                active=1
        """, (artifact_id, path, artifact_type, owner, team, description,
              now, file_hash, json.dumps(metadata or {})))
        db.commit()

        return Artifact(id=artifact_id, path=path, artifact_type=artifact_type,
                        owner=owner, team=team, description=description,
                        last_modified=now, last_hash=file_hash,
                        metadata=metadata or {})

    def get_artifact(self, path: str) -> Optional[Artifact]:
        db = get_db()
        row = db.execute("SELECT * FROM artifacts WHERE path=?", (path,)).fetchone()
        return row_to_artifact(row) if row else None

    def get_artifact_by_id(self, artifact_id: str) -> Optional[Artifact]:
        db = get_db()
        row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return row_to_artifact(row) if row else None

    def list_artifacts(self, team: str = None, artifact_type: str = None,
                       active_only: bool = True) -> list[Artifact]:
        db = get_db()
        query = "SELECT * FROM artifacts WHERE 1=1"
        params = []
        if active_only:
            query += " AND active=1"
        if team:
            query += " AND team=?"
            params.append(team)
        if artifact_type:
            query += " AND artifact_type=?"
            params.append(artifact_type)
        query += " ORDER BY path"
        rows = db.execute(query, params).fetchall()
        return [row_to_artifact(r) for r in rows]

    # ── Dependency CRUD ────────────────────────────────────────────────────

    def add_dependency(self, source_path: str, target_path: str,
                       dep_type: str = "file_dep", confidence: float = 1.0):
        """Add a dependency between two artifacts."""
        src = self.get_artifact(source_path)
        tgt = self.get_artifact(target_path)
        if not src or not tgt:
            return False

        db = get_db()
        db.execute("""
            INSERT OR IGNORE INTO dependencies (source_id, target_id, dep_type, confidence, discovered_at)
            VALUES (?, ?, ?, ?, ?)
        """, (src.id, tgt.id, dep_type, confidence, time.time()))
        db.commit()
        return True

    def get_dependents(self, path: str) -> list[dict]:
        """Get all artifacts that depend ON this artifact (downstream impact)."""
        artifact = self.get_artifact(path)
        if not artifact:
            return []

        db = get_db()
        rows = db.execute("""
            SELECT a.*, d.dep_type, d.confidence
            FROM dependencies d
            JOIN artifacts a ON a.id = d.source_id
            WHERE d.target_id = ? AND a.active = 1
            ORDER BY d.confidence DESC
        """, (artifact.id,)).fetchall()

        return [{"artifact": row_to_artifact(r), "dep_type": r["dep_type"],
                 "confidence": r["confidence"]} for r in rows]

    def get_dependencies(self, path: str) -> list[dict]:
        """Get all artifacts that this artifact depends ON (upstream)."""
        artifact = self.get_artifact(path)
        if not artifact:
            return []

        db = get_db()
        rows = db.execute("""
            SELECT a.*, d.dep_type, d.confidence
            FROM dependencies d
            JOIN artifacts a ON a.id = d.target_id
            WHERE d.source_id = ? AND a.active = 1
            ORDER BY d.confidence DESC
        """, (artifact.id,)).fetchall()

        return [{"artifact": row_to_artifact(r), "dep_type": r["dep_type"],
                 "confidence": r["confidence"]} for r in rows]

    def blast_radius(self, path: str, max_depth: int = 5) -> list[dict]:
        """Calculate the full blast radius of changing this artifact (BFS)."""
        artifact = self.get_artifact(path)
        if not artifact:
            return []

        db = get_db()
        visited = {artifact.id}
        queue = [(artifact.id, 0)]
        results = []

        while queue:
            current_id, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            rows = db.execute("""
                SELECT a.*, d.dep_type
                FROM dependencies d
                JOIN artifacts a ON a.id = d.source_id
                WHERE d.target_id = ? AND a.active = 1 AND a.id NOT IN ({})
            """.format(",".join("?" * len(visited))),
                [current_id] + list(visited)).fetchall()

            for r in rows:
                if r["id"] not in visited:
                    visited.add(r["id"])
                    results.append({
                        "artifact": row_to_artifact(r),
                        "depth": depth + 1,
                        "dep_type": r["dep_type"],
                    })
                    queue.append((r["id"], depth + 1))

        return results

    # ── Change Log ─────────────────────────────────────────────────────────

    def record_change(self, artifact_path: str, agent: str, change_type: str,
                      summary: str = "", details: dict = None) -> Optional[Change]:
        """Record a change to an artifact."""
        artifact = self.get_artifact(artifact_path)
        if not artifact:
            # Auto-register unknown artifacts
            artifact = self.register_artifact(artifact_path, owner=agent)

        change = Change(
            artifact_id=artifact.id, agent=agent, change_type=change_type,
            summary=summary, details=details or {},
        )

        db = get_db()
        db.execute("""
            INSERT INTO changes (id, artifact_id, agent, change_type, summary,
                                 details, timestamp, propagated, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (change.id, change.artifact_id, change.agent, change.change_type,
              change.summary, json.dumps(change.details), change.timestamp,
              json.dumps(change.metadata)))
        db.commit()

        return change

    def get_changes(self, artifact_path: str = None, agent: str = None,
                    since: float = 0, unpropagated_only: bool = False,
                    limit: int = 50) -> list[Change]:
        db = get_db()
        query = "SELECT * FROM changes WHERE 1=1"
        params = []

        if artifact_path:
            art = self.get_artifact(artifact_path)
            if art:
                query += " AND artifact_id = ?"
                params.append(art.id)
        if agent:
            query += " AND agent = ?"
            params.append(agent)
        if since > 0:
            query += " AND timestamp > ?"
            params.append(since)
        if unpropagated_only:
            query += " AND propagated = 0"

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()
        return [row_to_change(r) for r in rows]

    def mark_propagated(self, change_id: str):
        db = get_db()
        db.execute("UPDATE changes SET propagated=1 WHERE id=?", (change_id,))
        db.commit()

    # ── Auto-Discovery ────────────────────────────────────────────────────

    def scan_directory(self, root_path: str, team: str = "") -> dict:
        """Scan a directory and auto-register files as artifacts."""
        root = Path(root_path)
        if not root.exists():
            return {"error": f"Path not found: {root_path}"}

        registered = 0
        for f in root.rglob("*"):
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(root).parts):
                if f.suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                                ".java", ".yaml", ".yml", ".json", ".toml", ".md",
                                ".sql", ".sh", ".css", ".html", ".env", ".cfg", ".ini"):
                    rel = str(f.relative_to(root))
                    self.register_artifact(
                        path=str(f),
                        artifact_type="file",
                        team=team,
                        description=f"Auto-discovered: {rel}",
                    )
                    registered += 1

        # Auto-detect import dependencies for Python/JS
        deps_found = self._detect_import_deps(root)

        return {
            "scanned": str(root),
            "artifacts_registered": registered,
            "dependencies_found": deps_found,
        }

    def _detect_import_deps(self, root: Path) -> int:
        """Detect import/include dependencies between files."""
        count = 0
        suffix = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript"}

        for f in root.rglob("*"):
            if f.suffix not in suffix:
                continue
            try:
                content = f.read_text(errors="replace")
            except (OSError, PermissionError):
                continue

            if f.suffix == ".py":
                # import X, from X import Y
                imports = re.findall(r'^(?:import|from)\s+([\w.]+)', content, re.MULTILINE)
                for imp in imports:
                    # Try to resolve to a local file
                    parts = imp.split(".")
                    for i in range(len(parts), 0, -1):
                        candidate = root / "/".join(parts[:i])
                        for ext in ["", ".py"]:
                            target = str(candidate.with_suffix(ext) if ext else str(candidate))
                            if os.path.isfile(target):
                                if self.add_dependency(str(f.resolve()), target, "import", 0.9):
                                    count += 1
                                break
                            # Also try candidate/__init__.py
                            init_target = os.path.join(str(candidate), "__init__.py")
                            if os.path.isfile(init_target):
                                if self.add_dependency(str(f.resolve()), init_target, "import", 0.9):
                                    count += 1
                                break

            elif f.suffix in (".js", ".ts", ".tsx"):
                imports = re.findall(r'''(?:import|require)\s*\(?['"]([\w./@]+)['"]''', content)
                for imp in imports:
                    if imp.startswith("."):
                        # Relative import
                        resolved = (f.parent / imp).resolve()
                        for ext in ["", f.suffix, "/index" + f.suffix]:
                            candidate = resolved.parent / (resolved.name + ext) if ext.startswith("/") else resolved.with_suffix(ext) if ext else resolved
                            if candidate.exists():
                                if self.add_dependency(str(f.resolve()), str(candidate.resolve()), "import", 0.9):
                                    count += 1
                                break

        return count

    # ── Agent Registry ─────────────────────────────────────────────────────

    def register_agent(self, name: str, team: str = "", role: str = ""):
        db = get_db()
        db.execute("""
            INSERT INTO agents (name, team, role, last_seen, status)
            VALUES (?, ?, ?, ?, 'active')
            ON CONFLICT(name) DO UPDATE SET
                team=excluded.team, role=excluded.role,
                last_seen=excluded.last_seen, status='active'
        """, (name, team, role, time.time()))
        db.commit()

    def heartbeat(self, name: str):
        db = get_db()
        db.execute("UPDATE agents SET last_seen=?, status='active' WHERE name=?",
                   (time.time(), name))
        db.commit()

    def list_agents(self, active_only: bool = True) -> list[dict]:
        db = get_db()
        query = "SELECT * FROM agents"
        if active_only:
            cutoff = time.time() - 300  # 5 min timeout
            query += f" WHERE last_seen > {cutoff}"
        query += " ORDER BY team, name"
        rows = db.execute(query).fetchall()
        return [dict(r) for r in rows]

    # ── Subscriptions ──────────────────────────────────────────────────────

    def subscribe(self, agent: str, pattern: str, channel: str = "webhook"):
        db = get_db()
        db.execute("""
            INSERT OR REPLACE INTO subscriptions (agent, pattern, channel, active, created_at)
            VALUES (?, ?, ?, 1, ?)
        """, (agent, pattern, channel, time.time()))
        db.commit()

    def get_subscriptions(self, pattern: str = None) -> list[dict]:
        db = get_db()
        if pattern:
            rows = db.execute(
                "SELECT * FROM subscriptions WHERE active=1 AND ? LIKE REPLACE(pattern, '*', '%')",
                (pattern,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM subscriptions WHERE active=1").fetchall()
        return [dict(r) for r in rows]

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        db = get_db()
        return {
            "artifacts": db.execute("SELECT COUNT(*) FROM artifacts WHERE active=1").fetchone()[0],
            "dependencies": db.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0],
            "changes": db.execute("SELECT COUNT(*) FROM changes").fetchone()[0],
            "unpropagated": db.execute("SELECT COUNT(*) FROM changes WHERE propagated=0").fetchone()[0],
            "agents": db.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
            "active_agents": db.execute(
                "SELECT COUNT(*) FROM agents WHERE last_seen > ?",
                (time.time() - 300,)).fetchone()[0],
            "subscriptions": db.execute("SELECT COUNT(*) FROM subscriptions WHERE active=1").fetchone()[0],
        }


import time  # needed for scan_directory
