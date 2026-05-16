"""Context Broker Tests"""

import os
import sys
import time
import json
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core import init_db, get_db, Artifact, Change, Dependency
from src.graph import Graph
from src.detect import ChangeDetector
from src.propagate import Propagator


# Override DB for testing
TEST_DB = "/tmp/context-broker-test.db"

def setup_test_db():
    """Use a clean test database."""
    from src.core import DB_PATH
    # Point to test DB
    import src.core as core
    core.DB_PATH = core.Path(TEST_DB)
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db()
    return core


def test_core_models():
    """Test dataclasses and basic DB operations."""
    print("TEST: core models... ", end="")
    setup_test_db()

    db = get_db()

    # Insert artifact
    art = Artifact(path="/project/src/app.py", owner="agent-1", team="backend")
    db.execute(
        "INSERT INTO artifacts (id, path, artifact_type, owner, team, description, last_modified, metadata, active) VALUES (?,?,?,?,?,?,?,?,1)",
        (art.id, art.path, "file", art.owner, art.team, "", art.last_modified, "{}")
    )
    db.commit()

    # Query back
    row = db.execute("SELECT * FROM artifacts WHERE path=?", (art.path,)).fetchone()
    assert row is not None
    assert row["owner"] == "agent-1"
    assert row["team"] == "backend"

    # Insert change
    ch = Change(artifact_id=art.id, agent="agent-1", change_type="create",
                summary="Initial commit")
    db.execute(
        "INSERT INTO changes (id, artifact_id, agent, change_type, summary, details, timestamp, propagated, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
        (ch.id, ch.artifact_id, ch.agent, ch.change_type, ch.summary, "{}", ch.timestamp, 0, "{}")
    )
    db.commit()

    row = db.execute("SELECT COUNT(*) FROM changes").fetchone()
    assert row[0] == 1

    print("PASS")
    return True


