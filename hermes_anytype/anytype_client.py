"""Thin async REST + SSE wrapper over the Anytype local HTTP API.

Covers the subset of the API this plugin needs (see documents/design.md §9):
search, type/property introspection, object writes, and chat. Deliberately
dumb — no caching, no retry-with-backoff here (that's layered on top by
callers per the error-handling table in design.md §6).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from httpx_sse import aconnect_sse

ANYTYPE_API_VERSION = "2025-11-08"


class AnytypeAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"Anytype API error {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class AnytypeConfig:
    api_key: str
    base_url: str
    space_id: str


class AnytypeClient:
    """One instance per configured space (see design.md §2: single space per config)."""

    def __init__(self, config: AnytypeConfig, *, timeout: float = 30.0) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Anytype-Version": ANYTYPE_API_VERSION,
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AnytypeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("error", response.text)
            except ValueError:
                payload = None
                message = response.text
            raise AnytypeAPIError(response.status_code, message, payload)
        if not response.content:
            return None
        return response.json()

    # -- Schema introspection --------------------------------------------

    async def list_types(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/types"
        )
        return data.get("data", data)

    async def get_type(self, type_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/types/{type_id}"
        )

    async def list_properties(self, type_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/v1/spaces/{self._config.space_id}/properties"
        params = {"type_id": type_id} if type_id else None
        data = await self._request("GET", path, params=params)
        return data.get("data", data)

    async def get_property(self, property_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/properties/{property_id}"
        )

    # -- Search / objects --------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        types: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"query": query}
        if types:
            body["types"] = types
        if filters:
            body["filters"] = filters
        if sort:
            body["sort"] = sort
        data = await self._request(
            "POST", f"/v1/spaces/{self._config.space_id}/search", json=body
        )
        return data.get("data", data)

    async def create_object(
        self,
        *,
        type_key: str,
        name: str,
        properties: list[dict[str, Any]] | None = None,
        icon: dict[str, Any] | None = None,
        body: str | None = None,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type_key": type_key, "name": name}
        if properties is not None:
            payload["properties"] = properties
        if icon is not None:
            payload["icon"] = icon
        if body is not None:
            payload["body"] = body
        if template_id is not None:
            payload["template_id"] = template_id
        return await self._request(
            "POST", f"/v1/spaces/{self._config.space_id}/objects", json=payload
        )

    async def update_object(
        self, object_id: str, *, properties: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/v1/spaces/{self._config.space_id}/objects/{object_id}",
            json={"properties": properties},
        )

    # -- Chats ---------------------------------------------------------------

    async def list_chats(self) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/v1/spaces/{self._config.space_id}/chats")
        return data.get("data", data)

    async def add_chat_message(
        self,
        chat_id: str,
        *,
        text: str,
        marks: list[dict[str, Any]] | None = None,
        reply_to_message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if marks is not None:
            payload["marks"] = marks
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if attachments is not None:
            payload["attachments"] = attachments
        return await self._request(
            "POST",
            f"/v1/spaces/{self._config.space_id}/chats/{chat_id}/messages",
            json=payload,
        )

    async def get_chat_messages(
        self,
        chat_id: str,
        *,
        before_order_id: str | None = None,
        after_order_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if before_order_id is not None:
            params["before_order_id"] = before_order_id
        if after_order_id is not None:
            params["after_order_id"] = after_order_id
        if limit is not None:
            params["limit"] = limit
        data = await self._request(
            "GET",
            f"/v1/spaces/{self._config.space_id}/chats/{chat_id}/messages",
            params=params,
        )
        return data.get("data", data)

    async def read_chat_messages(
        self, chat_id: str, *, message_type: str = "messages"
    ) -> None:
        await self._request(
            "POST",
            f"/v1/spaces/{self._config.space_id}/chats/{chat_id}/messages/read",
            json={"type": message_type},
        )

    async def list_members(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/members"
        )
        return data.get("data", data)

    async def stream_chat_messages(
        self, chat_id: str, *, heartbeat_seconds: int = 30
    ) -> AsyncIterator[dict[str, Any]]:
        """Yields decoded SSE events: {"event": "message_added", "data": {...}}.

        Reconnect/backoff is the gateway's responsibility (design.md §6) —
        this just yields events for as long as the connection stays open.
        """
        path = f"/v1/spaces/{self._config.space_id}/chats/{chat_id}/messages/stream"
        headers = {"Anytype-Heartbeat-Seconds": str(heartbeat_seconds)}
        async with aconnect_sse(self._client, "GET", path, headers=headers) as event_source:
            async for sse in event_source.aiter_sse():
                if not sse.data:
                    continue  # heartbeat comment line
                try:
                    data = json.loads(sse.data)
                except json.JSONDecodeError:
                    continue
                yield {"event": sse.event, "data": data}
