import json
import re
from datetime import datetime
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def _date_time(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)", value):
        return False
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).tzinfo is not None

VALIDATORS = {name: Draft202012Validator(
    json.loads((ROOT / "contracts" / f"{name}.schema.json").read_text(encoding="utf-8")),
    format_checker=FORMATS) for name in ("health", "telemetry", "telemetry-reply")}


def validate(name, data):
    VALIDATORS[name].validate(data)
    return data
