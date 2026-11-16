from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def ring4_path() -> Path:
    return ROOT / "topologies" / "ring4.yaml"


@pytest.fixture(scope="session")
def ring6_path() -> Path:
    return ROOT / "topologies" / "ring6.yaml"
