"""Loads scripts/sim.py as a module: its name clashes with the infra `sim` package."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "sim.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("sim_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sim_cli"] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module
