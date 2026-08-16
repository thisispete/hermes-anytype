# hermes-anytype — Design & Roadmap

**Status:** Design approved in conversation, not yet implemented. This document is the source of truth for standing up a new repo.
**License:** MIT
**Repo name:** `hermes-anytype`

## 1. What this is

A Hermes plugin that gives Hermes (a) a live chat presence inside a self-hosted Anytype space, and (b) tools to search/create/update that space's typed objects — both driven by live schema introspection against the Anytype API, so it works against anyone's custom type setup (Person/Company/Task, or anything else) with zero manual mapping. Meant to be a genuine open-source community project, not a personal script: real README, clear config, sane defaults, MIT license.

### Origin / motivation

Built out of a real use case: modeling a job-hunt pipeline (Company → Role → Interview objects, replacing a hand-maintained `job-hunt-status.md` dashboard with a live filtered/grouped view) and wanting an agent that can both talk about that data in Anytype's native chat and keep it organized on your behalf, the way Hermes already does for an Obsidian vault today (see `obsidian-mcp` in the docker media-stack repo for the prior-art pattern this replaces/extends).

## 2. Scope decisions (settled)

These were deliberately chosen over alternatives during design — don't re-litigate without new information:

- **Chat gateway + object/schema tools, bundled in one plugin package.** Not split across two projects, and not dependent on the separate official `anyproto/anytype-mcp` server — this plugin is self-contained.
- **Real OSS polish from day one** — license, README, config validation with clear errors, basic tests. Built so a stranger can self-host it without asking questions.
- **Live introspection, no persisted schema cache.** No config file mapping types → properties that could go stale after a user edits their schema. (See §5 for how this is kept cheap despite being "live every time.")
- **Ships as a genuine Hermes platform-adapter plugin** (`ctx.register_platform()`), not a standalone bridge service. Requires a running Hermes instance; in exchange it gets Hermes's existing tool loop, memory, and retry handling for free. Confirmed Hermes plugins can register a platform adapter *and* tools from the same package in one `register(ctx)` call.
- **Single Anytype space per config.** Multiple spaces = multiple config blocks, not a list-of-spaces schema. Keeps the config and mental model simple.
- **Per-chat response mode, not global.** Each `chat_id` in the space gets its own setting: `mention` (only responds when addressed, e.g. `@hermes`) or `all` (responds to everything). Default is `mention`. This maps directly onto real usage patterns: a solo second-brain space sets its one chat to `all`; a team space sets a shared room to `mention` and a dedicated help room to `all`. Confirmed Anytype has no separate DM primitive — a space just has one or more `chat_id`s (`list-chats`/`create-chat`), so this per-chat config is the only lever needed.
- **Free writes by default.** The agent creates/updates objects autonomously when it decides to (matches how the user already runs `obsidian-mcp` today) — no opt-in write-permission gate for v1.
- **Naming:** `hermes-anytype`.

## 3. Architecture

One Python package, following Hermes's own plugin conventions:

```
hermes_anytype/
├── plugin.yaml
├── __init__.py          # register(ctx) — registers platform + tools
├── gateway.py            # the chat platform adapter (SSE listen, reply post)
├── tools.py               # search_objects / create_object / update_object / get_type
└── anytype_client.py      # thin REST + SSE wrapper over the Anytype API
```

`register(ctx)` calls both `ctx.register_platform("anytype", ...)` and `ctx.register_tool(...)` for each tool in the toolset — one package, two registration surfaces, using Hermes's documented multi-kind plugin support directly.

### Config shape

```yaml
anytype:
  api_key: ${ANYTYPE_API_KEY}
  api_base_url: http://127.0.0.1:31009      # or ANYTYPE_API_BASE_URL for anytype-cli (31012)
  space_id: ${ANYTYPE_SPACE_ID}
  channels:
    - chat_id: "..."
      mode: mention        # or: all
      trigger: "@hermes"   # only used when mode: mention
```

## 4. Components

### 4.1 Gateway (`gateway.py`)

