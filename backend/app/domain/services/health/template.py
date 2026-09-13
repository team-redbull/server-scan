"""Safe health-policy message templates.

No Jinja, no `eval`, no `str.format`/`format_map` on an untrusted template
string — `str.format`'s field syntax reaches attribute/index access
(`{obj.__class__}`, `{obj[0]}`, positional `{0}`) which is exactly the
kind of reachability a policy author (someone with `policy:write`, not
necessarily someone we'd trust with arbitrary object introspection) should
never get from a message string. `string.Formatter().parse()` is used only
to *tokenize* the template, never to render it — rendering is explicit
substitution from a caller-provided evidence dict, nothing else.
"""

from __future__ import annotations

import re
from string import Formatter

MAX_TEMPLATE_LENGTH = 300
MAX_TEMPLATE_FIELDS = 10
MAX_RENDERED_LENGTH = 300
MAX_LIST_ELEMENTS_RENDERED = 5

_FIELD_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ALLOWED_FORMAT_SPECS = re.compile(r"^$|^\.\d{1,2}f$|^d$|^,d$")


class TemplateValidationError(Exception):
    """A message template contains something unsafe to render."""


def validate_template(template: str, allowed_fields: set[str]) -> None:
    """
    Reject a message template unless every field is a plain, declared reference.

    Called at policy write time.

    Args:
        template (str): The message template to validate.
        allowed_fields (set[str]): The policy's declared `evidence` keys —
            the only names `template` may reference.

    Raises:
        TemplateValidationError: If `template` is too long, has too many
            fields, references a name outside `allowed_fields`, or uses a
            conversion specifier or a disallowed format spec.
    """
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise TemplateValidationError(f"template exceeds {MAX_TEMPLATE_LENGTH} characters")

    field_count = 0
    for _literal, field_name, format_spec, conversion in Formatter().parse(template):
        if field_name is None:
            continue
        field_count += 1
        if field_count > MAX_TEMPLATE_FIELDS:
            raise TemplateValidationError(f"template exceeds {MAX_TEMPLATE_FIELDS} fields")
        if not _FIELD_NAME_RE.match(field_name):
            raise TemplateValidationError(f"invalid field reference {field_name!r}")
        if field_name not in allowed_fields:
            raise TemplateValidationError(f"field {field_name!r} is not a declared evidence key")
        if conversion is not None:
            raise TemplateValidationError(f"conversion specifiers are not allowed: !{conversion}")
        if format_spec and not _ALLOWED_FORMAT_SPECS.match(format_spec):
            raise TemplateValidationError(f"format spec not allowed: {format_spec!r}")


def _coerce(value: object, format_spec: str) -> str:
    if isinstance(value, list):
        shown = value[:MAX_LIST_ELEMENTS_RENDERED]
        suffix = ", …" if len(value) > MAX_LIST_ELEMENTS_RENDERED else ""
        return ", ".join(str(v) for v in shown) + suffix
    if value is None:
        return "?"
    if format_spec:
        try:
            return format(value, format_spec)
        except (ValueError, TypeError):
            return str(value)
    return str(value)


def render_template(template: str, evidence: dict[str, object]) -> str:
    """
    Render a validated message template by explicit field substitution.

    Never `str.format`/`format_map`: not calling them is the enforcement
    (docs/architecture.md, "Health policy engine").

    Args:
        template (str): The template, already passed through `validate_template`.
        evidence (dict[str, object]): The values to substitute, keyed by field name.

    Returns:
        str: The rendered message, truncated to `MAX_RENDERED_LENGTH`.
    """
    parts: list[str] = []
    for literal, field_name, format_spec, _conversion in Formatter().parse(template):
        parts.append(literal)
        if field_name is None:
            continue
        parts.append(_coerce(evidence.get(field_name), format_spec or ""))
    return "".join(parts)[:MAX_RENDERED_LENGTH]
