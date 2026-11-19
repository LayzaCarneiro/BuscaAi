"""Testes de fronteiras arquiteturais: remoção do Mercado Livre e isolamento do DummyJSON."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def test_mercadolivre_files_are_removed() -> None:
    removed_paths = [
        ROOT / "src" / "buscai" / "catalog" / "mercadolivre.py",
        ROOT / "scripts" / "check_mercadolivre_catalog.py",
        ROOT / "tests" / "test_mercadolivre.py",
    ]
    for path in removed_paths:
        assert not path.exists(), f"Arquivo legado do Mercado Livre ainda existe: {path}"


def test_no_active_code_or_config_references_mercadolivre() -> None:
    scan_roots = [
        ROOT / "src",
        ROOT / "scripts",
        ROOT / "tests",
        ROOT / ".env.example",
        ROOT / "pyproject.toml",
        ROOT / "requirements.txt",
    ]
    self_path = Path(__file__).resolve()

    offenders: list[str] = []
    for target in scan_roots:
        files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
        for path in files:
            if path.resolve() == self_path:
                continue
            if path.suffix not in {".py", ".toml", ".txt", ".example", ""}:
                continue
            content = path.read_text(encoding="utf-8")
            if "mercadolivre" in content.casefold():
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], f"Referências ao Mercado Livre encontradas em: {offenders}"


def test_production_src_modules_never_import_dummyjson() -> None:
    dummyjson_module = (SRC / "buscai" / "catalog" / "dummyjson.py").resolve()
    streamlit_dir = (SRC / "buscai" / "app").resolve()

    offenders: list[str] = []
    for py_file in SRC.rglob("*.py"):
        resolved = py_file.resolve()
        if resolved == dummyjson_module or _is_within(resolved, streamlit_dir):
            continue

        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "dummyjson" in alias.name.casefold():
                        offenders.append(f"{py_file.relative_to(ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported = ", ".join(alias.name for alias in node.names)
                if "dummyjson" in module.casefold() or any(
                    "dummyjson" in alias.name.casefold() for alias in node.names
                ):
                    offenders.append(f"{py_file.relative_to(ROOT)}: from {module} import {imported}")

    assert offenders == [], f"Módulos de produção importando DummyJSON: {offenders}"
