"""A documentação em docs/ não pode divergir do código.

* a referência de configuração é gerada (scripts/gen_config_reference.py) e tem de estar em dia;
* todo link relativo (e âncora) entre os documentos precisa resolver;
* todo subcomando da CLI e todo alvo do Makefile aparece na referência da CLI;
* todo nome de arquivo em `src/qnetlab/...`, `templates/...` etc. citado nos documentos existe.
"""
import importlib.util
import re
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_config_reference", ROOT / "scripts" / "gen_config_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _slug(heading: str) -> str:
    """Âncora no estilo GitHub: minúsculas, sem pontuação, espaços -> '-'."""
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    text = "".join(c for c in text if unicodedata.category(c)[0] in "LN" or c in " -")
    return text.replace(" ", "-")


def _anchors(md: Path) -> set:
    out, in_code = set(), False
    for line in md.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            out.add(_slug(line.lstrip("#")))
    return out


def _md_files():
    return sorted(DOCS.glob("*.md"))


def test_docs_exist():
    names = {p.name for p in _md_files()}
    assert {"index.md", "decisions.md", "05-configuration-reference.md"} <= names


def test_every_config_field_has_a_description():
    gen = _load_generator()
    assert gen.missing_descriptions() == []


def test_configuration_reference_is_up_to_date():
    gen = _load_generator()
    current = (DOCS / "05-configuration-reference.md").read_text(encoding="utf-8")
    assert current == gen.render(), "rode: python scripts/gen_config_reference.py"


@pytest.mark.parametrize("md", _md_files(), ids=lambda p: p.name)
def test_relative_links_and_anchors_resolve(md):
    text = md.read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.S)           # ignora blocos de código
    problems = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", text):
        target = m.group(1)
        if re.match(r"[a-z]+://|mailto:", target):
            continue
        path_part, _, frag = target.partition("#")
        dest = (md.parent / path_part).resolve() if path_part else md
        if not dest.exists():
            problems.append(f"{target}: arquivo inexistente")
            continue
        if frag and dest.suffix == ".md" and frag not in _anchors(dest):
            problems.append(f"{target}: âncora inexistente em {dest.name}")
    assert not problems, "\n".join(problems)


def test_cli_commands_are_documented():
    from qnetlab.cli import build_parser

    sub = next(a for a in build_parser()._actions if a.dest == "cmd")
    text = (DOCS / "06-cli-reference.md").read_text(encoding="utf-8")
    missing = [c for c in sub.choices if f"`{c}`" not in text]
    assert not missing, f"comandos sem documentação: {missing}"


def test_makefile_targets_are_documented():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = re.search(r"^\.PHONY:\s*(.+)$", mk, flags=re.M).group(1).split()
    text = (DOCS / "06-cli-reference.md").read_text(encoding="utf-8")
    missing = [t for t in targets if t not in text]
    assert not missing, f"alvos do Makefile sem documentação: {missing}"


def test_cited_repository_paths_exist():
    pat = re.compile(r"`((?:src/qnetlab|templates|docker|schema|scripts|topologies|tests)/[A-Za-z0-9_./*-]+)`")
    missing = []
    for md in _md_files():
        for m in pat.finditer(md.read_text(encoding="utf-8")):
            p = m.group(1).rstrip("/")
            if "*" in p or "<" in p:
                continue
            if not (ROOT / p).exists():
                missing.append(f"{md.name}: {p}")
    assert not missing, "\n".join(missing)
