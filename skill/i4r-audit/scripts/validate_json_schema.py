#!/usr/bin/env python3
"""Dependency-free, fail-closed validator for the skill's JSON Schema vocabulary."""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any


SUPPORTED = {
    "$schema", "$id", "$defs", "$ref", "$comment", "title", "description", "default",
    "type", "required", "properties", "additionalProperties", "items", "enum", "const",
    "pattern", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    "uniqueItems", "minProperties", "maxProperties", "format", "allOf", "oneOf", "anyOf",
    "not", "if", "then", "else",
}
DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is forbidden")


def strict_loads(text: str, *, source: str = "JSON input") -> Any:
    """Parse RFC 8259 JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{source}: {exc}") from exc


def finite_errors(value: Any, path: str = "/") -> list[str]:
    """Reject programmatically supplied NaN/Infinity before schema evaluation."""
    errors: list[str] = []
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"{path}: non-finite JSON number is forbidden")
    elif isinstance(value, dict):
        for key, item in value.items():
            errors.extend(finite_errors(item, f"{path.rstrip('/')}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(finite_errors(item, f"{path.rstrip('/')}/{index}"))
    return errors


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value)))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported JSON Schema type {expected!r}")


def check_schema_vocabulary(schema: Any, path: str = "#") -> list[str]:
    if isinstance(schema, bool):
        return []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object or boolean"]
    errors = [f"{path}: unsupported schema keyword {key!r}" for key in schema if key not in SUPPORTED]
    for key in ("properties", "$defs"):
        value = schema.get(key)
        if isinstance(value, dict):
            for name, child in value.items():
                errors.extend(check_schema_vocabulary(child, f"{path}/{key}/{name}"))
    for key in ("items", "additionalProperties", "not", "if", "then", "else"):
        value = schema.get(key)
        if isinstance(value, (dict, bool)):
            errors.extend(check_schema_vocabulary(value, f"{path}/{key}"))
    for key in ("allOf", "oneOf", "anyOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for index, child in enumerate(value):
                errors.extend(check_schema_vocabulary(child, f"{path}/{key}/{index}"))
    return errors


def pointer(root: Any, fragment: str) -> Any:
    if fragment in ("", "#"):
        return root
    raw = fragment[1:] if fragment.startswith("#") else fragment
    if not raw.startswith("/"):
        raise ValueError(f"unsupported non-pointer schema fragment {fragment!r}")
    node = root
    for part in raw[1:].split("/"):
        part = urllib.parse.unquote(part).replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


@dataclass(frozen=True)
class SchemaDoc:
    schema: dict[str, Any]
    base: str


class Context:
    def __init__(self, root_path: pathlib.Path):
        self.by_uri: dict[str, SchemaDoc] = {}
        for path in root_path.resolve().parent.rglob("*.schema.json"):
            value = strict_loads(path.read_text(encoding="utf-8"), source=str(path))
            if not isinstance(value, dict):
                raise ValueError(f"{path}: root schema must be an object")
            uri = path.resolve().as_uri()
            base = value.get("$id") if isinstance(value.get("$id"), str) else uri
            doc = SchemaDoc(value, base)
            self.by_uri[uri] = doc
            self.by_uri[base] = doc
        root_uri = root_path.resolve().as_uri()
        if root_uri not in self.by_uri:
            raise ValueError(f"schema is outside discoverable schema tree: {root_path}")
        self.root = self.by_uri[root_uri]

    def resolve(self, ref: str, current: SchemaDoc) -> tuple[Any, SchemaDoc]:
        if ref.startswith("#"):
            return pointer(current.schema, ref), current
        absolute = urllib.parse.urljoin(current.base, ref)
        uri, fragment = urllib.parse.urldefrag(absolute)
        doc = self.by_uri.get(uri)
        if doc is None:
            raise ValueError(f"unresolved external schema reference {absolute!r}")
        return pointer(doc.schema, "#" + fragment if fragment else "#"), doc


def validate(instance: Any, schema: Any, context: Context, document: SchemaDoc,
             path: str = "", errors: list[str] | None = None) -> list[str]:
    if errors is None:
        errors = []
    where = path or "/"
    if schema is True:
        return errors
    if schema is False:
        errors.append(f"{where}: rejected by false schema")
        return errors
    if not isinstance(schema, dict):
        errors.append(f"{where}: malformed schema node")
        return errors
    if "$ref" in schema:
        target, target_doc = context.resolve(schema["$ref"], document)
        validate(instance, target, context, target_doc, path, errors)
        schema = {key: value for key, value in schema.items() if key != "$ref"}
    for child in schema.get("allOf", []):
        validate(instance, child, context, document, path, errors)
    if "oneOf" in schema:
        count = sum(not validate(instance, child, context, document, path, [])
                    for child in schema["oneOf"])
        if count != 1:
            errors.append(f"{where}: expected exactly one oneOf branch, matched {count}")
    if "anyOf" in schema and not any(
            not validate(instance, child, context, document, path, []) for child in schema["anyOf"]):
        errors.append(f"{where}: did not match any anyOf branch")
    if "not" in schema and not validate(instance, schema["not"], context, document, path, []):
        errors.append(f"{where}: matched forbidden not schema")
    if "if" in schema:
        matched = not validate(instance, schema["if"], context, document, path, [])
        branch = schema.get("then") if matched else schema.get("else")
        if branch is not None:
            validate(instance, branch, context, document, path, errors)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(type_ok(instance, expected) for expected in types):
            errors.append(f"{where}: expected type {'|'.join(types)}, got {type_name(instance)}")
            return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} is not one of {schema['enum']!r}")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{where}: {instance!r} != const {schema['const']!r}")
    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{where}: {instance[:60]!r} does not match /{schema['pattern']}/")
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{where}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{where}: longer than maxLength {schema['maxLength']}")
        if schema.get("format") == "date-time":
            try:
                if DT_RE.fullmatch(instance) is None:
                    raise ValueError
                dt.datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{where}: {instance!r} is not an RFC 3339 date-time")
        if schema.get("format") == "date":
            try:
                dt.date.fromisoformat(instance)
            except ValueError:
                errors.append(f"{where}: {instance!r} is not an ISO date")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{where}: {instance!r} < minimum {schema['minimum']!r}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{where}: {instance!r} > maximum {schema['maximum']!r}")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(f"{where}: missing required property {required!r}")
        if len(instance) < schema.get("minProperties", 0):
            errors.append(f"{where}: fewer than minProperties {schema['minProperties']}")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append(f"{where}: more than maxProperties {schema['maxProperties']}")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child_path = f"{path}/{key}"
            if key in properties:
                validate(value, properties[key], context, document, child_path, errors)
            elif additional is False:
                errors.append(f"{where}: unexpected property {key!r}")
            elif isinstance(additional, (dict, bool)):
                validate(value, additional, context, document, child_path, errors)
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{where}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{where}: more than maxItems {schema['maxItems']}")
        if "items" in schema:
            for index, value in enumerate(instance):
                validate(value, schema["items"], context, document, f"{path}/{index}", errors)
        if schema.get("uniqueItems"):
            encoded = [json.dumps(value, sort_keys=True, ensure_ascii=False) for value in instance]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{where}: array items are not unique")
    return errors


def errors_for_value(instance: Any, schema_path: pathlib.Path) -> list[str]:
    nonfinite = finite_errors(instance)
    if nonfinite:
        return nonfinite
    context = Context(schema_path)
    documents = {id(document): document for document in context.by_uri.values()}.values()
    vocabulary_errors = [error for document in documents
                         for error in check_schema_vocabulary(document.schema)]
    if vocabulary_errors:
        return vocabulary_errors
    return validate(instance, context.root.schema, context, context.root)


def errors_for(instance_path: pathlib.Path, schema_path: pathlib.Path) -> list[str]:
    instance = strict_loads(instance_path.read_text(encoding="utf-8"), source=str(instance_path))
    return errors_for_value(instance, schema_path)


def controls() -> int:
    schema = {
        "type": "object", "additionalProperties": False, "required": ["id", "items"],
        "properties": {
            "id": {"type": "string", "pattern": "^F[1-9][0-9]*$"},
            "items": {"type": "array", "minItems": 1, "maxItems": 2,
                      "uniqueItems": True, "items": {"type": "integer"}},
        },
    }
    doc = SchemaDoc(schema, "control://schema")
    context = object.__new__(Context)
    context.by_uri = {doc.base: doc}
    context.root = doc
    good = {"id": "F1", "items": [1, 2]}
    bad = [
        {}, {"id": "1", "items": [1]}, {"id": "F1", "items": []},
        {"id": "F1", "items": [1, 1]}, {"id": "F1", "items": [1, 2, 3]},
        {"id": "F1", "items": [True]}, {"id": "F1", "items": [1], "extra": True},
    ]
    if validate(good, schema, context, doc):
        print("  FAIL: validator rejects its known-good control")
        return 1
    accepted = [index for index, item in enumerate(bad, 1) if not validate(item, schema, context, doc)]
    if accepted:
        print(f"  FAIL: validator accepted known-bad controls {accepted}")
        return 1
    if not check_schema_vocabulary({"type": "string", "unknownKeyword": True}):
        print("  FAIL: validator did not reject an unsupported schema keyword")
        return 1
    for raw, label in (
        ('{"id":"F1","id":"F2","items":[1]}', "duplicate object key"),
        ('{"id":"F1","items":[NaN]}', "NaN"),
        ('{"id":"F1","items":[Infinity]}', "Infinity"),
    ):
        try:
            strict_loads(raw, source=label)
        except ValueError:
            pass
        else:
            print(f"  FAIL: strict parser accepted {label}")
            return 1
    if not finite_errors({"value": float("nan")}):
        print("  FAIL: in-memory non-finite number was accepted")
        return 1
    print(f"  ok: bundled validator controls ({len(bad)} bad instances and unknown vocabulary rejected)")
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--controls":
        return controls()
    if len(sys.argv) != 3:
        print(f"usage: {pathlib.Path(sys.argv[0]).name} INSTANCE.json SCHEMA.json", file=sys.stderr)
        return 2
    instance_path, schema_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    try:
        errors = errors_for(instance_path, schema_path)
    except Exception as exc:
        print(f"  FAIL: validator could not complete: {exc}")
        return 1
    if errors:
        print(f"  FAIL: {instance_path.name} does not validate against {schema_path.name} ({len(errors)} errors):")
        for error in errors[:40]:
            print(f"     {error}")
        if len(errors) > 40:
            print(f"     ... and {len(errors) - 40} more")
        return 1
    print(f"  ok: {instance_path.name} validates against {schema_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
