import pytest

from hermes_anytype.tools import PropertyValidationError, create_object, update_object


class FakeClient:
    """Minimal stand-in for AnytypeClient covering just what tools.py touches."""

    def __init__(self, type_info, properties, created=None, updated=None):
        self._type_info = type_info
        self._properties = properties
        self._created = created or {"id": "obj1"}
        self._updated = updated or {"id": "obj1"}
        self.create_object_calls = []
        self.update_object_calls = []

    async def get_type(self, type_key):
        return self._type_info

    async def list_properties(self, type_id=None):
        return self._properties

    async def create_object(self, **kwargs):
        self.create_object_calls.append(kwargs)
        return self._created

    async def update_object(self, object_id, **kwargs):
        self.update_object_calls.append((object_id, kwargs))
        return self._updated


TASK_TYPE = {"id": "type1", "key": "task"}
TASK_PROPERTIES = [
    {"key": "assigned_to", "format": "objects"},
    {"key": "due_date", "format": "date"},
    {"key": "status", "format": "select"},
]


async def test_create_object_normalizes_known_properties():
    client = FakeClient(TASK_TYPE, TASK_PROPERTIES)

    await create_object(
        client, "task", "Ship the thing", {"due_date": "2026-08-20", "status": "opt1"}
    )

    assert len(client.create_object_calls) == 1
    call = client.create_object_calls[0]
    assert call["type_key"] == "task"
    assert call["name"] == "Ship the thing"
    assert {"key": "due_date", "date": "2026-08-20"} in call["properties"]
    assert {"key": "status", "select": "opt1"} in call["properties"]


async def test_create_object_rejects_unknown_property_with_corrective_message():
    client = FakeClient(TASK_TYPE, TASK_PROPERTIES)

    with pytest.raises(PropertyValidationError) as exc_info:
        await create_object(client, "task", "Ship the thing", {"assignee": "bob"})

    message = str(exc_info.value)
    assert "'assignee' not found on type 'task'" in message
    assert "assigned_to" in message
    assert not client.create_object_calls


async def test_update_object_normalizes_and_forwards_object_id():
    client = FakeClient(TASK_TYPE, TASK_PROPERTIES)

    await update_object(client, "obj42", "task", {"assigned_to": ["person1"]})

    assert len(client.update_object_calls) == 1
    object_id, kwargs = client.update_object_calls[0]
    assert object_id == "obj42"
    assert kwargs["properties"] == [{"key": "assigned_to", "objects": ["person1"]}]
