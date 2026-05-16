"""
Context Broker — Inter-Agent Communication Architecture

Real-time dependency graph and change propagation for multi-agent systems.

Usage:
    python -m src.api                  # Start server on port 7879
    from src.graph import Graph         # Use as library
    from src.detect import ChangeDetector
    from src.propagate import Propagator
"""

__version__ = "1.0.0"

from .core import init_db, get_db, Artifact, Change, Dependency
from .graph import Graph
from .detect import ChangeDetector
from .propagate import Propagator

__all__ = [
    "init_db", "get_db", "Artifact", "Change", "Dependency",
    "Graph", "ChangeDetector", "Propagator",
]
