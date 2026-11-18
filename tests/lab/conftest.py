"""Harness dos testes de laboratório: fixtures de ``Lab`` real, restauração e registro de evidência."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent))            # permite `import labtools`

from qnetlab.errors import QNetLabError                    # noqa: E402
from qnetlab.lab import Lab                                # noqa: E402

_ENV_CACHE: Dict[str, Any] = {}
TOPOLOGY_ENV = "QNETLAB_TOPOLOGY"
WORKDIR_ENV = "QNETLAB_WORKDIR"
DISRUPTIVE_ENV = "QNETLAB_DISRUPTIVE"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "lab: exige um laboratório no ar (defina QNETLAB_TOPOLOGY)")
    config.addinivalue_line("markers", "disruptive: derruba enlaces/nós (o teardown restaura)")
    config.addinivalue_line("markers", "plan(id): ID do teste em docs/14-validation-test-plan.md")


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    here = str(Path(__file__).parent)
    enabled = bool(os.environ.get(TOPOLOGY_ENV))
    skip = pytest.mark.skip(reason=f"laboratório não configurado: defina {TOPOLOGY_ENV} (veja tests/lab/README.md)")
    markexpr = (config.getoption("-m", default="") or "").replace(" ", "")
    wants_disruptive = os.environ.get(DISRUPTIVE_ENV) == "1" or ("disruptive" in markexpr and "notdisruptive" not in markexpr)
    skip_disruptive = pytest.mark.skip(reason=f"disruptivo (derruba enlaces/nós): rode com -m disruptive ou {DISRUPTIVE_ENV}=1")
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.lab)
            if not enabled:
                item.add_marker(skip)
            elif item.get_closest_marker("disruptive") and not wants_disruptive:
                item.add_marker(skip_disruptive)             # `make test`/`pytest` puro nunca derruba o laboratório


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    out = yield
    rep = out.get_result()
    setattr(item, f"rep_{rep.when}", rep)


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def lab() -> Lab:
    topo = os.environ[TOPOLOGY_ENV]
    lab = Lab(topo, os.environ.get(WORKDIR_ENV, "build"))
    st = lab.ws.load_state()
    if not st.get("project_id") or not st.get("nodes"):
        pytest.fail(f"Laboratório '{lab.topo.name}' não está implantado em {lab.ws.root} (rode 'make up').", pytrace=False)
    try:
        lab.check_deployed()
    except QNetLabError as exc:
        pytest.fail(f"Laboratório fora do ar: {exc}", pytrace=False)
    return lab


@pytest.fixture()
def faults(lab: Lab):
    """Injetor de falhas; no teardown restaura enlaces e nós, mesmo se o teste falhar."""
    inj = lab.faults()
    yield inj
    inj.restore()


@pytest.fixture()
def capture(lab: Lab):
    return lab.capture()


class Evidence:
    """Registro de evidência de um teste (plano §10.2)."""

    def __init__(self, lab: Lab, test_id: str, nodeid: str, doc: str):
        self.lab, self.id, self.nodeid = lab, test_id, nodeid
        self.data: Dict[str, Any] = {"id": test_id, "test": nodeid, "criterion": (doc or "").strip(),
                                     "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": {}}
        self.files: Dict[str, str] = {}

    def record(self, key: str, value: Any) -> None:
        self.data["results"][key] = value

    def attach(self, path: "str | Path") -> Path:
        p = Path(path)
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        self.files[str(p)] = h.hexdigest()
        return p

    def write(self, outcome: str) -> Path:
        if "env" not in _ENV_CACHE:                       # o ambiente não muda durante a sessão
            _ENV_CACHE["env"] = self.lab.environment()
        self.data.update(outcome=outcome, finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         files=self.files, environment=_ENV_CACHE["env"])
        d = self.lab.ws.evidence_dir
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.id)
        path = d / f"{safe}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        path.write_text(json.dumps(self.data, indent=2, default=str) + "\n", encoding="utf-8")
        return path


@pytest.fixture()
def evidence(request: pytest.FixtureRequest, lab: Lab):
    marker = request.node.get_closest_marker("plan")
    ev = Evidence(lab, marker.args[0] if marker else request.node.name, request.node.nodeid,
                  request.node.function.__doc__ or "")
    yield ev
    rep: Optional[Any] = getattr(request.node, "rep_call", None)
    ev.write("skipped" if rep is None else ("passed" if rep.passed else "failed" if rep.failed else "skipped"))
