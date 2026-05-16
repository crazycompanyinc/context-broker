"""
Change Detection & Conflict Detection

Watches artifacts for changes, computes diffs, and detects conflicts
when multiple agents modify the same artifacts simultaneously.
"""

import json
import time
import hashlib
import os
from pathlib import Path
from typing import Optional
from ..core import get_db, init_db, Change, row_to_change


class ChangeDetector:
    """Detects file changes and conflicts between agents."""

    def __init__(self):
        init_db()

    def compute_hash(self, filepath: str) -> str:
        """Compute SHA256 hash of a file."""
        try:
            with open(filepath, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except (OSError, PermissionError):
            return ""

    def check_modified(self, filepath: str) -> bool:
        """Check if a file has changed since last recorded hash."""
        db = get_db()
        row = db.execute(
            "SELECT last_hash FROM artifacts WHERE path=?", (filepath,)
        ).fetchone()
        if not row:
            return True  # unknown artifact = treat as changed
        current_hash = self.compute_hash(filepath)
        return current_hash != (row["last_hash"] or "")

    def scan_for_changes(self, root_path: str) -> list[dict]:
        """Scan directory and detect all modified files."""
        root = Path(root_path)
        if not root.exists():
            return []

        changes = []
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            # Skip hidden dirs
            if any(p.startswith(".") for p in f.relative_to(root).parts):
                continue

            filepath = str(f)
            if self.check_modified(filepath):
                current_hash = self.compute_hash(filepath)
                changes.append({
                    "path": filepath,
                    "hash": current_hash,
                    "modified": f.stat().st_mtime,
                })

        return changes

    # ── Conflict Detection ──────────────────────────────────────────────────

    def detect_concurrent_modifications(self, artifact_path: str,
                                         window_seconds: float = 300) -> list[dict]:
        """Detect if multiple agents modified the same artifact within a time window."""
        db = get_db()
        artifact_row = db.execute(
            "SELECT id FROM artifacts WHERE path=?", (artifact_path,)
        ).fetchone()
        if not artifact_row:
            return []

        artifact_id = artifact_row["id"]
        now = time.time()

        # Get all changes in the time window, grouped by agent
        rows = db.execute("""
            SELECT agent, COUNT(*) as change_count, MIN(timestamp) as first_change,
                   MAX(timestamp) as last_change
            FROM changes
            WHERE artifact_id = ? AND timestamp > ?
            GROUP BY agent
            ORDER BY change_count DESC
        """, (artifact_id, now - window_seconds)).fetchall()

        agents = [dict(r) for r in rows]
        if len(agents) <= 1:
            return []  # No conflict

        # Multiple agents made changes — conflict!
        return [{
            "type": "concurrent_modification",
            "artifact": artifact_path,
            "agents": agents,
            "severity": "high" if len(agents) > 2 else "medium",
            "message": f"{len(agents)} agents modified '{artifact_path}' within {window_seconds}s",
        }]

    def detect_conflicting_decisions(self, artifacts: list[str] = None) -> list[dict]:
        """Detect when agents made semantically conflicting decisions (heuristic)."""
        db = get_db()

        if artifacts:
            placeholders = ",".join("?" * len(artifacts))
            # We need artifact_ids
            rows = db.execute(
                f"SELECT id, path FROM artifacts WHERE path IN ({placeholders})",
                artifacts
            ).fetchall()
            ids = [r["id"] for r in rows]
        else:
            ids = None

        query = """
            SELECT artifact_id, agent, change_type, summary, timestamp
            FROM changes
            WHERE timestamp > ?
        """
        params = [time.time() - 3600]  # last hour

        if ids:
            placeholders = ",".join("?" * len(ids))
            query += f" AND artifact_id IN ({placeholders})"
            params.extend(ids)

        query += " ORDER BY timestamp DESC"
        rows = db.execute(query, params).fetchall()

        conflicts = []
        # Group by artifact_id
        by_artifact = {}
        for r in rows:
            aid = r["artifact_id"]
            if aid not in by_artifact:
                by_artifact[aid] = []
            by_artifact[aid].append(dict(r))

        for aid, changes in by_artifact.items():
            if len(changes) <= 1:
                continue

            # Check for contradictory change types
            has_delete = any(c["change_type"] == "delete" for c in changes)
            has_create = any(c["change_type"] == "create" for c in changes)
            has_modify = any(c["change_type"] == "modify" for c in changes)

            if has_delete and (has_create or has_modify):
                conflict = {
                    "type": "conflicting_actions",
                    "artifact_id": aid,
                    "changes": changes,
                    "severity": "high",
                    "message": f"File deleted by one agent AND modified by another",
                }
                if not artifacts:
                    # Resolve path
                    row = db.execute("SELECT path FROM artifacts WHERE id=?", (aid,)).fetchone()
                    if row:
                        conflict["artifact"] = row["path"]
                conflicts.append(conflict)

        return conflicts

    # ── Diff Analysis ───────────────────────────────────────────────────────

    def diff_files(self, old_path: str, new_path: str) -> dict:
        """Compute a simple line-level diff between two files."""
        try:
            with open(old_path) as f:
                old_lines = f.readlines()
        except OSError:
            old_lines = []

        try:
            with open(new_path) as f:
                new_lines = f.readlines()
        except OSError:
            new_lines = []

        # Simple diff: removed, added, changed lines
        old_set = set(old_lines)
        new_set = new_lines

        removed = [l.rstrip() for l in old_lines if l not in new_set]
        added = [l.rstrip() for l in new_lines if l not in old_set]

        return {
            "old_file": old_path,
            "new_file": new_path,
            "old_lines": len(old_lines),
            "new_lines": len(new_lines),
            "lines_added": len(added),
            "lines_removed": len(removed),
            "added": added[:50],  # cap at 50
            "removed": removed[:50],
        }

    def git_diff_summary(self, repo_path: str) -> dict:
        """Get git diff summary for a repo (if it's a git repo)."""
        import subprocess
        git_dir = Path(repo_path) / ".git"
        if not git_dir.exists():
            return {"error": "Not a git repository"}

        try:
            # Get changed files
            result = subprocess.run(
                ["git", "diff", "--name-status", "HEAD"],
                capture_output=True, text=True, cwd=repo_path, timeout=10
            )
            changed = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        changed.append({"status": parts[0], "file": parts[1]})

            # Get last commit info
            log = subprocess.run(
                ["git", "log", "-1", "--format=%H|%an|%s|%ar"],
                capture_output=True, text=True, cwd=repo_path, timeout=10
            )
            last_commit = None
            if log.stdout.strip():
                parts = log.stdout.strip().split("|", 3)
                if len(parts) == 4:
                    last_commit = {
                        "hash": parts[0][:8],
                        "author": parts[1],
                        "subject": parts[2],
                        "relative_time": parts[3],
                    }

            return {
                "changed_files": changed,
                "change_count": len(changed),
                "last_commit": last_commit,
            }
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"error": str(e)}

    # ── Full Scan Report ────────────────────────────────────────────────────

    def full_report(self, root_path: str) -> dict:
        """Generate a full change + conflict report for a project."""
        modified = self.scan_for_changes(root_path)
        concurrent = []
        for c in modified:
            conflicts = self.detect_concurrent_modifications(c["path"])
            concurrent.extend(conflicts)

        decision_conflicts = self.detect_conflicting_decisions()

        git_info = git_diff_summary(root_path) if (Path(root_path) / ".git").exists() else None

        return {
            "scan_time": time.time(),
            "root": root_path,
            "modified_files": modified,
            "modified_count": len(modified),
            "concurrent_modifications": concurrent,
            "decision_conflicts": decision_conflicts,
            "total_conflicts": len(concurrent) + len(decision_conflicts),
            "git": git_info,
        }


# Re-export for standalone usage
from ..graph import Graph


def detect_from_git_commits(repo_path: str, since: float = None) -> list[dict]:
    """Extract changes from git commit history."""
    import subprocess

    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return []

    try:
        cmd = ["git", "log", "--format=%H|%an|%ae|%at|%s", "--name-only"]
        if since:
            cmd.extend(["--since", str(int(since))])

        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=repo_path, timeout=15
        )

        commits = []
        current = None
        for line in result.stdout.strip().split("\n"):
            if "|" in line and not line.startswith(" "):
                if current:
                    commits.append(current)
                parts = line.split("|", 4)
                if len(parts) == 5:
                    current = {
                        "hash": parts[0][:8],
                        "author": parts[1],
                        "email": parts[2],
                        "timestamp": float(parts[3]),
                        "subject": parts[4],
                        "files": [],
                    }
            elif line.strip() and current:
                current["files"].append(line.strip())

        if current:
            commits.append(current)

        return commits
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
