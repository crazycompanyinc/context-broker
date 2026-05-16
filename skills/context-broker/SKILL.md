# Context Broker Skill

Context Broker is the inter-agent communication layer for multi-agent AI systems.
Run it as a daemon to enable all agents to share context, detect conflicts, and propagate changes.

## Start the Broker

```bash
cd ~/.hermes/context-broker
python -m src.api &
# or
context-broker server --port 7879
```

## Register Your Agent

When starting any task that modifies project files:

```bash
curl -X POST http://localhost:7879/agents/register \
  -d '{"name": "felix-cto", "team": "leadership", "role": "CTO"}'
```

## Record Changes

After making changes:

```bash
curl -X POST http://localhost:7879/changes/record \
  -d '{"artifact": "/path/to/file.py", "agent": "felix-cto", "change_type": "modify", "summary": "Changed architecture to monolith"}'
```

## Subscribe to Changes

```bash
curl -X POST http://localhost:7879/subscribe \
  -d '{"agent": "agent-frontend", "pattern": "/src/api/*", "channel": "log"}'
```

## Check for Conflicts

```bash
curl http://localhost:7879/detect/conflicts
curl http://localhost:7879/report?root=/path/to/project
```

## Blast Radius Analysis

Before making a change, check what it would affect:

```bash
curl "http://localhost:7879/graph/blast-radius?path=/src/models.py&depth=5"
```