- Holds the SSE stream (`GET /v1/spaces/{space_id}/chats/{chat_id}/messages/stream`) per configured chat.
- **Implementation note:** follow whatever persistent-connection primitive Hermes's other 27+ gateways already use internally (Discord's websocket, Telegram's long-poll are presumably built on a shared background-task primitive inside `register_platform`) — confirm the exact API from Hermes's plugin-authoring reference during implementation rather than inventing a new lifecycle pattern.
- On `message_added`: check that chat's `mode`. If `mention`, look for the trigger — prefer Anytype's structural mention tracking if the message/mark format exposes it (the API has a distinct `mentions` read-state via `read-chat-messages`'s `type` param, suggesting real structural mention support; the exact `TextMark` shape for a mention isn't nailed down in the OpenAPI schema as a strict enum, so **verify empirically against a running instance** before committing to structural-only detection — fall back to substring match on message text if needed). If `all`, always proceed.
- Feeds matching messages into Hermes's normal turn/response pipeline as this platform's inbound message.
- Posts Hermes's reply via `POST /v1/spaces/{space_id}/chats/{chat_id}/messages`, using `reply_to_message_id` for threading.
- **Reconnect:** exponential backoff on SSE drop; resume via cursor-based pagination (`after_order_id`) rather than replaying history.

### 4.2 Tools (`tools.py` + `anytype_client.py`)

- `search_objects(query, types?)` — wraps `POST /v1/spaces/{space_id}/search` (the `types` filter narrows to specific type keys; `type_key` values for the tool's own schema enum are refreshed once per conversation, not per message, not cached to disk).
- `create_object(type_key, name, properties)` / `update_object(object_id, properties)` — **the key correctness design**: the handler internally calls `get-type` / `list-properties` itself, before ever submitting to Anytype, to validate and normalize the `properties` keys the LLM supplied. This lookup is invisible to the LLM's context (an HTTP round-trip inside the handler, not a tool call) — it costs latency, not tokens. If a property key doesn't match, return one corrective tool-result error to the LLM (`"'assignee' not found on type 'task' — did you mean 'assigned_to'? Available: assigned_to, due_date, status."`) instead of forwarding a bad write or raising an exception that kills the turn.
- `get_type(type_key)` — optional, LLM-visible, for cases where the agent wants to explain a schema conversationally. Not required for the write path's correctness (that's handled server-side per above).

This design was chosen specifically to balance two competing concerns raised during brainstorming: **context/token cost** (don't dump full schema into every turn or require a visible discover-then-retry tool-call dance) vs. **risk of the LLM guessing a wrong property key** (don't let bad guesses silently reach Anytype or surface as confusing raw API errors). Pushing validation into the handler, invisible to context, resolves both.

## 5. Data flow

```
Anytype SSE event (message_added)
  → gateway filters by that chat's mode/mention config
  → Hermes turn starts, message as input
  → Hermes's reasoning loop optionally calls tools
      (create_object/update_object validate server-side before writing)
  → Hermes produces reply text
  → gateway posts reply via add-chat-message, threaded via reply_to_message_id
```

## 6. Error handling

| Failure | Behavior |
|---|---|
| SSE disconnect (network blip, Anytype restart) | Exponential-backoff reconnect; resume from `after_order_id` cursor, don't replay history |
| Bad/revoked API key | Fail loud at plugin startup with a clear config error — never silently no-op |
| Property validation mismatch on write | Corrective tool-result error back to the LLM (see §4.2), not an exception |
| Anytype 5xx / rate limit | Bounded retry-with-backoff in `anytype_client.py`, not infinite |

## 7. Testing

- Unit tests for `anytype_client.py` (mocked HTTP) — the REST+SSE wrapper.
- Unit tests for the property-validation/normalization logic in the tool handlers — this is the correctness-critical piece.
- Unit tests for per-chat mention/reply-all filtering logic in the gateway.
- A real self-hosted Anytype instance in CI is likely impractical (needs a running server) — rely on thorough mocking, plus a documented manual verification checklist in the README for contributors rather than fabricated integration coverage.

## 8. Distribution

