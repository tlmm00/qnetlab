"""Configuração de logging."""
from __future__ import annotations

import logging
import sys


def setup_logging(verbosity: int = 0) -> None:
    level = logging.WARNING
    if verbosity == 0:
        level = logging.INFO
    elif verbosity >= 1:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
