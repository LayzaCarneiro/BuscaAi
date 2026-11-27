"""Testes offline de empacotamento, reprodutibilidade, Dockerfile e configuração de produção."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from buscai.config import is_placeholder_secret, load_settings

ROOT = Path(__file__).resolve().parents[1]


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip()
    return values


def test_pyproject_requirements_and_lockfile_alignment() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core_deps = [str(dep).casefold() for dep in pyproject["project"]["dependencies"]]
    semantic_deps = [str(dep).casefold() for dep in pyproject["project"]["optional-dependencies"]["semantic"]]

    assert any(dep.startswith("numpy") for dep in core_deps)
    assert not any(dep.startswith("tenacity") for dep in core_deps)
    assert any(dep.startswith("sentence-transformers") for dep in semantic_deps)

    req_lines = [
        line.strip().casefold()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(line.startswith("numpy") for line in req_lines)
    assert any(line.startswith("sentence-transformers") for line in req_lines)
    assert not any(line.startswith("pytest") for line in req_lines)
    assert not any(line.startswith("streamlit") for line in req_lines)
    assert not any(line.startswith("tenacity") for line in req_lines)

    lock_text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lock_text
    assert re.search(r"(?m)^sentence-transformers==", lock_text)
    assert re.search(r"(?m)^torch==", lock_text)
    assert re.search(r"(?m)^numpy==", lock_text)
    assert re.search(r"(?m)^fastapi==", lock_text)
    assert not re.search(r"(?m)^pytest==", lock_text)
    assert not re.search(r"(?m)^streamlit==", lock_text)


def test_dockerfile_and_dockerignore_security_and_runtime_controls() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.13-slim" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "requirements.lock" in dockerfile
    assert "scripts/preload_embedding_model.py" in dockerfile
    assert "BUSCAAI_EMBEDDING_PRELOAD=true" in dockerfile
    assert "BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY=true" in dockerfile
    assert "USER appuser" in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert ".env" not in dockerfile
    assert "GEMINI_API_KEY" not in dockerfile
    assert "EBAY_CLIENT_SECRET" not in dockerfile
    assert "BUSCAAI_API_KEYS" not in dockerfile

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for ignored in (".env", ".venv", ".tmp_*", "*.safetensors", "*.pt", "*.onnx", "src/buscai/app", "tests/"):
        assert ignored in dockerignore

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "!.env.production.example", ".tmp_*", "*.safetensors", "*.pt", "*.onnx"):
        assert pattern in gitignore


def test_production_env_example_uses_safe_placeholders_and_runtime_defaults() -> None:
    prod_env = _parse_env_file(ROOT / ".env.production.example")
    assert prod_env["BUSCAAI_EMBEDDING_PRELOAD"] == "true"
    assert prod_env["BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY"] == "true"
    assert prod_env["BUSCAAI_EMBEDDING_CACHE_DIR"] == "/opt/buscai/model_cache"
    assert prod_env["BUSCAAI_MAX_REQUEST_BODY_BYTES"] == "65536"
    assert prod_env["BUSCAAI_RATE_LIMIT_PER_MINUTE"] == "30"
    assert prod_env["BUSCAAI_RATE_LIMIT_WINDOW_SECONDS"] == "60"

    for secret_key in ("GEMINI_API_KEY", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "BUSCAAI_API_KEYS"):
        assert is_placeholder_secret(prod_env[secret_key]) is True

    settings = load_settings(environ=prod_env)
    assert settings.is_gemini_configured is False
    assert settings.is_ebay_configured is False
    assert settings.is_api_auth_configured is False
    assert settings.embedding_preload is True
    assert settings.embedding_local_files_only is True
    assert settings.embedding_cache_dir == "/opt/buscai/model_cache"


def test_package_data_includes_catalog_profiles_and_runtime_never_imports_buscai_app() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert "catalog_profiles.json" in package_data["buscai"]

    for required_rel in (
        "pyproject.toml",
        "README.md",
        "requirements.lock",
        "scripts/preload_embedding_model.py",
        "src/buscai/catalog_profiles.json",
        "src/buscai/api/app.py",
    ):
        assert (ROOT / required_rel).is_file(), f"Arquivo ausente: {required_rel}"

    dockerignore_lines = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "src/buscai/app" in dockerignore_lines
    assert "src/buscai/catalog_profiles.json" not in dockerignore_lines

    runtime_py_files = [
        *[
            p
            for p in (ROOT / "src" / "buscai").rglob("*.py")
            if "app" not in p.relative_to(ROOT / "src" / "buscai").parts
        ],
        *(ROOT / "src" / "userintent").rglob("*.py"),
        ROOT / "scripts" / "preload_embedding_model.py",
    ]
    for py_file in runtime_py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "buscai.app"
                    assert not alias.name.startswith("buscai.app.")
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module != "buscai.app"
                assert not node.module.startswith("buscai.app.")