- MIT license.
- README covering: prerequisites (running Hermes + self-hosted Anytype with API access enabled), install steps (drop into `~/.hermes/plugins/platforms/hermes-anytype/`, or via Hermes's plugin installer if one exists by the time this is built — verify at implementation time), the config example from §3, and a plain-language explanation of mention-mode vs. reply-all mode with the "solo space vs. team room" framing from §2.

## 9. Technical reference (verified against the live Anytype API spec, 2025-11-08 version)

Captured here so implementation doesn't have to re-derive this from scratch.

**Property (relation) formats** — `create-property` request schema, `format` enum:
`text, number, select, multi_select, date, files, checkbox, url, email, phone, objects`.
`objects` is the object-to-object relation type — its value is a list of other objects' IDs (`ObjectsPropertyLinkValue`: `{key, objects: [id, ...]}`). This is the mechanism for typed relations (e.g. a Role's `company` property, a Person's `mother`/`siblings`).

**Object creation** — `create-object` takes `type_key`, `name`, `icon`, `body`, `template_id`, and a `properties` array of typed link-value objects (`TextPropertyLinkValue`, `NumberPropertyLinkValue`, `SelectPropertyLinkValue`, `MultiSelectPropertyLinkValue`, `DatePropertyLinkValue`, `FilesPropertyLinkValue`, `ObjectsPropertyLinkValue`, etc. — one `oneOf` variant per format).

**Schema introspection** — `list-types` / `get-type` (`GET /v1/spaces/{space_id}/types/{type_id}`) return a type's key, name, icon, layout — explicitly documented as existing to "assist clients in understanding the expected structure... for objects of that type." `list-properties` / `get-property` do the equivalent for relations.

**Search** — `search-space` (`POST /v1/spaces/{space_id}/search`) takes `query`, `filters`, `sort`, and a `types` array to filter results by type key.

**Chat endpoints**, all under `/v1/spaces/{space_id}/chats/{chat_id}/`:
- `list-chats` / `create-chat` — a space can have multiple chats; no separate DM primitive exists.
- `POST /messages` (add-chat-message) — body: `text` (required), `marks` (rich-text formatting spans), `reply_to_message_id` (threading), `attachments` (can target an object ID directly — a message can carry a real embedded object reference, not just a link).
- `GET /messages/stream` (chat-message-stream) — Server-Sent Events: `message_added`, `message_updated`, `message_deleted`, `reactions_updated`. Heartbeat comment lines keep the connection alive; tunable via `Anytype-Heartbeat-Seconds` header (1–60s, default 30s).
- `GET /messages` (get-chat-messages) — cursor-based pagination via `before_order_id`/`after_order_id`.
- `GET /messages/search` (search-chat-messages) — full-text search over chat history.
- `POST /messages/read` (read-chat-messages) — marks messages read; `type` param defaults to `"messages"`, can be set to `"mentions"` specifically — confirms Anytype tracks @-mentions as a first-class read-state, not just a text convention. The exact `TextMark` type string for a mention wasn't pinned down as a strict enum in the spec — verify empirically.
- `list-members` / `get-member` — space membership, useful for resolving a human display name to a member ID if needed.

**Auth / connection:** API key generated in Anytype's app settings (API Keys section); default local base URL `http://127.0.0.1:31009` (or `31012` for `anytype-cli`). MCP/API version pinned via an `Anytype-Version` header (e.g. `2025-11-08`).

## 10. Hermes's Anytype identity (bot account setup)

Open question from §9 that turned out to matter architecturally: **Anytype has no bot-token concept.** Unlike Slack/Discord, there's no way to mint a credential scoped *inside* an existing account. Every space participant is a full any-sync identity (own keypair, own local node). The REST API key from the challenge flow (`POST /v1/auth/challenges` → `POST /v1/auth/api_keys`) just authenticates to whichever account is running the local node at the base URL you point at — it does not create an identity. **If `api_key` is minted against the user's own desktop Anytype app, every Hermes message posts under the human's own member profile, not "Hermes."**

To give Hermes a distinct profile, it needs its own account, running its own node, invited into the space as its own member. `anyproto/anytype-cli` is built for exactly this ("bot accounts") — it embeds `anytype-heart` headless, no desktop app required:

1. Run the CLI as its own headless node: `anytype serve` (or `anytype service install && anytype service start` to persist it).
2. `anytype auth create hermes` — mints a new account key (account-key auth only, no mnemonic login). This is the identity that becomes "Hermes." If self-hosted (not the default anytype.io network), pass `--network-config` pointing at the *same* any-sync deployment the human's primary space lives on, or the bot account lands on an unreachable network.
3. `anytype auth apikey create hermes-integration` — mints the API key scoped to *this* identity/node. This is `ANYTYPE_API_KEY`; `api_base_url` is this node's own local port (CLI default `31012`), not the human's desktop app's `31009`.
4. From the human's desktop app: Share Space → generate an invite link (approval-required by default; owner can enable auto-approve but Anytype's own docs advise against it for sensitive spaces).
5. `anytype space join "<invite-link>"` run against the bot identity, then the human approves the join request in-app. Will likely need explicit promotion to Editor role to match the "free writes by default" decision in §2 — default post-join role isn't confirmed.

