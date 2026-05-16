# Context Broker v1.0.0

**Inter-Agent Communication Architecture**

Real-time dependency graph and change propagation for multi-agent AI systems.

## The Problem

When multiple AI agents work on the same project simultaneously, each operates
in isolation. If the CTO agent decides to switch from microservices to monolith,
the backend-eng agent doesn't know. If the designer changes the color scheme,
the frontend-eng doesn't know. Today this is solved with manual kanban updates
or manual messages — if at all.

## The Solution

Context Broker is an autonomous agent that:

1. **Reads** decisions, changes, and artifacts produced by each agent (files, commits, kanban messages)
2. **Detects impact** — if a change in one file affects other teams, it knows
3. **Propagates automatically** — notifies affected agents (via webhook, kanban, or direct message)
4. **Maintains a dependency graph** — which files depend on which, which teams touch which parts
5. **Detects conflicts before they happen** — if two agents are touching the same file with incompatible changes, warns BEFORE the merge

It's a "communication architect for agents." Nothing like this exists as a product.

## Quick Start

```bash
# Install
pip install -e .

# Scan a project
context-broker scan /path/to/project --team backend

# Get full conflict report
context-broker report /path/to/project

# Start API server
context-broker server --port 7879

# Record a change
context-broker record --agent my-agent --artifact src/config.py --type modify --summary "Updated DB config"

# Propagate pending changes
context-broker propagate

# Check status
context-broker status
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/stats` | Broker statistics |
| POST | `/agents/register` | Register an agent |
| GET | `/agents` | List agents |
| GET | `/artifacts` | List artifacts |
| POST | `/artifacts/register` | Register an artifact |
| POST | `/changes/record` | Record a change |
| GET | `/changes` | Query changes |
| POST | `/changes/propagate-all` | Propagate all pending |
| POST | `/scan` | Scan a directory |
| GET | `/graph/blast-radius?path=X` | Get blast radius |
| GET | `/graph/dependents?path=X` | Get dependents |
| GET | `/detect/conflicts` | Detect conflicts |
| GET | `/report?root=X` | Full project report |
| POST | `/subscribe` | Subscribe to changes |

## Architecture

```
src/
├── core/       — SQLite storage, models (Artifact, Change, Dependency)
├── graph/      — Dependency graph, blast radius, auto-discovery
├── detect/     — Change detection, conflict detection, git diff
├── propagate/  — Multi-channel change propagation (webhook/log/message)
└── api/        — REST API server (stdlib http.server)
```

## Running Tests

```bash
python tests/test_broker.py
```

## Dependencies

Zero external dependencies. Uses only Python stdlib (sqlite3, http.server, json, etc.).
