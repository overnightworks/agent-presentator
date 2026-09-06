"""Catalog purity: no template carries a literal, user-facing string.

Owner: [ADR 0012](../docs/decisions/0012-themes-and-language.md), "Language is
an adapter". Every user-facing word in a Jinja2 lobby template must come from
the message catalog; typing a sentence into a template is the retrofit ADR
0012 exists to avoid. This script is a CI gate, not a package layer.

The rule is narrow and mechanical: it flags a literal (non-catalog) piece of
template text only where a person reads it — HTML text-node content, or one
of the human-readable attributes `title`, `placeholder`, `aria-label`, `alt`,
`label`, `aria-description`, `aria-roledescription`, `aria-valuetext`, a
`value` on a submit, button, or reset control (an `<input>` of that type, or a
`<button>` whose type is `submit` or omitted — the HTML default), and a
`content` on `<meta name="description">`. A plain `<input>`'s `value` is data
a person typed, not a label, and is left alone. Markup (tag names, other
attributes such as `class`, `href`, `id`, and a plain `<input>`'s `value`),
and content made only of digits or whitespace, are not text under this rule.

Jinja2's own parser draws that line for us. `jinja2.Environment.parse` returns
an AST in which every literal template character lives in a `TemplateData`
node and every substituted value is an expression node (`Name`, `Getattr`,
`Const`, ...); a catalog lookup is always a `Name`/`Getattr`, never a literal.
A hard-coded `{{ "Sign in" }}` bypasses the catalog exactly as much as the
same words typed straight into the template, so a `Const` string folded
anywhere into an expression — including inside a ternary such as `{{ "Yes" if
flag else "No" }}` — is treated as literal text too. Walking that AST, rather
than a regular expression over the raw source, cannot be fooled by markup
that looks like a sentence or by a `{{ }}` that happens to look like plain
text.

The AST's `Output` nodes are visited in document order (`find_all` walks
`If`/`For`/`Block` bodies in the order they are written) and fed, one after
another, into a single `html.parser.HTMLParser` for the whole template —
never reset between `Output` nodes. That single-scanner design is what lets a
`{% if %}` sitting inside a tag or an attribute value resolve correctly:
`HTMLParser.feed()` is built to buffer an incomplete tag across any number of
calls, so `<input{% if required %} required{% endif %}>` and `title="Name{%
if suffix %} ({{ suffix }}){% endif %}"` are seen as the one tag or attribute
they render as. Mutually exclusive branches (`if`/`else`, loop `else`) are fed
one after another as if both ran; that is a deliberate simplification, safe
for this check because it only ever asks "does literal text sit at a text or
flagged-attribute position", never "is the markup correctly nested".

Script and style bodies are excluded from the text-node scan: a `<script>`
or `<style>` element's content is code, not something a person reads as
prose, and flagging a JavaScript string literal here would be a false
positive this check has no business raising.

Named gap, not fixed here: a word that reaches the rendered page from Python
— a value computed or interpolated by a route rather than looked up in the
catalog — is invisible to this check, because it never appears as
`TemplateData` or a `Const` in any template's AST. A template scan can prove
a template stays literal-free; it cannot prove the whole page does.

Rejected: a regular expression over the raw template source, which the task
that named this check already rejected — it cannot tell a `{{ catalog_value
}}` from a literal sentence, or a class name from a word a person reads. A
full HTML/DOM library was not warranted either: the stdlib `html.parser`
gives exactly the two callbacks (`handle_data`, `handle_starttag`) this
narrow rule needs, with no new dependency.
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

_ALWAYS_FLAGGED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "title",
        "placeholder",
        "aria-label",
        "alt",
        "label",
        "aria-description",
        "aria-roledescription",
        "aria-valuetext",
    }
)
_INPUT_TYPES_WITH_A_READ_LABEL_VALUE: frozenset[str] = frozenset(
    {"submit", "button", "reset"}
)
_RAW_TEXT_TAGS: frozenset[str] = frozenset({"script", "style"})

_OFFENDER_MESSAGE = "{template}: {problem}"
_LITERAL_TEXT_PROBLEM = "literal text outside the catalog: {text!r}"
_PARSE_ERROR_PROBLEM = "cannot parse: {error}"


@dataclass(frozen=True)
class CatalogPurityFinding:
    """One problem found in one template: a literal string, or a syntax error."""

    template_name: str
    problem: str


def _is_user_facing_text(candidate: str) -> bool:
    stripped = candidate.strip()
    return bool(stripped) and any(character.isalpha() for character in stripped)


def _value_attribute_is_flagged(tag: str, type_value: str | None) -> bool:
    """A submit/button/reset value is a read label; a plain input's value is data."""
    if tag == "input":
        return type_value in _INPUT_TYPES_WITH_A_READ_LABEL_VALUE
    if tag == "button":
        return type_value is None or type_value == "submit"
    return False


