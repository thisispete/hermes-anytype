"""LLM-visible tools: search_objects, create_object, update_object, get_type.

Property validation for writes happens here, invisible to the LLM's context
(design.md §4.2) — an HTTP round-trip inside the handler, not a tool call, so
a bad property key costs latency, not a confusing raw API error surfaced to
the model.
"""

from __future__ import annotations

from typing import Any

from .anytype_client import AnytypeClient


class PropertyValidationError(ValueError):
    """Raised when the LLM-supplied properties don't match the type's schema.

    Handlers should catch this and return the message as a corrective
    tool-result error rather than letting it kill the turn (design.md §6).
    """


async def _validate_properties(
    client: AnytypeClient, type_key: str, properties: dict[str, Any]
) -> list[dict[str, Any]]:
    """Look up the type's real property keys and normalize the LLM's input
    into the typed link-value shape the API expects (design.md §9).

    Raises PropertyValidationError with a corrective message on the first
    unknown key, naming what's available so the LLM can retry itself.
    """
    type_info = await client.get_type(type_key)
    known = await client.list_properties(type_id=type_info.get("id", type_key))
    by_key = {prop["key"]: prop for prop in known}

    normalized: list[dict[str, Any]] = []
    for key, value in properties.items():
        prop = by_key.get(key)
        if prop is None:
            available = ", ".join(sorted(by_key)) or "(none defined)"
            raise PropertyValidationError(
                f"'{key}' not found on type '{type_key}'. Available: {available}."
            )
        normalized.append(_to_link_value(prop, value))
    return normalized


def _to_link_value(prop: dict[str, Any], value: Any) -> dict[str, Any]:
    """Wrap a raw value in the {key, <format>} shape create-object expects."""
    fmt = prop["format"]
    field_by_format = {
        "text": "text",
        "number": "number",
        "select": "select",
        "multi_select": "multi_select",
        "date": "date",
        "files": "files",
        "checkbox": "checkbox",
        "url": "url",
        "email": "email",
        "phone": "phone",
        "objects": "objects",
    }
    field = field_by_format.get(fmt, fmt)
    return {"key": prop["key"], field: value}


async def search_objects(
    client: AnytypeClient, query: str, types: list[str] | None = None
) -> list[dict[str, Any]]:
    return await client.search(query, types=types)


async def get_type(client: AnytypeClient, type_key: str) -> dict[str, Any]:
    return await client.get_type(type_key)


async def create_object(
    client: AnytypeClient,
    type_key: str,
    name: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = await _validate_properties(client, type_key, properties or {})
    return await client.create_object(type_key=type_key, name=name, properties=normalized)


async def update_object(
    client: AnytypeClient,
    object_id: str,
    type_key: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    normalized = await _validate_properties(client, type_key, properties)
    return await client.update_object(object_id, properties=normalized)
