"""AnytypeClient tests against a real local aiohttp.web server (via
aiohttp.test_utils), not a third-party HTTP-mocking library.

aioresponses was tried first but monkeypatches aiohttp's ClientResponse
constructor, which breaks on every aiohttp release that touches that
signature (it already had on the aiohttp version this repo pins). Driving
requests through a real, tiny aiohttp server instead only depends on
aiohttp's own stable public API, so it can't lag behind aiohttp itself.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hermes_anytype.anytype_client import (
    AnytypeAPIError,
    AnytypeClient,
    AnytypeConfig,
    parse_sse_lines,
)


class FakeAnytypeServer:
    """Records requests and serves pre-registered canned responses."""

    def __init__(self):
        self.requests: list[dict] = []
        self._responses: dict[tuple[str, str], tuple[int, dict]] = {}
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._app = app
        self._server: TestServer | None = None
        self.base_url = ""

    def set_response(self, method: str, path: str, *, status: int = 200, payload: dict) -> None:
        self._responses[(method.upper(), path)] = (status, payload)

    async def _handle(self, request: web.Request) -> web.Response:
        raw_body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "headers": dict(request.headers),
                "body": json.loads(raw_body) if raw_body else None,
            }
        )
        key = (request.method, request.path)
        if key not in self._responses:
            return web.json_response({"error": f"no mock for {key}"}, status=500)
        status, payload = self._responses[key]
        return web.json_response(payload, status=status)

    async def start(self) -> None:
        self._server = TestServer(self._app)
        await self._server.start_server()
        self.base_url = str(self._server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()


@pytest.fixture
async def fake_server():
    server = FakeAnytypeServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def client(fake_server):
    config = AnytypeConfig(api_key="test-key", base_url=fake_server.base_url, space_id="space1")
    async with AnytypeClient(config) as c:
        yield c


async def test_search_sends_query_and_returns_data(client, fake_server):
    fake_server.set_response(
        "POST", "/v1/spaces/space1/search", payload={"data": [{"id": "obj1"}]}
    )

    results = await client.search("roadmap", types=["task"])

    assert results == [{"id": "obj1"}]
    request = fake_server.requests[0]
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["headers"]["Anytype-Version"] == "2025-11-08"
    assert request["body"] == {"query": "roadmap", "types": ["task"]}


async def test_create_object_posts_expected_payload(client, fake_server):
    fake_server.set_response(
        "POST",
        "/v1/spaces/space1/objects",
        payload={"id": "obj1", "name": "Ship the thing"},
    )

    result = await client.create_object(type_key="task", name="Ship the thing")

    assert result["id"] == "obj1"
    assert fake_server.requests[0]["body"] == {"type_key": "task", "name": "Ship the thing"}


async def test_raises_on_error_status(client, fake_server):
    fake_server.set_response(
        "GET",
        "/v1/spaces/space1/types/bogus",
        status=404,
        payload={"error": "type not found"},
    )

    with pytest.raises(AnytypeAPIError) as exc_info:
        await client.get_type("bogus")

    assert exc_info.value.status_code == 404
    assert "type not found" in str(exc_info.value)


async def test_add_chat_message_includes_reply_to(client, fake_server):
    fake_server.set_response(
        "POST", "/v1/spaces/space1/chats/chat1/messages", payload={"id": "msg2"}
    )

    await client.add_chat_message("chat1", text="hi", reply_to_message_id="msg1")

    assert fake_server.requests[0]["body"] == {"text": "hi", "reply_to_message_id": "msg1"}


# -- SSE parsing (no HTTP mocking needed -- see anytype_client.py) ----------


async def _lines(*raw: str):
    for line in raw:
        yield line.encode()


async def test_parse_sse_lines_yields_event_and_data():
    events = [
        event
        async for event in parse_sse_lines(
            _lines(
                "event: message_added",
                'data: {"id": "msg1", "text": "hi"}',
                "",
            )
        )
    ]
    assert events == [{"event": "message_added", "data": {"id": "msg1", "text": "hi"}}]


async def test_parse_sse_lines_defaults_event_name_to_message():
    events = [
        event
        async for event in parse_sse_lines(_lines('data: {"id": "msg1"}', ""))
    ]
    assert events == [{"event": "message", "data": {"id": "msg1"}}]


async def test_parse_sse_lines_ignores_heartbeat_comments():
    events = [
        event
        async for event in parse_sse_lines(
            _lines(":heartbeat", 'data: {"id": "msg1"}', "")
        )
    ]
    assert events == [{"event": "message", "data": {"id": "msg1"}}]


async def test_parse_sse_lines_skips_malformed_json():
    events = [
        event
        async for event in parse_sse_lines(_lines("data: not json", ""))
    ]
    assert events == []


async def test_parse_sse_lines_joins_multiline_data():
    events = [
        event
        async for event in parse_sse_lines(
            _lines("data: {\"id\":", 'data: "msg1"}', "")
        )
    ]
    assert events == [{"event": "message", "data": {"id": "msg1"}}]