def test_graph_scan():
    """Test directory scanning and artifact registration."""
    print("TEST: graph scan... ", end="")
    setup_test_db()
    g = Graph()

    # Create temp project
    tmpdir = tempfile.mkdtemp()
    try:
        # Create some files
        (tmpdir_path := type("P", (), {"__truediv__": lambda s, x: os.path.join(tmpdir, x), "__str__": lambda s: tmpdir})())
        pathlib = type("p", (), {"Path": lambda x: type("N", (), {
            "exists": lambda: os.path.exists(x),
            "mkdir": lambda **kw: os.makedirs(x, exist_ok=True),
            "relative_to": lambda other: type("R", (), {
                "parts": x.replace(str(other), "").strip("/").split("/"),
            })(),
            "rglob": lambda pat: [type("F", (), {
                "is_file": lambda: True,
                "suffix": ".py",
                "relative_to": lambda o: type("RR", (), {"parts": []})(),
                "resolve": lambda: x,
                "read_text": lambda **kw: "import os\nimport sys\n",
                "stat": lambda: type("S", (), {"st_mtime": 1234567890})(),
                "name": x.split("/")[-1],
                "parent": type("Pr", (), {"__truediv__": lambda s, n: x.rsplit("/", 1)[0] + "/" + n})(),
                "with_suffix": lambda s: x.rsplit(".", 1)[0] + s,
                "__str__": lambda: x,
            })()],
        })()})
        # Actually, let's just use real files
        os.makedirs(os.path.join(tmpdir, "src"), exist_ok=True)
        with open(os.path.join(tmpdir, "src", "app.py"), "w") as f:
            f.write("import os\nimport sys\ndef main(): pass\n")
        with open(os.path.join(tmpdir, "src", "utils.py"), "w") as f:
            f.write("def helper(): return True\n")
        with open(os.path.join(tmpdir, "src", "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(tmpdir, "README.md"), "w") as f:
            f.write("# Project\n")

        result = g.scan_directory(tmpdir, team="backend")
        assert "artifacts_registered" in result
        assert result["artifacts_registered"] >= 3  # at least app.py, utils.py, README.md

        print(f"PASS ({result['artifacts_registered']} artifacts)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_dependency_tracking():
    """Test dependency CRUD and blast radius."""
    print("TEST: dependency tracking... ", end="")
    setup_test_db()
    g = Graph()

    # Register artifacts
    a1 = g.register_artifact("/proj/src/models.py", owner="agent-1", team="backend")
    a2 = g.register_artifact("/proj/src/api.py", owner="agent-2", team="backend")
    a3 = g.register_artifact("/proj/src/views.py", owner="agent-3", team="frontend")
    a4 = g.register_artifact("/proj/tests/test_api.py", owner="agent-4", team="qa")

    # Add dependencies: api -> models, views -> api, test_api -> api
    g.add_dependency("/proj/src/api.py", "/proj/src/models.py", "import", 0.95)
    g.add_dependency("/proj/src/views.py", "/proj/src/api.py", "import", 0.95)
    g.add_dependency("/proj/tests/test_api.py", "/proj/src/api.py", "import", 0.90)

    # Dependents of models.py (what depends ON it)
    deps = g.get_dependents("/proj/src/models.py")
    dep_paths = [d["artifact"].path for d in deps]
    assert "/proj/src/api.py" in dep_paths, f"Expected api.py in dependents, got {dep_paths}"

    # Blast radius of models.py
    radius = g.blast_radius("/proj/src/models.py")
    radius_paths = [r["artifact"].path for r in radius]
    assert "/proj/src/api.py" in radius_paths
    assert "/proj/src/views.py" in radius_paths  # views -> api -> models

    print(f"PASS (blast radius of models.py = {len(radius)} artifacts)")
    return True


def test_change_recording():
    """Test change recording and querying."""
    print("TEST: change recording... ", end="")
    setup_test_db()
    g = Graph()

    g.register_change = g.record_change  # alias

    a = g.register_artifact("/proj/config.yaml", owner="agent-1", team="devops")

    # Record changes
    c1 = g.record_change("/proj/config.yaml", "agent-1", "create", "Added config")
    c2 = g.record_change("/proj/config.yaml", "agent-2", "modify", "Updated DB host")
    c3 = g.record_change("/proj/config.yaml", "agent-3", "modify", "Changed port")

    assert c1 is not None
    assert c2 is not None

    # Query changes
    changes = g.get_changes(artifact_path="/proj/config.yaml")
    assert len(changes) >= 3

    print(f"PASS ({len(changes)} changes recorded)")
    return True


def test_conflict_detection():
    """Test concurrent modification detection."""
    print("TEST: conflict detection... ", end="")
    setup_test_db()
    g = Graph()

    g.register_artifact("/proj/schema.sql", owner="agent-1", team="dba")
    g.record_change("/proj/schema.sql", "agent-1", "modify", "Added users table")
    g.record_change("/proj/schema.sql", "agent-2", "modify", "Added indexes")
    g.record_change("/proj/schema.sql", "agent-3", "modify", "Changed column types")

    detector = ChangeDetector()

    conflicts = detector.detect_concurrent_modifications("/proj/schema.sql", window_seconds=3600)
    assert len(conflicts) > 0, f"Expected conflicts, got {conflicts}"
    assert conflicts[0]["type"] == "concurrent_modification"
    assert len(conflicts[0]["agents"]) >= 3

    print(f"PASS ({len(conflicts[0]['agents'])} agents conflicted)")
    return True


def test_propagation():
    """Test change propagation."""
    print("TEST: propagation... ", end="")
    setup_test_db()
    g = Graph()
    p = Propagator()

    # Register agent and subscription
    g.register_agent("agent-backend", team="backend", role="developer")
    g.register_agent("agent-frontend", team="frontend", role="developer")
    g.subscribe("agent-frontend", "/proj/src/api.py", "log")

    # Record a change
    g.register_artifact("/proj/src/api.py", owner="agent-backend", team="backend")
    change = g.record_change("/proj/src/api.py", "agent-backend", "modify",
                              "Changed API response format")

    # Propagate
    results = p.propagate_change(change.id)
    log_results = [r for r in results if r.channel == "log"]
    assert len(log_results) > 0, f"Expected log propagation, got {results}"

    print(f"PASS ({len(results)} notification(s))")
    return True


def test_subscription_matching():
    """Test pattern-based subscription matching."""
    print("TEST: subscription matching... ", end="")
    setup_test_db()
    g = Graph()

    g.subscribe("agent-1", "/proj/config/*", "webhook")
    g.subscribe("agent-2", "/proj/src/*", "log")
    g.subscribe("agent-3", "/proj/*", "message")

    subs = g.get_subscriptions("/proj/src/api.py")
    assert len(subs) == 2  # agent-2 and agent-3

    print(f"PASS ({len(subs)} matching subscriptions)")
    return True


def test_agent_registry():
    """Test agent registration and heartbeat."""
    print("TEST: agent registry... ", end="")
    setup_test_db()
    g = Graph()

    g.register_agent("cto", team="leadership", role="CTO")
    g.register_agent("backend-eng", team="backend", role="developer")
    g.register_agent("frontend-eng", team="frontend", role="developer")
    g.heartbeat("cto")
    g.heartbeat("backend-eng")

    agents = g.list_agents()
    names = [a["name"] for a in agents]
    assert "cto" in names
    assert "backend-eng" in names
    assert "frontend-eng" in names

    print(f"PASS ({len(agents)} agents)")
    return True


def test_stats():
    """Test statistics endpoint."""
    print("TEST: stats... ", end="")
    setup_test_db()
    g = Graph()

    g.register_agent("agent-1")
    g.register_artifact("/proj/file1.py", team="backend")
    g.register_artifact("/proj/file2.py", team="backend")
    g.record_change("/proj/file1.py", "agent-1", "create", "test")

    stats = g.stats()
    assert stats["artifacts"] == 2
    assert stats["agents"] >= 1
    assert stats["changes"] >= 1

    print(f"PASS (artifacts={stats['artifacts']}, changes={stats['changes']})")
    return True


def run_all():
    tests = [
        test_core_models,
        test_graph_scan,
        test_dependency_tracking,
        test_change_recording,
        test_conflict_detection,
        test_propagation,
        test_subscription_matching,
        test_agent_registry,
        test_stats,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
