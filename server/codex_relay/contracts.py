"""Validate wire data before Pydantic can coerce values or supply defaults."""
import json
import re
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

_ROOT = Path(__file__).resolve().parents[2] / "contracts"
_VALIDATORS = {}
_FORMATS = FormatChecker()


@_FORMATS.checks("date-time", raises=ValueError)
def _date_time(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)", value):
        return False
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).tzinfo is not None


def validate(name, payload):
    if name not in _VALIDATORS:
        schema = json.loads((_ROOT / (name + ".schema.json")).read_text(encoding="utf-8"))
        _VALIDATORS[name] = Draft202012Validator(schema, format_checker=_FORMATS)
    _VALIDATORS[name].validate(payload)


def load_json(raw):
    def reject_constant(value):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, parse_constant=reject_constant)
