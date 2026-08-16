"""The Anytype chat platform adapter.

The per-chat mention/reply-all filtering logic below is real and testable in
isolation. The actual wiring into Hermes's `ctx.register_platform()` /
background-task primitive is stubbed (`# TODO(confirm-hermes-api)`) pending
design.md §11 next-step #2 — confirming the literal method signatures against
Hermes's plugin-authoring docs. Don't guess at that surface; get it from the
docs, then fill in the TODOs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from .anytype_client import AnytypeClient

logger = logging.getLogger(__name__)

ResponseMode = Literal["mention", "all"]


@dataclass(frozen=True)
class ChannelConfig:
    chat_id: str
    mode: ResponseMode = "mention"
    trigger: str = "@hermes"


def should_respond(message_text: str, channel: ChannelConfig) -> bool:
    """Per-chat mention/reply-all filter (design.md §2, §4.1).

    Structural mention detection (via TextMark spans) isn't wired in yet —
    the mark shape needs empirical verification against a running instance
    per design.md §9/§12. Falls back to a case-insensitive substring match.
    """
    if channel.mode == "all":
        return True
    return channel.trigger.lower() in message_text.lower()


class AnytypeGateway:
    """Holds one SSE stream per configured chat and feeds matching messages
    into Hermes's turn pipeline."""

    def __init__(
        self,
        client: AnytypeClient,
        channels: list[ChannelConfig],
        on_message: Callable[[ChannelConfig, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._client = client
        self._channels = channels
        self._on_message = on_message
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        # TODO(confirm-hermes-api): replace this raw asyncio.create_task loop
        # with whatever background-task primitive Hermes's other gateways use
        # internally (design.md §4.1) once that's confirmed.
        for channel in self._channels:
            self._tasks.append(asyncio.create_task(self._run_channel(channel)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_channel(self, channel: ChannelConfig) -> None:
        backoff = 1.0
        max_backoff = 60.0
        while True:
            try:
                async for event in self._client.stream_chat_messages(channel.chat_id):
                    backoff = 1.0  # reset on a good connection
                    if event["event"] != "message_added":
                        continue
                    message = event["data"]
                    text = message.get("text", "")
                    if not should_respond(text, channel):
                        continue
                    await self._on_message(channel, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Anytype SSE stream for chat %s dropped, reconnecting in %.0fs",
                    channel.chat_id,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def reply(
        self, channel: ChannelConfig, text: str, *, reply_to_message_id: str | None = None
    ) -> None:
        await self._client.add_chat_message(
            channel.chat_id, text=text, reply_to_message_id=reply_to_message_id
        )
