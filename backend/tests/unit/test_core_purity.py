"""The deterministic matching core must stay sealed. See ADR-0004.

Quorum's central claim is that matching is reproducible and auditable: the same inputs
give the same result forever, with no API key and no network. That claim survives only
as long as nothing in app/services/matching/ reaches for the AI layer or an HTTP client.

Good intentions do not enforce this. This test does.
"""

from __future__ import annotations

import ast
from pathlib import Path

MATCHING_PACKAGE = Path(__file__).resolve().parents[2] / "app" / "services" / "matching"

FORBIDDEN_PREFIXES = (
    "app.services.agent",
    "anthropic",
    "openai",
    "httpx",
    "requests",
    "urllib.request",
    "aiohttp",
    "socket",
)


def _imported_modules(source: str) -> set[str]:
    """Every module name a file imports, including `from x.y import z` as `x.y`."""
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _violations() -> tuple[list[str], int]:
    """Returns (violations, files_scanned)."""
    found: list[str] = []
    files = sorted(MATCHING_PACKAGE.rglob("*.py"))
    for path in files:
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if module.startswith(FORBIDDEN_PREFIXES):
                found.append(f"{path.name} imports {module}")
    return found, len(files)


def test_matching_package_exists():
    """A missing package would make the purity check vacuously pass."""
    assert MATCHING_PACKAGE.is_dir(), f"{MATCHING_PACKAGE} not found"


def test_purity_check_actually_scans_files():
    """Guards against the failure mode where this test passes by finding nothing."""
    _, files_scanned = _violations()
    assert files_scanned >= 1, "no Python files scanned; the purity check would be vacuous"


def test_matching_core_imports_nothing_from_the_ai_layer_or_network():
    violations, files_scanned = _violations()
    assert not violations, (
        f"deterministic core is no longer sealed (ADR-0004), {files_scanned} files scanned:\n  "
        + "\n  ".join(violations)
    )


def test_detector_catches_a_planted_violation():
    """The check is only worth having if it would actually fail. Prove it does."""
    modules = _imported_modules("from app.services.agent.explain import explain_match\n")
    assert any(m.startswith(FORBIDDEN_PREFIXES) for m in modules)


def test_detector_ignores_permitted_imports():
    modules = _imported_modules("import datetime\nfrom app.core.money import to_minor_units\n")
    assert not any(m.startswith(FORBIDDEN_PREFIXES) for m in modules)
