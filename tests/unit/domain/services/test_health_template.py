import pytest

from app.domain.services.health.template import (
    TemplateValidationError,
    render_template,
    validate_template,
)


class TestValidateTemplate:
    def test_accepts_declared_field(self) -> None:
        validate_template("{down} of {total} paths down", {"down", "total"})

    def test_rejects_undeclared_field(self) -> None:
        with pytest.raises(TemplateValidationError, match="not a declared"):
            validate_template("{secret}", {"down"})

    def test_rejects_attribute_access(self) -> None:
        with pytest.raises(TemplateValidationError, match="invalid field"):
            validate_template("{obj.__class__}", {"obj"})

    def test_rejects_index_access(self) -> None:
        with pytest.raises(TemplateValidationError, match="invalid field"):
            validate_template("{obj[0]}", {"obj"})

    def test_rejects_positional_field(self) -> None:
        with pytest.raises(TemplateValidationError, match="invalid field"):
            validate_template("{0}", set())

    def test_rejects_conversion_specifier(self) -> None:
        with pytest.raises(TemplateValidationError, match="conversion"):
            validate_template("{down!r}", {"down"})

    def test_rejects_disallowed_format_spec(self) -> None:
        with pytest.raises(TemplateValidationError, match="format spec"):
            validate_template("{down:>99999}", {"down"})

    def test_allows_decimal_format_spec(self) -> None:
        validate_template("{down:.2f}", {"down"})

    def test_rejects_too_many_fields(self) -> None:
        template = " ".join(f"{{f{i}}}" for i in range(11))
        with pytest.raises(TemplateValidationError, match="fields"):
            validate_template(template, {f"f{i}" for i in range(11)})

    def test_rejects_overlong_template(self) -> None:
        with pytest.raises(TemplateValidationError, match="characters"):
            validate_template("x" * 301, set())


class TestRenderTemplate:
    def test_substitutes_declared_fields(self) -> None:
        result = render_template("{down} of {total} paths down", {"down": 1, "total": 2})
        assert result == "1 of 2 paths down"

    def test_missing_evidence_key_renders_placeholder_not_crash(self) -> None:
        result = render_template("{down}", {})
        assert result == "?"

    def test_list_evidence_is_joined_and_capped(self) -> None:
        result = render_template("fabrics: {fabrics}", {"fabrics": ["A", "B", "C", "D", "E", "F"]})
        assert result == "fabrics: A, B, C, D, E, …"

    def test_never_reaches_dunder_attributes_of_a_non_scalar(self) -> None:
        class Evil:
            def __format__(self, spec: str) -> str:
                raise RuntimeError("should never be called")

        # A non-scalar in evidence must not reach an exotic `__format__` hook:
        # `_coerce` special-cases list/None and otherwise falls back to str().
        result = render_template("{x}", {"x": Evil()})
        assert isinstance(result, str)

    def test_output_is_length_capped(self) -> None:
        result = render_template("{x}", {"x": "y" * 1000})
        assert len(result) <= 300
