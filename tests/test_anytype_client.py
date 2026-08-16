import pytest
import respx
from httpx import Response

from hermes_anytype.anytype_client import AnytypeAPIError, AnytypeClient, AnytypeConfig

CONFIG = AnytypeConfig(api_key="test-key", base_url="http://127.0.0.1:31012", space_id="space1")


@pytest.fixture
async def client():
    async with AnytypeClient(CONFIG) as c:
        yield c


@respx.mock
async def test_search_sends_query_and_returns_data(client):
    route = respx.post("http://127.0.0.1:31012/v1/spaces/space1/search").mock(
        return_value=Response(200, json={"data": [{"id": "obj1"}]})
    )

    results = await client.search("roadmap", types=["task"])

    assert results == [{"id": "obj1"}]
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Anytype-Version"] == "2025-11-08"


@respx.mock
async def test_create_object_posts_expected_payload(client):
    route = respx.post("http://127.0.0.1:31012/v1/spaces/space1/objects").mock(
        return_value=Response(200, json={"id": "obj1", "name": "Ship the thing"})
    )

    result = await client.create_object(type_key="task", name="Ship the thing")

    assert result["id"] == "obj1"
    assert route.calls.last.request.content
    import json

    body = json.loads(route.calls.last.request.content)
    assert body == {"type_key": "task", "name": "Ship the thing"}


@respx.mock
async def test_raises_on_error_status(client):
    respx.get("http://127.0.0.1:31012/v1/spaces/space1/types/bogus").mock(
        return_value=Response(404, json={"error": "type not found"})
    )

    with pytest.raises(AnytypeAPIError) as exc_info:
        await client.get_type("bogus")

    assert exc_info.value.status_code == 404
    assert "type not found" in str(exc_info.value)


@respx.mock
async def test_add_chat_message_includes_reply_to(client):
    route = respx.post(
        "http://127.0.0.1:31012/v1/spaces/space1/chats/chat1/messages"
    ).mock(return_value=Response(200, json={"id": "msg2"}))

    await client.add_chat_message("chat1", text="hi", reply_to_message_id="msg1")

    import json

    body = json.loads(route.calls.last.request.content)
    assert body == {"text": "hi", "reply_to_message_id": "msg1"}
