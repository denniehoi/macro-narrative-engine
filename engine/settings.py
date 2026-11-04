from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_settings(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "settings.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_benchmarks(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "benchmarks.yaml"
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["instruments"] = {str(k): v for k, v in data["instruments"].items()}
    return data
