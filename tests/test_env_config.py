from hermes_anytype.env_config import env_bool, has_required_config, should_respond, split_csv


def test_split_csv_strips_and_drops_empties():
    assert split_csv("a, b ,, c") == ["a", "b", "c"]
    assert split_csv("") == []
    assert split_csv(None) == []


def test_env_bool_parses_truthy_and_falsy_strings():
    assert env_bool("true", default=False) is True
    assert env_bool("1", default=False) is True
    assert env_bool("yes", default=False) is True
    assert env_bool("false", default=True) is False
    assert env_bool("0", default=True) is False


def test_env_bool_uses_default_when_all_values_unset():
    assert env_bool(None, "", default=True) is True
    assert env_bool(None, default=False) is False


def test_env_bool_prefers_first_set_value():
    assert env_bool("false", "true", default=True) is False


def test_has_required_config():
    assert has_required_config("key", "http://127.0.0.1:31012", "space1") is True
    assert has_required_config("", "http://127.0.0.1:31012", "space1") is False
    assert has_required_config("key", "  ", "space1") is False


def test_should_respond_all_mode_always_true():
    assert should_respond("anything at all", require_mention=False, trigger="@hermes")
    assert should_respond("", require_mention=False, trigger="@hermes")


def test_should_respond_mention_mode_requires_trigger():
    assert should_respond("hey @hermes can you help", require_mention=True, trigger="@hermes")
    assert not should_respond("no trigger here", require_mention=True, trigger="@hermes")


def test_should_respond_mention_mode_is_case_insensitive():
    assert should_respond("Hey @HERMES!", require_mention=True, trigger="@hermes")


def test_should_respond_mention_mode_uses_custom_trigger():
    assert should_respond("!bot status?", require_mention=True, trigger="!bot")
    assert not should_respond("@hermes status?", require_mention=True, trigger="!bot")
