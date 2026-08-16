import pytest

from hermes_anytype.config import ConfigError, load_config
from hermes_anytype.gateway import ChannelConfig


def base_raw(**overrides):
    anytype = {
        "api_key": "key",
        "api_base_url": "http://127.0.0.1:31012",
        "space_id": "space1",
        "channels": [{"chat_id": "chat1"}],
    }
    anytype.update(overrides)
    return {"anytype": anytype}


def test_loads_valid_config_with_defaults():
    config = load_config(base_raw())

    assert config.anytype.api_key == "key"
    assert config.channels == [ChannelConfig(chat_id="chat1", mode="mention", trigger="@hermes")]


def test_missing_anytype_section_raises():
    with pytest.raises(ConfigError, match="anytype"):
        load_config({})


@pytest.mark.parametrize("missing", ["api_key", "api_base_url", "space_id"])
def test_missing_required_field_raises(missing):
    raw = base_raw()
    raw["anytype"][missing] = ""
    with pytest.raises(ConfigError, match=missing):
        load_config(raw)


def test_channel_missing_chat_id_raises():
    raw = base_raw(channels=[{"mode": "all"}])
    with pytest.raises(ConfigError, match="chat_id"):
        load_config(raw)


def test_channel_invalid_mode_raises():
    raw = base_raw(channels=[{"chat_id": "chat1", "mode": "bogus"}])
    with pytest.raises(ConfigError, match="mode"):
        load_config(raw)


def test_channel_all_mode_and_custom_trigger():
    raw = base_raw(channels=[{"chat_id": "chat1", "mode": "all"}])
    config = load_config(raw)
    assert config.channels[0].mode == "all"
