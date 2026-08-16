from hermes_anytype.gateway import ChannelConfig, should_respond


def test_all_mode_always_responds():
    channel = ChannelConfig(chat_id="c1", mode="all")
    assert should_respond("anything at all", channel)
    assert should_respond("", channel)


def test_mention_mode_requires_trigger():
    channel = ChannelConfig(chat_id="c1", mode="mention", trigger="@hermes")
    assert should_respond("hey @hermes can you help", channel)
    assert not should_respond("no trigger here", channel)


def test_mention_mode_is_case_insensitive():
    channel = ChannelConfig(chat_id="c1", mode="mention", trigger="@hermes")
    assert should_respond("Hey @HERMES!", channel)


def test_mention_mode_uses_custom_trigger():
    channel = ChannelConfig(chat_id="c1", mode="mention", trigger="!bot")
    assert should_respond("!bot status?", channel)
    assert not should_respond("@hermes status?", channel)
