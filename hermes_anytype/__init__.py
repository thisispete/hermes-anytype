"""register(ctx) — registers the Anytype platform adapter and its tools.

STUB: the exact `ctx.register_platform()` / `ctx.register_tool()` signatures
below are best-guess, matching the shape design.md describes but NOT verified
against Hermes's actual plugin-authoring API (design.md §11/§12, next-step
#2). Confirm the literal signatures before relying on this to actually load
in a running Hermes instance.
"""

from __future__ import annotations

from typing import Any

from .anytype_client import AnytypeClient
from .config import load_config
from .gateway import AnytypeGateway

__all__ = ["register"]

__version__ = "0.1.0"


def register(ctx: Any) -> None:
    config = load_config(ctx.config)
    client = AnytypeClient(config.anytype)

    # TODO(confirm-hermes-api): confirm register_platform's expected
    # interface (does it want a class instance, a start/stop pair of
    # coroutines, an object implementing some Protocol?) against Hermes's
    # plugin-development docs, then wire AnytypeGateway in for real.
    async def _on_message(channel, message: dict) -> None:
        raise NotImplementedError(
            "wire this into Hermes's turn pipeline once ctx's message-handoff "
            "API is confirmed (design.md §4.1, §12)"
        )

    gateway = AnytypeGateway(client, config.channels, _on_message)
    ctx.register_platform("anytype", gateway)

    from . import tools as tools_module

    for tool_name in ("search_objects", "get_type", "create_object", "update_object"):
        ctx.register_tool(tool_name, getattr(tools_module, tool_name), client=client)
