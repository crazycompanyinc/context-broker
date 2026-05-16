"""
Context Broker — Core Models & Storage

Manages the dependency graph and change log for inter-agent communication.
All state is stored in SQLite for persistence and concurrent access.
"""

import json
import time
import sqlite3
import hashlib
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

DB_PATH = Path.home() / ".context-broker" / "broker.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for concurrent access."""
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    """Initialize the database schema."""
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            artifact_type TEXT NOT NULL DEFAULT 'file',
            owner TEXT DEFAULT '',
            team TEXT DEFAULT '',
            description TEXT DEFAULT '',
            last_modified REAL NOT NULL,
            last_hash TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            dep_type TEXT NOT NULL DEFAULT 'file_dep',
            confidence REAL DEFAULT 1.0,
            discovered_at REAL NOT NULL,
            UNIQUE(source_id, target_id, dep_type),
            FOREIGN KEY(source_id) REFERENCES artifacts(id),
            FOREIGN KEY(target_id) REFERENCES artifacts(id)
        );

        CREATE TABLE IF NOT EXISTS changes (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            change_type TEXT NOT NULL,
            summary TEXT DEFAULT '',
            details TEXT DEFAULT '{}',
            timestamp REAL NOT NULL,
            propagated INTEGER DEFAULT 0,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            pattern TEXT NOT NULL,
            channel TEXT DEFAULT 'webhook',
            active INTEGER DEFAULT 1,
            created_at REAL NOT NULL,
            UNIQUE(agent, pattern)
        );

        CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            team TEXT DEFAULT '',
            role TEXT DEFAULT '',
            last_seen REAL NOT NULL,
            status TEXT DEFAULT 'active',
            metadata TEXT DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
        CREATE INDEX IF NOT EXISTS idx_artifacts_team ON artifacts(team);
        CREATE INDEX IF NOT EXISTS idx_deps_source ON dependencies(source_id);
        CREATE INDEX IF NOT EXISTS idx_deps_target ON dependencies(target_id);
        CREATE INDEX IF NOT EXISTS idx_changes_agent ON changes(agent);
        CREATE INDEX IF NOT EXISTS idx_changes_timestamp ON changes(timestamp);
        CREATE INDEX IF NOT EXISTS idx_changes_propagated ON changes(propagated);
    """)
    db.commit()
    return db


@dataclass
class Artifact:
    """A file, service, config, or any project artifact."""
    id: str = ""
    path: str = ""
    artifact_type: str = "file"  # file, service, config, schema, api, database
    owner: str = ""
    team: str = ""
    description: str = ""
    last_modified: float = 0
    last_hash: str = ""
    metadata: dict = field(default_factory=dict)
    active: bool = True

    def __post_init__(self):
        if not self.id:
            self.id = hashlib.sha256(self.path.encode()).hexdigest()[:16]
        if not self.last_modified:
            self.last_modified = time.time()


@dataclass
class Change:
    """A change made to an artifact."""
    id: str = ""
    artifact_id: str = ""
    agent: str = ""
    change_type: str = "modify"  # create, modify, delete, refactor, decision
    summary: str = ""
    details: dict = field(default_factory=dict)
    timestamp: float = 0
    propagated: bool = False
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            raw = f"{self.artifact_id}:{self.agent}:{time.time()}"
            self.id = hashlib.sha256(raw.encode()).hexdigest()[:16]
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class Dependency:
    """A dependency relationship between two artifacts."""
    source_id: str = ""
    target_id: str = ""
    dep_type: str = "file_dep"  # file_dep, api_call, data_flow, semantic
    confidence: float = 1.0
    discovered_at: float = 0

    def __post_init__(self):
        if not self.discovered_at:
            self.discovered_at = time.time()


def row_to_artifact(row) -> Artifact:
    return Artifact(
        id=row["id"], path=row["path"], artifact_type=row["artifact_type"],
        owner=row["owner"], team=row["team"], description=row["description"],
        last_modified=row["last_modified"], last_hash=row["last_hash"],
        metadata=json.loads(row["metadata"] or "{}"), active=bool(row["active"]),
    )


def row_to_change(row) -> Change:
    return Change(
        id=row["id"], artifact_id=row["artifact_id"], agent=row["agent"],
        change_type=row["change_type"], summary=row["summary"],
        details=json.loads(row["details"] or "{}"), timestamp=row["timestamp"],
        propagated=bool(row["propagated"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )
