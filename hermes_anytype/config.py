"""Config loading/validation (design.md §3). Fails loud on bad/missing values —
never silently no-ops (design.md §6)."""

from __future__ import annotations

from dataclasses import dataclass

from .anytype_client import AnytypeConfig
from .gateway import ChannelConfig


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PluginConfig:
    anytype: AnytypeConfig
    channels: list[ChannelConfig]


def load_config(raw: dict) -> PluginConfig:
    try:
        section = raw["anytype"]
    except KeyError as exc:
        raise ConfigError("missing top-level 'anytype' config section") from exc

    for required in ("api_key", "api_base_url", "space_id"):
        if not section.get(required):
            raise ConfigError(f"'anytype.{required}' is required and cannot be empty")

    anytype_config = AnytypeConfig(
        api_key=section["api_key"],
        base_url=section["api_base_url"],
        space_id=section["space_id"],
    )

    channels: list[ChannelConfig] = []
    for entry in section.get("channels", []):
        if not entry.get("chat_id"):
            raise ConfigError("each entry in 'anytype.channels' requires a 'chat_id'")
        mode = entry.get("mode", "mention")
        if mode not in ("mention", "all"):
            raise ConfigError(
                f"'anytype.channels[].mode' must be 'mention' or 'all', got {mode!r}"
            )
        channels.append(
            ChannelConfig(
                chat_id=entry["chat_id"],
                mode=mode,
                trigger=entry.get("trigger", "@hermes"),
            )
        )

    return PluginConfig(anytype=anytype_config, channels=channels)
