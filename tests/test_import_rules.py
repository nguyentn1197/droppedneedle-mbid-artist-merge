"""CLAUDE.md hard rules on imports, checked statically."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = [REPO_ROOT / "plugin.py", *sorted((REPO_ROOT / "mbid_artist_merge").glob("*.py"))]
HOST_PREFIXES = ("core", "infrastructure")
LAZY_HOST_ALLOWED = {"plugin.py", "state.py"}


def imports(path: Path) -> list[tuple[str, bool]]:
    """(absolute module name, is_module_level) for every import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = {id(node) for node in tree.body}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, id(node) in top) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, id(node) in top))
    return found


def root(module: str) -> str:
    return module.split(".", 1)[0]


def test_runtime_is_stdlib_only() -> None:
    allowed = set(sys.stdlib_module_names) | {"mbid_artist_merge", *HOST_PREFIXES}
    for path in RUNTIME:
        for module, _ in imports(path):
            assert root(module) in allowed, f"{path.name} imports {module}"
            assert root(module) != "httpx", f"{path.name} imports httpx"


def test_host_imports_are_lazy_and_confined() -> None:
    for path in RUNTIME:
        for module, module_level in imports(path):
            if root(module) in HOST_PREFIXES:
                assert path.name in LAZY_HOST_ALLOWED, f"{path.name} imports {module}"
                assert not module_level, f"{path.name} imports {module} at module level"


def test_tests_never_import_host_modules() -> None:
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        for module, _ in imports(path):
            assert root(module) not in HOST_PREFIXES, f"{path.name} imports {module}"
