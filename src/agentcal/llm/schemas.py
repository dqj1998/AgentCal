"""Minimal JSON-schema validation (no external dependency)."""
from __future__ import annotations

TYPES = {"object": dict, "string": str, "boolean": bool, "number": (int, float), "integer": int, "array": list}


def validate(value, schema: dict) -> tuple[bool, str]:
    expected = schema.get("type")
    if expected and expected in TYPES and not isinstance(value, TYPES[expected]):
        return False, f"expected_{expected}"
    if expected == "boolean" and isinstance(value, bool):
        return True, "ok"
    if expected == "object":
        if not isinstance(value, dict):
            return False, "expected_object"
        for key in schema.get("required", []):
            if key not in value:
                return False, f"missing_{key}"
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                ok, reason = validate(value[key], sub)
                if not ok:
                    return False, f"{key}:{reason}"
    if "enum" in schema and value not in schema["enum"]:
        return False, "not_in_enum"
    if expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            ok, reason = validate(item, schema["items"])
            if not ok:
                return False, f"[{index}]:{reason}"
    return True, "ok"
