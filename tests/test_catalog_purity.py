"""Catalog purity check."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_CHECK_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_catalog_purity.py"
)
_TEMPLATES_DIRECTORY = Path("src") / "presentator" / "api" / "templates"


def _write_template(repository: Path, name: str, body: str) -> None:
    templates_directory = repository / _TEMPLATES_DIRECTORY
    templates_directory.mkdir(parents=True, exist_ok=True)
    (templates_directory / name).write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("template_body", "expected_returncode", "expected_stdout"),
    [
        pytest.param(
            "<p>{{ wordmark }}</p>\n"
            '<input title="{{ hint }}" placeholder="{{ hint }}">\n'
            '<button type="submit" value="{{ submit }}">{{ submit }}</button>\n',
            0,
            "",
            id="catalog-lookups-only-passes",
        ),
        pytest.param(
            '<input class="hero" href="/decks" placeholder="42">\n',
            0,
            "",
            id="markup-numbers-and-unflagged-attributes-pass",
        ),
        pytest.param(
            '<input type="button" value="Not flagged">\n',
            0,
            "",
            id="value-on-a-non-submit-control-passes",
        ),
        pytest.param(
            "<p>Sign in below</p>\n",
            1,
            "probe.html: literal text outside the catalog: 'Sign in below'\n",
            id="literal-text-node-fails",
        ),
        pytest.param(
            '<input title="Enter your name">\n',
            1,
            "probe.html: literal text outside the catalog: 'Enter your name'\n",
            id="literal-flagged-attribute-fails",
        ),
        pytest.param(
            '<button type="submit" value="Go now">{{ submit }}</button>\n',
            1,
            "probe.html: literal text outside the catalog: 'Go now'\n",
            id="literal-value-on-a-submit-control-fails",
        ),
    ],
)
def test_catalog_purity_check_reports_whether_every_template_stays_literal_free(
    tmp_path: Path,
    template_body: str,
    expected_returncode: int,
    expected_stdout: str,
) -> None:
    _write_template(tmp_path, "probe.html", template_body)

    completed = subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT)],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == expected_returncode
    assert completed.stdout == expected_stdout


def test_catalog_purity_check_passes_on_the_real_templates() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT)],
        check=False,
        cwd=repository_root,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
