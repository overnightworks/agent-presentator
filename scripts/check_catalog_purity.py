"""Catalog purity: no template carries a literal, user-facing string.

Owner: [ADR 0012](../docs/decisions/0012-themes-and-language.md), "Language is
an adapter". Every user-facing word in a Jinja2 lobby template must come from
the message catalog; typing a sentence into a template is the retrofit ADR
0012 exists to avoid. This script is a CI gate, not a package layer.

The rule is narrow and mechanical: it flags a literal (non-catalog) piece of
template text only where a person reads it — HTML text-node content, or one
of the human-readable attributes `title`, `placeholder`, `aria-label`, `alt`,
and `value` on a submit control (`<button type="submit">` or `<input
type="submit">`). Markup (tag names, other attributes such as `class`,
`href`, `id`), and content made only of digits or whitespace, are not text
under this rule.

Jinja2's own parser draws that line for us. `jinja2.Environment.parse` returns
an AST in which every literal template character lives in a `TemplateData`
node and every substituted value is an expression node (`Name`, `Getattr`,
...); a catalog lookup is always an expression, never literal text, so a
`TemplateData` chunk sitting at a text or flagged-attribute position can only
be a hand-typed literal. Walking that AST — rather than a regular expression
over the raw source — cannot be fooled by markup that looks like a sentence or
by a `{{ }}` that happens to look like plain text.

Each `Output` node in the AST holds one ordered run of `TemplateData` and
expression children between two structural boundaries (an extends, block, if,
or for). To find text and attribute positions, this run is reconstructed as an
HTML fragment: literal chunks verbatim, expressions replaced by the empty
string. A stdlib `html.parser.HTMLParser` then walks that fragment for start
tags (to catch flagged attributes) and text data (to catch text nodes). Runs
reset between `Output` nodes, which is safe here because Jinja never splits a
tag or an attribute value across two `Output` nodes; it always splits at a
`>` or in plain text.

Rejected: a regular expression over the raw template source, which the task
that named this check already rejected — it cannot tell a `{{ catalog_value
}}` from a literal sentence, or a class name from a word a person reads. A
full HTML/DOM library was not warranted either: the stdlib `html.parser` gives
exactly the two callbacks (`handle_data`, `handle_starttag`) this narrow rule
needs, with no new dependency.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import jinja2
from jinja2 import nodes

_TEMPLATES_DIRECTORY = Path("src") / "presentator" / "api" / "templates"
_TEMPLATE_GLOB = "*.html"

_FLAGGED_ATTRIBUTES: frozenset[str] = frozenset(
    {"title", "placeholder", "aria-label", "alt"}
)
_SUBMIT_CONTROL_TAGS: frozenset[str] = frozenset({"button", "input"})

_EXPRESSION_PLACEHOLDER = ""

_OFFENDER_MESSAGE = "{template}: literal text outside the catalog: {text!r}"


@dataclass(frozen=True)
class CatalogPurityViolation:
    """One literal, catalog-bypassing string found in one template."""

    template_name: str
    literal_text: str


def _is_user_facing_text(candidate: str) -> bool:
    stripped = candidate.strip()
    return bool(stripped) and any(character.isalpha() for character in stripped)


def _reconstruct_fragment(children: tuple[nodes.Node, ...]) -> str:
    """Rebuild one `Output` run as HTML, expressions blanked to the empty string."""
    parts = [
        child.data if isinstance(child, nodes.TemplateData) else _EXPRESSION_PLACEHOLDER
        for child in children
    ]
    return "".join(parts)


class _CatalogPurityScanner(HTMLParser):
    """Collects literal text at text-node and flagged-attribute positions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.literal_texts: list[str] = []

    def handle_data(self, data: str) -> None:
        """Record text-node content that carries a literal, catalog-bypassing word."""
        if _is_user_facing_text(data):
            self.literal_texts.append(data.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record flagged attribute values that carry a literal, bypassing word."""
        values_by_name = dict(attrs)
        is_submit_control = (
            tag in _SUBMIT_CONTROL_TAGS and values_by_name.get("type") == "submit"
        )
        flagged_names = _FLAGGED_ATTRIBUTES | (
            {"value"} if is_submit_control else set[str]()
        )
        for name in flagged_names:
            value = values_by_name.get(name)
            if value is not None and _is_user_facing_text(value):
                self.literal_texts.append(value.strip())


def _literal_texts_in_template(template_ast: nodes.Template) -> list[str]:
    literal_texts: list[str] = []
    for output in template_ast.find_all(nodes.Output):
        scanner = _CatalogPurityScanner()
        scanner.feed(_reconstruct_fragment(tuple(output.nodes)))
        scanner.close()
        literal_texts.extend(scanner.literal_texts)
    return literal_texts


def find_catalog_purity_violations(
    repository: Path,
) -> tuple[CatalogPurityViolation, ...]:
    """Return every literal, catalog-bypassing string found in a template."""
    # This environment only parses templates to an AST and never renders one,
    # so autoescape has no effect on this check's behaviour.
    environment = jinja2.Environment(autoescape=True)
    templates_directory = repository / _TEMPLATES_DIRECTORY
    violations: list[CatalogPurityViolation] = []
    for template_path in sorted(templates_directory.glob(_TEMPLATE_GLOB)):
        source = template_path.read_text(encoding="utf-8")
        template_ast = environment.parse(source, filename=template_path.name)
        violations.extend(
            CatalogPurityViolation(template_name=template_path.name, literal_text=text)
            for text in _literal_texts_in_template(template_ast)
        )
    return tuple(violations)


def main() -> int:
    """Print each catalog purity violation and return 1 when any exist."""
    violations = find_catalog_purity_violations(Path.cwd())
    if not violations:
        return 0
    for violation in violations:
        sys.stdout.write(
            _OFFENDER_MESSAGE.format(
                template=violation.template_name, text=violation.literal_text
            )
            + "\n"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
