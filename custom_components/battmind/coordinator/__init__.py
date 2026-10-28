"""Event loop side of BattMind.

The coordinator only does light work on the event loop: it collects a plain
:class:`Snapshot` of Home Assistant data, sends the calculation to the BattMind worker
thread and publishes the result. All heavy work (planning, history, predictions,
markdown) runs in the worker.
"""
from .coordinator import BattMindCoordinator

__all__ = ["BattMindCoordinator"]
