#!/usr/bin/env python3
"""Atalho: python scripts/plot_metrics.py -i build/<lab>/metrics -o build/<lab>/plots"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qnetlab.telemetry.plot_metrics import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
