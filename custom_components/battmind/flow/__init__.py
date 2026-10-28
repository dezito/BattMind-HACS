"""Config flow for BattMind.

Everything from <prefix>_config.yaml is configured here, split into small pages with
help texts. The settings that the dashboard changes at runtime (planner switches,
thresholds, ...) are entities.
"""
from .entry import BattMindConfigFlow
from .options import BattMindOptionsFlow

__all__ = [
    "BattMindConfigFlow",
    "BattMindOptionsFlow",
]
