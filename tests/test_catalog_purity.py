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
    template_path = repository / _TEMPLATES_DIRECTORY / name
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(body, encoding="utf-8")


def _run_check(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT)],
        check=False,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("template_name", "template_body", "expected_returncode", "expected_stdout"),
    [
        pytest.param(
            "probe.html",
            "<p>{{ wordmark }}</p>\n"
            '<input title="{{ hint }}" placeholder="{{ hint }}">\n'
            '<button type="submit" value="{{ submit }}">{{ submit }}</button>\n',
            0,
            "",
            id="catalog-lookups-only-passes",
        ),
        pytest.param(
            "probe.html",
            '<input class="hero" href="/decks" placeholder="42">\n',
            0,
            "",
            id="markup-numbers-and-unflagged-attributes-pass",
        ),
        pytest.param(
            "probe.html",
            '<input type="text" value="Not flagged">\n',
            0,
            "",
            id="value-on-a-plain-text-input-passes",
        ),
        pytest.param(
            "probe.html",
            "<p>Sign in below</p>\n",
            1,
            "probe.html: literal text outside the catalog: 'Sign in below'\n",
            id="literal-text-node-fails",
        ),
        pytest.param(
            "probe.html",
            '<input title="Enter your name">\n',
            1,
            "probe.html: literal text outside the catalog: 'Enter your name'\n",
            id="literal-flagged-attribute-fails",
        ),
        pytest.param(
            "probe.html",
            '<button type="submit" value="Go now">{{ submit }}</button>\n',
            1,
            "probe.html: literal text outside the catalog: 'Go now'\n",
            id="literal-value-on-a-submit-button-fails",
        ),
        pytest.param(
            "probe.html",
            '<button value="Go now">{{ submit }}</button>\n',
            1,
            "probe.html: literal text outside the catalog: 'Go now'\n",
            id="literal-value-on-a-button-with-omitted-type-fails",
        ),
        pytest.param(
            "probe.html",
            '<input type="button" value="Go now">\n',
            1,
            "probe.html: literal text outside the catalog: 'Go now'\n",
            id="literal-value-on-an-input-button-control-fails",
        ),
        pytest.param(
            "probe.html",
            '<input label="Enter your name">\n',
            1,
            "probe.html: literal text outside the catalog: 'Enter your name'\n",
            id="literal-label-attribute-fails",
        ),
        pytest.param(
            "probe.html",
            '<div aria-description="Read this first">{{ body }}</div>\n',
            1,
            "probe.html: literal text outside the catalog: 'Read this first'\n",
            id="literal-aria-description-attribute-fails",
        ),
        pytest.param(
            "probe.html",
            '<div aria-roledescription="Slide carousel">{{ body }}</div>\n',
            1,
            "probe.html: literal text outside the catalog: 'Slide carousel'\n",
            id="literal-aria-roledescription-attribute-fails",
        ),
        pytest.param(
            "probe.html",
            '<input aria-valuetext="Half full">\n',
            1,
            "probe.html: literal text outside the catalog: 'Half full'\n",
            id="literal-aria-valuetext-attribute-fails",
        ),
        pytest.param(
            "probe.html",
            '<meta name="description" content="A tool for a talk">\n',
            1,
            "probe.html: literal text outside the catalog: 'A tool for a talk'\n",
            id="literal-meta-description-content-fails",
        ),
        pytest.param(
            "probe.html",
            '<input title="Name{% if suffix %} ({{ suffix }}){% endif %}">\n',
            1,
            "probe.html: literal text outside the catalog: 'Name ()'\n",
            id="conditional-inside-an-attribute-value-fails",
        ),
        pytest.param(
            "probe.html",
            '<p>{{ "Sign in below" }}</p>\n',
            1,
            "probe.html: literal text outside the catalog: 'Sign in below'\n",
            id="literal-inside-a-jinja-expression-fails",
        ),
        pytest.param(
            "probe.html",
            '<p>{{ labels["home"] }}</p>\n',
            0,
            "",
            id="literal-used-as-a-subscript-key-passes",
        ),
        pytest.param(
            "probe.html",
            '<p>{{ "Yes" if flag else "No" }}</p>\n',
            1,
            "probe.html: literal text outside the catalog: 'Yes No'\n",
            id="literal-conditional-expression-branch-fails",
        ),
        pytest.param(
            "probe.html",
            "{% if debug %}<script>{% endif %}<p>Sign in below</p>\n",
            1,
            "probe.html: <script> is opened but never closed; everything "
            "after it is invisible to this check\n",
            id="unclosed-raw-text-tag-fails-loud",
        ),
        pytest.param(
            "auth/probe.html",
            "<p>Sign in below</p>\n",
            1,
            "auth/probe.html: literal text outside the catalog: 'Sign in below'\n",
            id="template-in-a-subdirectory-is-scanned",
        ),
        pytest.param(
            "probe.html",
            '<script>const label = "Sign in";</script>\n'
            "<style>.hero { color: red; }</style>\n",
            0,
            "",
            id="script-and-style-bodies-are-not-text",
        ),
    ],
)
def test_catalog_purity_check_reports_whether_every_template_stays_literal_free(
    tmp_path: Path,
    template_name: str,
    template_body: str,
    expected_returncode: int,
    expected_stdout: str,
) -> None:
    _write_template(tmp_path, template_name, template_body)

    completed = _run_check(tmp_path)

    assert completed.returncode == expected_returncode
    assert completed.stdout == expected_stdout


def test_catalog_purity_check_reports_a_template_syntax_error_without_crashing(
    tmp_path: Path,
) -> None:
    _write_template(tmp_path, "broken.html", "{% if x %}\n")

    completed = _run_check(tmp_path)

    assert completed.returncode == 1
    assert completed.stdout == (
        "broken.html: cannot parse: Unexpected end of template. Jinja was "
        "looking for the following tags: 'elif' or 'else' or 'endif'. The "
        "innermost block that needs to be closed is 'if'.\n"
    )


def test_catalog_purity_check_reports_a_missing_templates_directory_without_crashing(
    tmp_path: Path,
) -> None:
    completed = _run_check(tmp_path)

    assert completed.returncode == 1
    assert completed.stdout == "src/presentator/api/templates: no templates directory\n"
    assert completed.stderr == ""


def test_catalog_purity_check_reports_an_empty_templates_directory_without_crashing(
    tmp_path: Path,
) -> None:
    (tmp_path / _TEMPLATES_DIRECTORY).mkdir(parents=True)

    completed = _run_check(tmp_path)

    assert completed.returncode == 1
    assert completed.stdout == "src/presentator/api/templates: no template found\n"
    assert completed.stderr == ""


def test_catalog_purity_check_passes_on_the_real_templates() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    completed = _run_check(repository_root)

    assert completed.returncode == 0
    assert completed.stdout == ""
