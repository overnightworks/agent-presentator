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
same words typed straight into the template, so a `Const` string is treated
as literal text too, but only where it can be the expression's own runtime
value: the expression itself, a conditional expression's branch (`{{ "Yes" if
flag else "No" }}`), an `or`-chain's side (`{{ x or "Fallback" }}`), a `~`
concatenation's operand (`{{ "Welcome " ~ name }}`), or `default`'s first
positional argument (`{{ value | default("Sign in") }}`) — the one filter
whose argument becomes its own output when the piped value is empty. A
constant used as a subscript key (`{{ labels["home"] }}`) or as any other
call or filter argument never reaches the page as that expression's value, so
it is not followed. Walking that AST, rather than a regular expression over
the raw source, cannot be fooled by markup that looks like a sentence or by a
`{{ }}` that happens to look like plain text.

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
positive this check has no business raising. The one persistent scanner pays
for that with a new failure mode: a `<script>` or `<style>` opened on one
branch of a template and never closed leaves the scanner inside it for the
rest of the file, so every literal after it would otherwise go unseen. That
is reported as its own finding — an open tag caught at end of file — rather
than a silent pass, because a scan that can be silently blinded is worse than
one that visibly refuses to trust itself.

Named gaps, not fixed here: a word that reaches the rendered page from Python
— a value computed or interpolated by a route rather than looked up in the
catalog — is invisible to this check, because it never appears as
`TemplateData` or a `Const` in any template's AST. So is a literal bound with
`{% set heading = "Sign in" %}` and rendered later through the bound name:
this check only ever looks at what an `Output` node renders, never at what a
`set` assigns, because following an assignment to every place its name is
later used is a dataflow analysis, not the narrow "does literal text sit at a
text or attribute position" this rule stays mechanical by asking. A template
scan can prove a template's own markup and expressions stay literal-free; it
cannot prove the whole rendered page does.

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
_DEFAULT_FILTER_NAME = "default"

_OFFENDER_MESSAGE = "{template}: {problem}"
_LITERAL_TEXT_PROBLEM = "literal text outside the catalog: {text!r}"
_PARSE_ERROR_PROBLEM = "cannot parse: {error}"
_UNCLOSED_RAW_TEXT_PROBLEM = (
    "<{tag}> is opened but never closed; everything after it is invisible to this check"
)
_NO_TEMPLATES_DIRECTORY_PROBLEM = "no templates directory"
_NO_TEMPLATE_FOUND_PROBLEM = "no template found"


@dataclass(frozen=True)
class CatalogPurityFinding:
    """One problem found under the templates directory.

    A literal string, a syntax error, an unclosed script/style tag, or a
    missing or empty templates directory.
    """

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


def _string_literal_values(expression: nodes.Node) -> tuple[str, ...]:
    """Return the string literals that can be this expression's own runtime value.

    Only descends into node types whose evaluated result is exactly one of
    their operands: a bare constant, a conditional expression's taken
    branch, an `or`-chain's left or right side, a `~` concatenation's
    operand, or the `default` filter's first positional argument (the one
    filter whose argument becomes its own output when the piped value is
    empty). A constant used as a subscript key (`labels["home"]`) or as any
    other call/filter argument never reaches the page as this expression's
    value, so those are not followed.
    """
    if isinstance(expression, nodes.Const):
        return (expression.value,) if isinstance(expression.value, str) else ()
    if isinstance(expression, nodes.CondExpr):
        branches = [expression.expr1]
        if expression.expr2 is not None:
            branches.append(expression.expr2)
        return tuple(
            literal for branch in branches for literal in _string_literal_values(branch)
        )
    if isinstance(expression, nodes.Or):
        return (
            *_string_literal_values(expression.left),
            *_string_literal_values(expression.right),
        )
    if isinstance(expression, nodes.Concat):
        return tuple(
            literal
            for operand in expression.nodes
            for literal in _string_literal_values(operand)
        )
    if isinstance(expression, nodes.Filter) and expression.name == _DEFAULT_FILTER_NAME:
        first_positional_argument = expression.args[0] if expression.args else None
        if first_positional_argument is not None:
            return _string_literal_values(first_positional_argument)
    return ()


def _reconstruct_fragment(children: tuple[nodes.Node, ...]) -> str:
    """Rebuild one `Output` run as HTML: literal chunks verbatim, expressions folded."""
    parts = [
        child.data
        if isinstance(child, nodes.TemplateData)
        else " ".join(_string_literal_values(child))
        for child in children
    ]
    return "".join(parts)


class _CatalogPurityScanner(HTMLParser):
    """Collects literal text at text-node and flagged-attribute positions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.literal_texts: list[str] = []
        self.open_raw_text_tag: str | None = None

    def handle_data(self, data: str) -> None:
        """Record text-node content, ignoring script/style bodies."""
        if self.open_raw_text_tag is not None:
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
            self.open_raw_text_tag = tag

    def handle_endtag(self, tag: str) -> None:
        """Leave a script/style body once it closes."""
        if tag == self.open_raw_text_tag:
            self.open_raw_text_tag = None


def _scan_template(template_ast: nodes.Template) -> _CatalogPurityScanner:
    scanner = _CatalogPurityScanner()
    for output in template_ast.find_all(nodes.Output):
        scanner.feed(_reconstruct_fragment(tuple(output.nodes)))
    scanner.close()
    return scanner


def _require_template_paths(templates_directory: Path) -> tuple[Path, ...]:
    """Return every template path, sorted; refuse silence when there is none."""
    if not templates_directory.is_dir():
        raise FileNotFoundError(_NO_TEMPLATES_DIRECTORY_PROBLEM)
    template_paths = tuple(sorted(templates_directory.rglob(_TEMPLATE_GLOB)))
    if not template_paths:
        raise FileNotFoundError(_NO_TEMPLATE_FOUND_PROBLEM)
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
    scanner = _scan_template(template_ast)
    findings = [
        CatalogPurityFinding(
            template_name=relative_name,
            problem=_LITERAL_TEXT_PROBLEM.format(text=text),
        )
        for text in scanner.literal_texts
    ]
    if scanner.open_raw_text_tag is not None:
        findings.append(
            CatalogPurityFinding(
                template_name=relative_name,
                problem=_UNCLOSED_RAW_TEXT_PROBLEM.format(
                    tag=scanner.open_raw_text_tag
                ),
            )
        )
    return findings


def find_catalog_purity_findings(repository: Path) -> tuple[CatalogPurityFinding, ...]:
    """Return every problem found under the templates.

    Names the templates directory itself when there is nothing to scan.
    """
    environment = jinja2.Environment()
    templates_directory = repository / _TEMPLATES_DIRECTORY
    try:
        template_paths = _require_template_paths(templates_directory)
    except FileNotFoundError as error:
        problem = CatalogPurityFinding(
            template_name=_TEMPLATES_DIRECTORY.as_posix(), problem=str(error)
        )
        return (problem,)
    findings: list[CatalogPurityFinding] = []
    for template_path in template_paths:
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
