"""
Architecture rules, checked on every run so they hold as the code grows.

  - The engine never imports the desktop app. The one exception is the
    `veltrix app` command, whose whole job is to start it.
  - The analysis never imports the report or the command line.
  - No module grows past MAX_LINES: a module that size is several modules.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
MAX_LINES = 900
ALLOWED = {("veltrix_engine/cli.py", "veltrix_app.window")}       # `veltrix app` starts the app


def imports(path: Path) -> list:
    """Every module a file imports, absolute or relative, resolved to a dotted name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = ".".join(path.relative_to(SRC).with_suffix("").parts[:-1])
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1] + ([base] if base else []))
            out.append(base)
    return out


def sources(package: str) -> list:
    return sorted(p for p in (SRC / package).rglob("*.py") if "__pycache__" not in p.parts)


def test_the_engine_never_imports_the_app():
    bad = [(str(p.relative_to(SRC)), m) for p in sources("veltrix_engine") for m in imports(p)
           if m.startswith("veltrix_app") and (str(p.relative_to(SRC)), m) not in ALLOWED]
    assert bad == [], f"engine modules importing the app: {bad}"


def test_the_analysis_never_imports_the_report_or_the_command_line():
    bad = [(str(p.relative_to(SRC)), m) for p in sources("veltrix_engine/analysis") for m in imports(p)
           if m.startswith(("veltrix_engine.report", "veltrix_engine.cli"))]
    assert bad == [], f"analysis modules importing the report or the CLI: {bad}"


@pytest.mark.parametrize("package", ["veltrix_engine", "veltrix_app"])
def test_no_module_is_too_long(package):
    long = {str(p.relative_to(SRC)): n for p in sources(package)
            if (n := len(p.read_text(encoding="utf-8").splitlines())) > MAX_LINES}
    assert long == {}, f"modules over {MAX_LINES} lines — split them: {long}"
