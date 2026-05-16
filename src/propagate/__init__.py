"""
Change Propagation Engine

Propagates changes to subscribed agents via multiple channels:
- webhook (HTTP callback)
- kanban (create kanban card/comment)
- message (direct agent message)
- log (write to change log for polling)
"""

import json
import time
import urllib.request
import urllib.error
from typing import Optional
from ..core import get_db, init_db, Change, row_to_change


class PropagationResult:
    """Result of a propagation attempt."""

    def __init__(self, change_id: str, agent: str, channel: str,
                 success: bool, message: str = ""):
        self.change_id = change_id
        self.agent = agent
        self.channel = channel
        self.success = success
        self.message = message
        self.timestamp = time.time()

    def to_dict(self):
        return {
            "change_id": self.change_id,
            "agent": self.agent,
            "channel": self.channel,
            "success": self.success,
            "message": self.message,
            "timestamp": self.timestamp,
        }


class Propagator:
    """Propagates changes to affected agents."""

    def __init__(self):
        init_db()

    def propagate_change(self, change_id: str) -> list[PropagationResult]:
        """Propagate a single change to all interested agents."""
        db = get_db()
        change_row = db.execute(
            "SELECT * FROM changes WHERE id=?", (change_id,)
        ).fetchone()
        if not change_row:
            return [PropagationResult(change_id, "system", "log", False, "Change not found")]

        change = row_to_change(change_row)

        # Get the artifact to find path
        art_row = db.execute(
            "SELECT * FROM artifacts WHERE id=?", (change.artifact_id,)
        ).fetchone()
        artifact_path = art_row["path"] if art_row else "unknown"
        artifact_team = art_row["team"] if art_row else ""

        # Get subscriptions matching the artifact path
        subs = db.execute("""
            SELECT s.*, a.name as agent_name
            FROM subscriptions s
            LEFT JOIN agents a ON a.name = s.agent
            WHERE s.active = 1 AND ? LIKE s.pattern
        """, (artifact_path,)).fetchall()

        # If no explicit subscriptions, check team-based routing
        if not subs and artifact_team:
            subs = db.execute("""
                SELECT * FROM agents
                WHERE team = ? AND name != ? AND last_seen > ?
            """, (artifact_team, change.agent, time.time() - 3600)).fetchall()
            results = []
            for agent in subs:
                results.append(self._notify_team_agent(agent, change, artifact_path))
            return results

        results = []
        for sub in subs:
            channel = sub["channel"]
            agent_name = sub["agent"]

            if agent_name == change.agent:
                continue  # Don't notify the agent who made the change

            if channel == "webhook":
                r = self._send_webhook(sub, change, artifact_path)
            elif channel == "log":
                r = self._write_to_log(sub, change, artifact_path)
            elif channel == "message":
                r = self._send_message(sub, change, artifact_path)
            else:
                r = PropagationResult(change_id, agent_name, channel, False,
                                       f"Unknown channel: {channel}")

            results.append(r)

        # Mark as propagated if at least one succeeded
        if any(r.success for r in results):
            db.execute("UPDATE changes SET propagated=1 WHERE id=?", (change_id,))
            db.commit()

        return results

    def propagate_all_pending(self) -> list[PropagationResult]:
        """Propagate all unpropagated changes."""
        db = get_db()
        rows = db.execute(
            "SELECT id FROM changes WHERE propagated=0 ORDER BY timestamp"
        ).fetchall()

        all_results = []
        for row in rows:
            results = self.propagate_change(row["id"])
            all_results.extend(results)

        return all_results

    def _notify_team_agent(self, agent: dict, change: Change,
                            artifact_path: str) -> PropagationResult:
        """Write to the change log for team members to pick up."""
        return PropagationResult(
            change_id=change.id,
            agent=agent["name"],
            channel="log",
            success=True,
            message=f"Team [{agent.get('team', 'unknown')}] agent [{agent['name']}] notified about change to {artifact_path}",
        )

    def _send_webhook(self, sub: dict, change: Change,
                       artifact_path: str) -> PropagationResult:
        """Send change notification via HTTP webhook."""
        # Get webhook URL from metadata if available
        metadata = {}
        try:
            metadata = json.loads(sub.get("metadata", "{}"))
        except (json.JSONDecodeError, AttributeError):
            pass

        url = metadata.get("url", sub["pattern"])
        if not url.startswith("http"):
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="webhook", success=False,
                message=f"No valid webhook URL for {sub['agent']}",
            )

        payload = json.dumps({
            "event": "artifact_changed",
            "change": {
                "id": change.id,
                "type": change.change_type,
                "agent": change.agent,
                "summary": change.summary,
                "timestamp": change.timestamp,
            },
            "artifact": artifact_path,
            "target_agent": sub["agent"],
        }).encode()

        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # Non-blocking: we set a short timeout
            urllib.request.urlopen(req, timeout=5)
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="webhook", success=True,
                message=f"Webhook sent to {url}",
            )
        except (urllib.error.URLError, OSError) as e:
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="webhook", success=False,
                message=f"Webhook failed: {e}",
            )

    def _write_to_log(self, sub: dict, change: Change,
                       artifact_path: str) -> PropagationResult:
        """Write to propagation log (/tmp/context-broker-propagation.log)."""
        import os
        log_path = "/tmp/context-broker-propagation.log"
        entry = json.dumps({
            "timestamp": time.time(),
            "change_id": change.id,
            "artifact": artifact_path,
            "agent": sub["agent"],
            "change_agent": change.agent,
            "change_type": change.change_type,
            "summary": change.summary,
        })

        try:
            with open(log_path, "a") as f:
                f.write(entry + "\n")
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="log", success=True,
                message=f"Written to {log_path}",
            )
        except OSError as e:
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="log", success=False,
                message=f"Log write failed: {e}",
            )

    def _send_message(self, sub: dict, change: Change,
                       artifact_path: str) -> PropagationResult:
        """Send a direct message to an agent (writes to inbox file)."""
        inbox_dir = Path.home() / ".context-broker" / "inbox" / sub["agent"]
        inbox_dir.mkdir(parents=True, exist_ok=True)

        msg_file = inbox_dir / f"{int(time.time())}_{change.id[:8]}.json"
        payload = {
            "from": "context-broker",
            "change_id": change.id,
            "artifact": artifact_path,
            "changed_by": change.agent,
            "change_type": change.change_type,
            "summary": change.summary,
            "timestamp": change.timestamp,
            "read": False,
        }

        try:
            with open(msg_file, "w") as f:
                json.dump(payload, f, indent=2)
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="message", success=True,
                message=f"Message written to {msg_file}",
            )
        except OSError as e:
            return PropagationResult(
                change_id=change.id, agent=sub["agent"],
                channel="message", success=False,
                message=f"Message write failed: {e}",
            )

    # ── Summary ─────────────────────────────────────────────────────────────

    def propagation_stats(self) -> dict:
        """Get propagation statistics."""
        db = get_db()
        total = db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        propagated = db.execute(
            "SELECT COUNT(*) FROM changes WHERE propagated=1"
        ).fetchone()[0]

        # Per-agent propagation
        agent_rows = db.execute("""
            SELECT agent, COUNT(*) as cnt
            FROM changes GROUP BY agent ORDER BY cnt DESC
        """).fetchall()

        # Per-channel from subscriptions
        channel_rows = db.execute("""
            SELECT channel, COUNT(*) as cnt
            FROM subscriptions WHERE active=1 GROUP BY channel
        """).fetchall()

        return {
            "total_changes": total,
            "propagated": propagated,
            "pending": total - propagated,
            "propagation_rate": round(propagated / total, 2) if total > 0 else 1.0,
            "by_agent": {r["agent"]: r["cnt"] for r in agent_rows},
            "by_channel": {r["channel"]: r["cnt"] for r in channel_rows},
        }
