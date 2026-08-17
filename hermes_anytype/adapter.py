"""The Anytype platform adapter -- the real Hermes gateway integration.

Confirmed against Hermes's own `gateway/platforms/base.py` and the shipped
Mattermost/IRC plugins in NousResearch/hermes-agent's `plugins/platforms/`
(see docs/design.md Section 12 for how that was verified). This supersedes the
earlier stubbed `gateway.py` -- the register(ctx)/BasePlatformAdapter shapes
here are the real, confirmed API, not a best guess.

Third-party plugins that were never added to Hermes core's `Platform` enum
get a dynamic pseudo-member via `Platform(name)` -- this only works because
`Platform._missing_()` checks `platform_registry.is_registered(name)`, which
is already true by the time this adapter is constructed (`register()` below
calls `register_platform` first, and `adapter_factory` is only invoked
after). Mirrors `plugins/platforms/irc/adapter.py`'s `Platform("irc")`
pattern exactly -- that's the sanctioned template for third-party platforms.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

from .anytype_client import AnytypeClient, AnytypeConfig
from .env_config import (
    env_bool,
    has_required_config,
    parse_message_author,
    should_respond,
    split_csv,
)

logger = logging.getLogger(__name__)

_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_SENT_ID_CACHE_SIZE = 500
_SEEN_ID_CACHE_SIZE = 500
# How many of a chat's existing messages to fetch when priming the
# already-seen set (see _run_chat's priming step, docs/design.md Section
# 4.1, beta round 13). A confirmed real API behavior: a fresh SSE
# connection replays the chat's entire message history as message_added
# events, not just genuinely new ones -- without priming, that replay
# (including on every future reconnect, not just the first connection)
# would be reprocessed as if new every time.
_PRIME_MESSAGE_LIMIT = 200


def check_anytype_requirements() -> bool:
    """aiohttp ships with Hermes core -- see anytype_client.py's module
    docstring for why this plugin deliberately has no extra runtime
    dependency to check for."""
    return True


def validate_anytype_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    api_key = config.token or os.getenv("ANYTYPE_API_KEY", "")
    base_url = extra.get("api_base_url") or os.getenv("ANYTYPE_API_BASE_URL", "")
    space_id = extra.get("space_id") or os.getenv("ANYTYPE_SPACE_ID", "")
    ok = has_required_config(api_key, base_url, space_id)
    if not ok:
        logger.debug(
            "Anytype: ANYTYPE_API_KEY/ANYTYPE_API_BASE_URL/ANYTYPE_SPACE_ID not fully set"
        )
    return ok


class AnytypeAdapter(BasePlatformAdapter):
    """Gateway adapter for a self-hosted Anytype space's native chat.

    Env-var configuration (see plugin.yaml):
        ANYTYPE_API_KEY, ANYTYPE_API_BASE_URL, ANYTYPE_SPACE_ID  (required)
        ANYTYPE_CHATS               comma-separated chat_ids to monitor;
                                     unset = auto-discover all chats in the
                                     space at connect time
        ANYTYPE_REQUIRE_MENTION     "true"/"false", default true
        ANYTYPE_MENTION_TRIGGER     default "@hermes"
        ANYTYPE_FREE_RESPONSE_CHATS comma-separated chat_ids that respond to
                                     everything regardless of REQUIRE_MENTION
                                     (mirrors MATTERMOST_FREE_RESPONSE_CHANNELS)
        ANYTYPE_ACCOUNT_ID          this identity's own Anytype account id
                                     (printed during `anytype auth create` --
                                     see docs/design.md Section 10). Needed
                                     for structural mention detection
                                     (env_config.is_mentioned) -- without it,
                                     mention mode falls back to trigger-text
                                     substring matching only, which misses
                                     real UI-driven @mentions whenever the
                                     bot's display name isn't literally the
                                     trigger string (confirmed live, beta
                                     round 11, Section 4.1).
    """

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        platform = Platform("anytype")
        super().__init__(config, platform)

        extra = getattr(config, "extra", {}) or {}
        api_key = (config.token or os.getenv("ANYTYPE_API_KEY", "")).strip()
        base_url = (extra.get("api_base_url") or os.getenv("ANYTYPE_API_BASE_URL", "")).strip()
        space_id = (extra.get("space_id") or os.getenv("ANYTYPE_SPACE_ID", "")).strip()
        self.client = AnytypeClient(
            AnytypeConfig(api_key=api_key, base_url=base_url, space_id=space_id)
        )

        self._configured_chat_ids = split_csv(
            extra.get("chats") or os.getenv("ANYTYPE_CHATS")
        )
        self._require_mention = env_bool(
            extra.get("require_mention"), os.getenv("ANYTYPE_REQUIRE_MENTION"), default=True
        )
        self._trigger = extra.get("mention_trigger") or os.getenv(
            "ANYTYPE_MENTION_TRIGGER", "@hermes"
        )
        self._free_response_chats = set(
            split_csv(extra.get("free_response_chats") or os.getenv("ANYTYPE_FREE_RESPONSE_CHATS"))
        )
        self._account_id = (
            extra.get("account_id") or os.getenv("ANYTYPE_ACCOUNT_ID", "")
        ).strip() or None

        self._tasks: list[asyncio.Task] = []
        self._recently_sent_ids: list[str] = []
        self._seen_incoming_ids: list[str] = []
        self._closing = False

    # ------------------------------------------------------------------
    # BasePlatformAdapter contract
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        chat_ids = self._configured_chat_ids
        if not chat_ids:
            try:
                chats = await self.client.list_chats()
                chat_ids = [chat["id"] for chat in chats if chat.get("id")]
            except Exception:
                logger.exception("Anytype: failed to auto-discover chats in space")
                return False
        if not chat_ids:
            logger.warning("Anytype: no chats configured (ANYTYPE_CHATS) or found in space")
            return False

        self._closing = False
        self._tasks = [asyncio.create_task(self._run_chat(chat_id)) for chat_id in chat_ids]
        return True

    async def disconnect(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.client.aclose()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            result = await self.client.add_chat_message(
                chat_id, text=content, reply_to_message_id=reply_to
            )
        except Exception as exc:
            logger.exception("Anytype: failed to send message to chat %s", chat_id)
            return SendResult(success=False, error=str(exc))
        message_id = result.get("id") if isinstance(result, dict) else None
        if message_id:
            self._remember_sent(message_id)
        return SendResult(success=True, message_id=message_id, raw_response=result)

    async def send_typing(self, chat_id: str) -> None:
        pass  # Anytype's chat API has no typing-indicator endpoint (docs/design.md Section 9)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        chats = await self.client.list_chats()
        for chat in chats:
            if chat.get("id") == chat_id:
                return {"name": chat.get("name", chat_id), "type": "channel", "chat_id": chat_id}
        return {"name": chat_id, "type": "channel", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _remember_sent(self, message_id: str) -> None:
        self._recently_sent_ids.append(message_id)
        if len(self._recently_sent_ids) > _SENT_ID_CACHE_SIZE:
            self._recently_sent_ids = self._recently_sent_ids[-_SENT_ID_CACHE_SIZE:]

    def _remember_seen(self, message_id: str) -> None:
        self._seen_incoming_ids.append(message_id)
        if len(self._seen_incoming_ids) > _SEEN_ID_CACHE_SIZE:
            self._seen_incoming_ids = self._seen_incoming_ids[-_SEEN_ID_CACHE_SIZE:]

    async def _prime_seen_messages(self, chat_id: str) -> None:
        """Pre-populate the seen-message set from the chat's current
        history before ever opening the live stream (docs/design.md
        Section 4.1, beta round 13) -- confirmed real: a fresh SSE
        connection replays the entire message history as message_added
        events, not just genuinely new ones. Without this, every one of
        those replayed messages -- including on every future reconnect,
        not just the very first connection -- would be reprocessed as if
        it just arrived. Best-effort: if this fails, connect() still
        proceeds (logged, not fatal) since an empty seen-set just means
        the first stream's replay gets processed once, not that the
        adapter can't run at all.
        """
        try:
            messages = await self.client.get_chat_messages(chat_id, limit=_PRIME_MESSAGE_LIMIT)
        except Exception:
            logger.exception(
                "Anytype: failed to prime seen-message set for chat %s -- "
                "its current history may get reprocessed once on first connect",
                chat_id,
            )
            return
        for message in messages:
            message_id = message.get("id")
            if message_id:
                self._remember_seen(message_id)

    async def _run_chat(self, chat_id: str) -> None:
        backoff = _RECONNECT_BASE_DELAY
        require_mention = self._require_mention and chat_id not in self._free_response_chats
        await self._prime_seen_messages(chat_id)
        while not self._closing:
            try:
                async for event in self.client.stream_chat_messages(chat_id):
                    backoff = _RECONNECT_BASE_DELAY  # reset on a good connection
                    if event["event"] != "message_added":
                        continue
                    # Isolated from the stream's own try/except on purpose:
                    # a bug in handling *one* message (an unexpected field
                    # shape, say) shouldn't force a full SSE reconnect for
                    # the whole chat. Beta round 12 found exactly this --
                    # every message was hitting a real crash here, which
                    # tore down the connection on every single message
                    # instead of just failing that one message loudly.
                    try:
                        await self._handle_anytype_message(chat_id, event["data"], require_mention)
                    except Exception:
                        logger.exception(
                            "Anytype: failed to handle message %s in chat %s",
                            event["data"].get("id"),
                            chat_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Anytype: SSE stream for chat %s dropped, reconnecting in %.0fs",
                    chat_id,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_DELAY)

    async def _handle_anytype_message(
        self, chat_id: str, message: dict[str, Any], require_mention: bool
    ) -> None:
        message_id = message.get("id")
        if message_id and message_id in self._recently_sent_ids:
            return  # our own message, echoed back over the stream -- not a reply loop
        if message_id and message_id in self._seen_incoming_ids:
            return  # already processed -- a replayed/re-delivered message, not a new one
        if message_id:
            self._remember_seen(message_id)
        # Confirmed live (beta round 11, docs/design.md Section 4.1): message
        # text/marks are nested under "content", not top-level fields -- an
        # earlier version of this code read message.get("text") directly,
        # which silently returned "" for every message, in every mode, not
        # just mention mode.
        content = message.get("content") or {}
        text = content.get("text") or ""
        marks = content.get("marks") or []
        if not should_respond(
            text,
            require_mention=require_mention,
            trigger=self._trigger,
            marks=marks,
            account_id=self._account_id,
        ):
            return

        user_id, user_name = parse_message_author(message)
        source = self.build_source(
            chat_id=chat_id,
            chat_type="channel",
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=user_name,
            source=source,
            message_id=message_id,
            reply_to_message_id=message.get("reply_to_message_id"),
        )
        await self.handle_message(event)


# ---------------------------------------------------------------------------
# Plugin registration entry point
# ---------------------------------------------------------------------------


def _build_adapter(config: PlatformConfig) -> AnytypeAdapter:
    return AnytypeAdapter(config)


def register(ctx) -> None:
    """Plugin entry point -- called by the Hermes plugin system."""
    ctx.register_platform(
        name="anytype",
        label="Anytype",
        adapter_factory=_build_adapter,
        check_fn=check_anytype_requirements,
        validate_config=validate_anytype_config,
        required_env=["ANYTYPE_API_KEY", "ANYTYPE_API_BASE_URL", "ANYTYPE_SPACE_ID"],
        install_hint="",
        emoji="\U0001f537",
    )
