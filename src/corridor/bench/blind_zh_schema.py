"""Validate the published case schema without an optional runtime dependency."""

from pathlib import Path

from corridor.schemas.base import loads_exact, reject_floats

CASE_SCHEMA = Path(__file__).resolve().parents[3] / "data/blind-zh/materials/case-schema.json"
KEYWORDS = {
    "$schema",
    "$id",
    "title",
    "type",
    "required",
    "properties",
    "additionalProperties",
    "const",
    "enum",
    "minLength",
    "minItems",
    "maxItems",
    "items",
    "minimum",
    "maximum",
}
JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _check_schema(schema):
    """Reject unsupported schema changes instead of silently weakening validation."""
    if not isinstance(schema, dict) or set(schema) - KEYWORDS:
        raise ValueError("Unsupported case schema")
    for child in schema.get("properties", {}).values():
        _check_schema(child)
    if "items" in schema:
        _check_schema(schema["items"])
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not set(names) <= JSON_TYPES.keys():
            raise ValueError("Unsupported case schema type")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError("Unsupported additional property schema")


def _matches(value, schema):
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if type(value) not in [JSON_TYPES[name] for name in names]:
            return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            return False
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            return False
        if any(
            not _matches(child, properties[key])
            for key, child in value.items()
            if key in properties
        ):
            return False
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            return False
        if "items" in schema and any(not _matches(child, schema["items"]) for child in value):
            return False
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        return False
    if type(value) is int:
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            return False
    return True


def validate_schema(value):
    """Use the authoritative JSON schema; all diagnostics are content-free."""
    reject_floats(value)
    schema = loads_exact(CASE_SCHEMA.read_bytes())
    _check_schema(schema)
    if not _matches(value, schema):
        raise ValueError("Case does not satisfy case-schema.json")
