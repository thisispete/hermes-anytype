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
# Confirmed live (beta round 16): the SSE connection can go silently dead
# -- no exception on either end, ever, at intervals ranging from ~90s to
# 8+ minutes across repeated tests -- leaving _run_chat's own reconnect
# logic never triggered, since it depends on an exception that never
# comes. Rather than chase the exact transport-level cause further, this
# is a activity-based watchdog independent of it: anytype_client.py's
# stream_chat_messages now yields a real event for every heartbeat
# comment line too (previously silently swallowed), so genuine silence --
# not even a heartbeat -- for this long is itself the failure signal,
# regardless of whether anything ever raises. Must stay well below
# aiohttp's own sock_read timeout (heartbeat_seconds * 3 = 90s default,
# anytype_client.py) so this watchdog is the one that actually fires.
# 2x the 30s heartbeat default -- if that default changes, this should
# move with it.
_STREAM_ACTIVITY_TIMEOUT = 60.0


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
        for task in self._tasks:
            task.add_done_callback(self._log_run_chat_done)
        return True

    def _log_run_chat_done(self, task: asyncio.Task) -> None:
        """Surface an unexpected _run_chat exit that would otherwise be
        silent (beta round 18): _run_chat is supposed to loop until
        disconnect() cancels it. If it ever ends any other way -- an
        uncaught exception, or a cancellation nobody requested -- nothing
        else observes that task's result, so by default asyncio logs a
        cancellation as nothing at all and an exception only via its own
        default handler (which this deployment wasn't capturing). That
        made a real, 100%-reproducible bug (get_chat_messages returning
        the wrong shape, see anytype_client.py) look exactly like an
        indefinite silent hang for days. This is the fix for the next one.
        """
        if self._closing:
            return  # expected: disconnect() cancelled it
        if task.cancelled():
            logger.error("Anytype: _run_chat task for a chat ended via unexpected cancellation")
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Anytype: _run_chat task for a chat died with an uncaught exception", exc_info=exc)

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
        # Confirmed live (beta round 14, self-reply loop): the real
        # add-chat-message response is {"message_id": "..."}, not {"id":
        # "..."} -- result.get("id") always returned None, so
        # _remember_sent() never actually recorded anything, and the
        # dedup check in _handle_anytype_message could never match a
        # single self-sent message. With ANYTYPE_REQUIRE_MENTION=false
        # this produced an immediate infinite self-reply loop (every
        # reply looked like a new incoming message eligible for another
        # reply); with mention-gating on it was the same latent bug, just
        # far less likely to trigger since a reply rarely re-contains the
        # trigger string or a self-referencing structural mention.
        message_id = result.get("message_id") if isinstance(result, dict) else None
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
            reconnect_reason: str | None = None
            stream = self.client.stream_chat_messages(chat_id).__aiter__()
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(
                            stream.__anext__(), timeout=_STREAM_ACTIVITY_TIMEOUT
                        )
                    except StopAsyncIteration:
                        reconnect_reason = "stream ended"
                        break
                    except asyncio.TimeoutError:
                        # Confirmed live (beta round 16): a dead connection
                        # can produce zero log output and never raise on
                        # either end, at intervals from ~90s to 8+ minutes
                        # across repeated tests -- the exception-based
                        # reconnect logic below never triggers because
                        # nothing ever raises into it. This is independent
                        # of that: genuine silence, not even a heartbeat
                        # (see anytype_client.py's parse_sse_lines, which
                        # now yields one instead of swallowing it), for
                        # _STREAM_ACTIVITY_TIMEOUT is itself the failure
                        # signal, regardless of whether the underlying
                        # transport ever tells us anything went wrong.
                        reconnect_reason = (
                            f"no activity (not even a heartbeat) for "
                            f"{_STREAM_ACTIVITY_TIMEOUT:.0f}s"
                        )
                        break
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
                # Confirmed live (beta round 16): blindly re-raising every
                # CancelledError here was itself a bug, not just a safe
                # default. aiohttp enforces its stream_timeout (anytype_
                # client.py's sock_read=heartbeat_seconds*3) by cancelling
                # whatever task is currently awaiting the read -- that
                # cancellation is *supposed* to get converted to a clean
                # TimeoutError before escaping aiohttp's own timeout context,
                # but a raw CancelledError reaching here instead would look
                # exactly like the silent-death symptom above. A *real*
                # external cancellation (disconnect() calling task.cancel())
                # always sets self._closing = True first -- so that's the
                # one reliable signal to distinguish "actually shutting
                # down, let this propagate" from "something cancelled us
                # internally, this is just a dead connection, treat it like
                # any other reconnectable failure."
                if self._closing:
                    raise
                reconnect_reason = "cancelled unexpectedly (not a real shutdown)"
            except Exception:
                reconnect_reason = "exception"
                logger.exception(
                    "Anytype: SSE stream for chat %s dropped (%s)",
                    chat_id,
                    reconnect_reason,
                )
            finally:
                # Best-effort: force-close the abandoned generator (and the
                # aiohttp response/connection it holds) rather than leaving
                # it for eventual GC, especially important on the timeout
                # path where the underlying connection is exactly the thing
                # suspected of being dead already.
                try:
                    await stream.aclose()
                except Exception:
                    pass
            if reconnect_reason and reconnect_reason != "exception":
                logger.warning(
                    "Anytype: SSE stream for chat %s reconnecting in %.0fs -- %s",
                    chat_id,
                    backoff,
                    reconnect_reason,
                )
            if self._closing:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_DELAY)

    async def _handle_anytype_message(
        self, chat_id: str, message: dict[str, Any], require_mention: bool
    ) -> None:
        message_id = message.get("id")
        if message_id and message_id in self._recently_sent_ids:
            return  # our own message, echoed back over the stream -- not a reply loop
        # Confirmed live (beta round 14, self-reply loop): the sent-id cache
        # above only catches messages sent through THIS adapter's own
        # send(). Hermes core sends some messages through other paths (e.g.
        # the "Interrupting current task..."/"No home channel..." system
        # notifications observed live) that never touch _remember_sent(),
        # so they were never in the cache and kept getting treated as fresh
        # inbound messages from the bot's own account -- an immediate
        # self-sustaining loop once ANYTYPE_REQUIRE_MENTION=false removed
        # the (accidental) protection of the trigger/mention check rarely
        # matching a reply's own text. This is a more fundamental filter:
        # if the message's own author IS this identity, it can never be a
        # real inbound message, full stop, regardless of which path sent
        # it or whether the id cache happened to catch it. `in` rather than
        # `==` matches is_mentioned()'s convention -- creator is a longer
        # "_participant_<space>_<account id>" string, not a bare account id.
        author_id = message.get("creator") or message.get("creator_id") or ""
        if self._account_id and self._account_id in author_id:
            return
        # beta round 13: a fresh SSE connection (including every future
        # reconnect, not just the first) replays the chat's entire message
        # history -- this catches genuine backlog replays of someone
        # else's already-processed messages, distinct from the self-authored
        # case above.
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
        # Confirmed live (beta round 13): gateway/authz_mixin.py's
        # _is_user_authorized() keys its per-platform allowlist/allow-all
        # checks off hardcoded dicts covering only Hermes's built-in
        # platforms -- a third-party plugin platform is never in those
        # dicts. It DOES fall back to the plugin registry for
        # allowed_users_env/allow_all_env, but only if the plugin actually
        # declares them here; without this, every inbound message is
        # unconditionally denied ("Dropping message from unauthorized
        # user... user=None") regardless of mention detection or anything
        # else being correct -- the message never even reaches should_respond.
        allowed_users_env="ANYTYPE_ALLOWED_USERS",
        allow_all_env="ANYTYPE_ALLOW_ALL_USERS",
    )