**Unverified, needs empirical check before finalizing setup docs:** whether `auth create hermes` makes "Hermes" the actual display name/avatar shown in chat, or whether that has to be set separately once logged in as that identity. Same category of "verify against a running instance" caveat as the mention-mark shape in §9.

This doesn't change the config shape in §3 — still one `api_key`/`api_base_url` pair — but adds a real manual prerequisite outside the plugin itself: standing up a second, bot-owned Anytype node before Hermes can appear as itself rather than as the human. Belongs in the README's prerequisites section (§8).

## 11. Alternatives considered (context, not decisions)

Captured for whoever picks this up later, since they were seriously discussed:

- **Claude-first via MCP instead of Hermes.** The object/schema capability layer already exists as an official server (`anyproto/anytype-mcp`) — client-agnostic, works with Claude Desktop (local MCP), claude.ai Connectors (remote MCP behind Caddy, same pattern as every other service in the docker media-stack), or Claude Code under a Pro/Max login — all without metered API billing. The one thing none of those paths can do is proactive/reactive chat presence (something has to sit listening and call an LLM on its own initiative, which structurally requires a metered API-backed process) — that's the actual reason a Hermes-side gateway is still worth building, not just inertia. Likely end state: both — Claude/Connectors for on-demand pull-based use, this plugin for the ambient/reactive chat presence Claude structurally can't provide under a subscription model.
- **Retiring Hermes entirely.** Partially reconsidered mid-design. Claude Code now has native scheduled-task support (a real replacement for simple cron-style recurring prompts), but Hermes's Discord-gateway model still provides ambient cross-device context continuity (same conversation reachable from any device with zero setup) that Claude Code's current session/machine-bound model doesn't fully replicate — claude.ai syncs conversational context across devices, but Claude Code sessions tied to a specific machine's filesystem (like this docker host) still require deliberate remote setup (SSH, or Anthropic's newer Remote Control / cloud-session mechanism) rather than being automatic. Not resolved — flagged as an open question, not blocking this project.

## 12. Next steps

1. Bootstrap the actual repo (`git init`, `LICENSE` (MIT), this file as `docs/design.md` or `ROADMAP.md`, standard `.gitignore` for Python).
2. Confirm Hermes's exact plugin-authoring API surface (the precise signatures for `ctx.register_platform()` / `ctx.register_tool()`, and the background-task primitive gateways use for persistent connections) against Hermes's current plugin-development docs — the design above is correct in shape but implementation needs the literal method signatures.
3. Spin up a self-hosted Anytype instance to verify the mention-mark shape empirically (§4.1, §9) before finalizing mention-detection logic.
4. Stand up the actual Hermes bot identity via `anytype-cli` per §10, and verify empirically whether the account name flows through as the space's display name, or needs setting separately.
5. Once verified, this doc is ready to hand to the `writing-plans` process for a step-by-step implementation plan.