def _flagged_attribute_names(
    tag: str, values_by_name: dict[str, str | None]
) -> frozenset[str]:
    names = set(_ALWAYS_FLAGGED_ATTRIBUTES)
    if _value_attribute_is_flagged(tag, values_by_name.get("type")):
        names.add("value")
    if tag == "meta" and values_by_name.get("name") == "description":
        names.add("content")
    return frozenset(names)


def _string_literal_in_expression(expression: nodes.Node) -> str:
    """Fold every string constant nested in a Jinja expression into plain text.

    `find_all` only searches an expression's children, so a bare `Const`
    string used directly as the whole expression (`{{ "Sign in" }}`) is
    included explicitly alongside any `Const` nested deeper (`{{ "Yes" if
    flag else "No" }}`).
    """
    candidates = (expression, *expression.find_all(nodes.Const))
    return " ".join(
        candidate.value
        for candidate in candidates
        if isinstance(candidate, nodes.Const) and isinstance(candidate.value, str)
    )


def _reconstruct_fragment(children: tuple[nodes.Node, ...]) -> str:
    """Rebuild one `Output` run as HTML: literal chunks verbatim, expressions folded."""
    parts = [
        child.data
        if isinstance(child, nodes.TemplateData)
        else _string_literal_in_expression(child)
        for child in children
    ]
    return "".join(parts)


class _CatalogPurityScanner(HTMLParser):
    """Collects literal text at text-node and flagged-attribute positions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.literal_texts: list[str] = []
        self._raw_text_tag: str | None = None

    def handle_data(self, data: str) -> None:
        """Record text-node content, ignoring script/style bodies."""
        if self._raw_text_tag is not None:
            return
        if _is_user_facing_text(data):
            self.literal_texts.append(data.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record flagged attribute values and enter a script/style body."""
        values_by_name = dict(attrs)
        for name in _flagged_attribute_names(tag, values_by_name):
            value = values_by_name.get(name)
            if value is not None and _is_user_facing_text(value):
                self.literal_texts.append(value.strip())
        if tag in _RAW_TEXT_TAGS:
            self._raw_text_tag = tag

    def handle_endtag(self, tag: str) -> None:
        """Leave a script/style body once it closes."""
        if tag == self._raw_text_tag:
            self._raw_text_tag = None


def _literal_texts_in_template(template_ast: nodes.Template) -> list[str]:
    scanner = _CatalogPurityScanner()
    for output in template_ast.find_all(nodes.Output):
        scanner.feed(_reconstruct_fragment(tuple(output.nodes)))
    scanner.close()
    return scanner.literal_texts


def _require_template_paths(templates_directory: Path) -> tuple[Path, ...]:
    """Return every template path, sorted; refuse silence when there is none."""
    if not templates_directory.is_dir():
        message = f"no templates directory at {templates_directory}"
        raise FileNotFoundError(message)
    template_paths = tuple(sorted(templates_directory.rglob(_TEMPLATE_GLOB)))
    if not template_paths:
        message = f"no template found under {templates_directory}"
        raise FileNotFoundError(message)
    return template_paths


def _template_findings(
    template_path: Path, relative_name: str, environment: jinja2.Environment
) -> list[CatalogPurityFinding]:
    source = template_path.read_text(encoding="utf-8")
    try:
        template_ast = environment.parse(source, filename=relative_name)
    except jinja2.TemplateSyntaxError as error:
        problem = _PARSE_ERROR_PROBLEM.format(error=error.message)
        return [CatalogPurityFinding(template_name=relative_name, problem=problem)]
    return [
        CatalogPurityFinding(
            template_name=relative_name,
            problem=_LITERAL_TEXT_PROBLEM.format(text=text),
        )
        for text in _literal_texts_in_template(template_ast)
    ]


def find_catalog_purity_findings(repository: Path) -> tuple[CatalogPurityFinding, ...]:
    """Return every literal string or template syntax error found under templates."""
    environment = jinja2.Environment()
    templates_directory = repository / _TEMPLATES_DIRECTORY
    findings: list[CatalogPurityFinding] = []
    for template_path in _require_template_paths(templates_directory):
        relative_name = template_path.relative_to(templates_directory).as_posix()
        findings.extend(_template_findings(template_path, relative_name, environment))
    return tuple(findings)


def main() -> int:
    """Print each catalog purity finding and return 1 when any exist."""
    findings = find_catalog_purity_findings(Path.cwd())
    if not findings:
        return 0
    for finding in findings:
        sys.stdout.write(
            _OFFENDER_MESSAGE.format(
                template=finding.template_name, problem=finding.problem
            )
            + "\n"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
