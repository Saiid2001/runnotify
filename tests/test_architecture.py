"""Static checks on the boundaries the design depends on.

A channel seam is only real if the core cannot reach around it. These scan the
source rather than the behaviour, because the failure they guard against is a
convenience — one vendor special case in the orchestration layer — that no
functional test would notice.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import runnotify

SRC = Path(runnotify.__file__).parent

#: Names of things only a channel may know about.
VENDOR_TERMS = (
    "slack",
    "notion",
    "hooks.slack.com",
    "api.notion.com",
    "webhook_url",
    "notion-version",
    "rich_text",
)

#: Modules that must stay vendor-neutral. Everything a study composes runs
#: through these, and a special case here is one no plugin can override.
CORE_MODULES = (
    "notifier.py",
    "channel.py",
    "event.py",
    "_http.py",
    "_watchdog.py",
    "config.py",
    "_cli.py",
)


def source_of(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def code_without_docstrings(name: str) -> str:
    """The module's source with docstrings stripped.

    Prose may name a vendor — the documentation has to be concrete. Code may not.
    """
    tree = ast.parse(source_of(name))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    lines = source_of(name).splitlines()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value in docstrings
        ):
            for lineno in range(node.lineno - 1, (node.end_lineno or node.lineno)):
                lines[lineno] = ""
    return "\n".join(lines)


@pytest.mark.parametrize("module", CORE_MODULES)
@pytest.mark.parametrize("term", VENDOR_TERMS)
def test_core_modules_hold_no_vendor_knowledge(module: str, term: str) -> None:
    code = code_without_docstrings(module).lower()
    assert term not in code, (
        f"{module} mentions {term!r} outside a docstring. Vendor knowledge belongs "
        f"in runnotify/channels/, which is the only place a plugin can replace."
    )


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_modules_do_not_import_a_channel(module: str) -> None:
    tree = ast.parse(source_of(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "channels" not in node.module.split("."), (
                f"{module} imports from runnotify.channels; the dependency runs the other way."
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "channels" not in alias.name.split(".")


@pytest.mark.parametrize("module", ["channels/slack.py", "channels/notion.py"])
def test_each_channel_knows_only_its_own_vendor(module: str) -> None:
    """Slack must not know Notion, and vice versa."""
    other = "notion" if "slack" in module else "slack"
    assert other not in code_without_docstrings(module).lower()


def test_the_package_has_no_runtime_dependencies() -> None:
    """Imported by every run in a study; a dependency here is one there too."""
    import tomllib

    pyproject = Path(runnotify.__file__).parents[2] / "pyproject.toml"
    if not pyproject.is_file():  # installed from a wheel, not the source tree
        pytest.skip("not running against the source tree")
    with pyproject.open("rb") as handle:
        assert tomllib.load(handle)["project"]["dependencies"] == []


def test_every_public_name_is_importable_from_the_package_root() -> None:
    for name in runnotify.__all__:
        assert hasattr(runnotify, name), f"{name} is exported but missing"


def test_the_package_ships_a_typing_marker() -> None:
    assert (SRC / "py.typed").is_file()
