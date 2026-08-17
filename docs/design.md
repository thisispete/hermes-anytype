# hermes-anytype — Design & Roadmap

**Status:** Implemented against Hermes's confirmed real plugin API (verified against `NousResearch/hermes-agent`'s own shipped Mattermost/IRC plugins — see Section 12). Not yet run against a live Anytype instance.
**License:** MIT
**Repo name:** `hermes-anytype`

## 1. What this is

A Hermes plugin that gives Hermes (a) a live chat presence inside a self-hosted Anytype space, and (b) tools to search/create/update that space's typed objects — both driven by live schema introspection against the Anytype API, so it works against anyone's custom type setup (Person/Company/Task, or anything else) with zero manual mapping. Meant to be a genuine open-source community project, not a personal script: real README, clear config, sane defaults, MIT license. Scope is intentionally bounded rather than exhaustive — see the scope-cutoff calls in Section 6 and Section 12, and the decision not to cross-reference adjacent community projects in Section 4.2.

### Origin / motivation

Built out of a real use case: tracking a multi-stage pipeline of custom object types (e.g. Company → Role → Interview) in place of a hand-maintained markdown dashboard, and wanting an agent that can both talk about that data in Anytype's native chat and keep it organized on your behalf — the same pattern Hermes already applies to other personal knowledge-base platforms via its plugin ecosystem, just extended to Anytype's typed-object model. The live-introspection design (Section 2) means this works against *any* type schema, not just the one that motivated it — Person/Company/Task, or anything else a user has set up.

## 2. Scope decisions (settled)

These were deliberately chosen over alternatives during design — don't re-litigate without new information:

- **Chat gateway + object/schema tools, bundled in one plugin package.** Not split across two projects, and not dependent on the separate official `anyproto/anytype-mcp` server — this plugin is self-contained.
- **Real OSS polish from day one** — license, README, config validation with clear errors, basic tests. Built so a stranger can self-host it without asking questions.
- **Live introspection, no persisted schema cache.** No config file mapping types → properties that could go stale after a user edits their schema. (See Section 5 for how this is kept cheap despite being "live every time.")
- **Ships as a genuine Hermes platform-adapter plugin** (`ctx.register_platform()`), not a standalone bridge service. Requires a running Hermes instance; in exchange it gets Hermes's existing tool loop, memory, and retry handling for free. Confirmed Hermes plugins can register a platform adapter *and* tools from the same package in one `register(ctx)` call.
- **Single Anytype space per config.** Multiple spaces = multiple config blocks, not a list-of-spaces schema. Keeps the config and mental model simple.
- **Per-chat response mode, not global.** Each `chat_id` in the space gets its own setting: `mention` (only responds when addressed, e.g. `@hermes`) or `all` (responds to everything). Default is `mention`. This maps directly onto real usage patterns: a solo second-brain space sets its one chat to `all`; a team space sets a shared room to `mention` and a dedicated help room to `all`. Confirmed Anytype has no separate DM primitive — a space just has one or more `chat_id`s (`list-chats`/`create-chat`), so this per-chat config is the only lever needed.
- **Free writes by default.** The agent creates/updates objects autonomously when it decides to — no opt-in write-permission gate for v1. Revisit if real-world usage shows this needs a confirmation step for destructive edits.
- **Naming:** `hermes-anytype`.

## 3. Architecture

One Python package, following Hermes's own real plugin conventions (confirmed against `plugins/platforms/mattermost/` and `plugins/platforms/irc/` in `NousResearch/hermes-agent` — see Section 12):

```
hermes_anytype/
├── plugin.yaml
├── __init__.py          # register(ctx) — registers platform + tools (Hermes-agnostic import, defers adapter import)
├── adapter.py            # AnytypeAdapter(BasePlatformAdapter) — SSE listen, reply post
├── env_config.py          # pure env/mention-filter logic, deliberately free of any Hermes import (so it's unit-testable standalone)
├── tools.py               # search_objects / create_object / update_object / get_type + Hermes handler wrappers
└── anytype_client.py      # thin REST + SSE wrapper over the Anytype API, built on aiohttp (already bundled with Hermes — see its docstring)
```

`register(ctx)` calls both `ctx.register_platform(name="anytype", ...)` and `ctx.register_tool(...)` for each tool — one package, two registration surfaces, confirmed real (not guessed) against `hermes_cli/plugins.py`'s `PluginContext.register_platform`/`register_tool`.

### Config shape

Confirmed real: Hermes's platform plugins are configured via env vars (`plugin.yaml`'s `requires_env`/`optional_env`), not a nested custom YAML block — the original guess below was wrong in shape, right in spirit. **Caught by real-world install feedback:** `plugin.yaml` also needs a `provides_tools` list naming every tool the package registers (see `plugins/platforms/a2a/plugin.yaml` for the confirmed real pattern) — without it, `hermes doctor` warns, and in some deferred-loading contexts (CLI/TUI, not just the gateway process) the tools may never register at all. Missing this wasn't caught by anything short of an actual install attempt.

```bash
ANYTYPE_API_KEY=...
ANYTYPE_API_BASE_URL=http://127.0.0.1:31012   # Hermes's own bot identity's node, not the user's desktop app
ANYTYPE_SPACE_ID=...

# optional
ANYTYPE_CHATS=                    # comma-separated chat_ids; blank = auto-discover all
ANYTYPE_REQUIRE_MENTION=true
ANYTYPE_MENTION_TRIGGER=@hermes
ANYTYPE_FREE_RESPONSE_CHATS=      # comma-separated chat_ids that ignore REQUIRE_MENTION (mirrors Mattermost's MATTERMOST_FREE_RESPONSE_CHANNELS)
```

## 4. Components

### 4.1 Adapter (`adapter.py`)

`AnytypeAdapter` subclasses `gateway.platforms.base.BasePlatformAdapter`, confirmed against the real base class and the shipped Mattermost/IRC adapters. Only `connect()`, `disconnect()`, and `send()` are actually `@abstractmethod` on the base class; `send_typing()`/`get_chat_info()` are implemented too for good citizenship (the former is a no-op — Anytype's chat API has no typing-indicator endpoint).

- Third-party plugins were never added to Hermes core's `Platform` enum, but that enum has a `_missing_()` hook that creates a dynamic pseudo-member for any name already registered in `platform_registry` — so `Platform("anytype")` works in `__init__` for the same reason `Platform("irc")` works in the real IRC plugin: `register_platform(name="anytype", ...)` has already run by the time the adapter is constructed.
- `connect()` spawns one background `asyncio.Task` per configured chat (from `ANYTYPE_CHATS`, or auto-discovered via `list-chats` if unset), each holding an SSE stream (`GET /v1/spaces/{space_id}/chats/{chat_id}/messages/stream`) with exponential-backoff reconnect on drop.
- On `message_added`: mention/reply-all filtering is a real substring match (`env_config.should_respond`) — structural mention tracking via Anytype's `TextMark` shape was flagged as unverified against a running instance and was never confirmed, so the substring fallback described in the original design is what actually shipped, not a stopgap.
- Matching messages become a `MessageEvent` (built via `self.build_source(...)`) and get handed to Hermes's own turn pipeline via `self.handle_message(event)` — Hermes owns session/auth/turn plumbing entirely past that point.
- Replies go out via `send()` → `POST /v1/spaces/{space_id}/chats/{chat_id}/messages`, using `reply_to_message_id` for threading. Sent message IDs are tracked in a bounded cache so the adapter can filter its own messages back out of the SSE stream (avoids a reply loop) without needing to resolve "my own member ID" from the API.

### 4.2 Tools (`tools.py` + `anytype_client.py`)

Registered as `anytype_search_objects` / `anytype_get_type` / `anytype_create_object` / `anytype_update_object` via `ctx.register_tool(name, toolset, schema, handler, is_async=True, ...)` — confirmed real against `hermes_cli/plugins.py`. Hermes's tool registry dispatches with `handler(args: dict, **kwargs) -> dict`, so each tool's core logic (Hermes-agnostic, independently testable) is wrapped in a thin `handler(args, **kwargs)` closure in `tools.py`'s `make_tool_handlers()`.

- `search_objects(query, types?)` — wraps `POST /v1/spaces/{space_id}/search` (the `types` filter narrows to specific type keys; `type_key` values for the tool's own schema enum are refreshed once per conversation, not per message, not cached to disk).
- `create_object(type_key, name, properties)` / `update_object(object_id, properties)` — **the key correctness design**: the handler internally calls `get-type` / `list-properties` itself, before ever submitting to Anytype, to validate and normalize the `properties` keys the LLM supplied. This lookup is invisible to the LLM's context (an HTTP round-trip inside the handler, not a tool call) — it costs latency, not tokens. If a property key doesn't match, the wrapper catches `PropertyValidationError` and returns `{"error": "'assignee' not found on type 'task'. Available: assigned_to, due_date, status."}` as the tool result instead of forwarding a bad write or raising an exception that kills the turn.
- `get_type(type_key)` — optional, LLM-visible, for cases where the agent wants to explain a schema conversationally. Not required for the write path's correctness (that's handled server-side per above).

**Scope note:** a third-party Hermes skill, `clawhub/anytype` (`Foolafroos/anytype-hermes-skill`), already wraps the official `anyproto/anytype-mcp` server for pull-based object CRUD — meaningful overlap with this section. Deliberately kept independent rather than deferring to it: this plugin doesn't depend on Node.js or a second MCP server process, and the validation design above is more correctness-focused than a generic MCP passthrough. No cross-reference to that skill is maintained in this repo's docs — keeping it in sync would mean ongoing tracking of an external project's changes, which is out of scope here.

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
| SSE disconnect (network blip, Anytype restart) | Exponential-backoff reconnect. **Simpler than originally planned:** reconnects to a fresh live stream rather than resuming from an `after_order_id` cursor -- messages sent during the disconnect window can be missed, but a full history replay is avoided either way. Cursor-based gapless resume is a real enhancement, not implemented (Section 12) -- deprioritized as added complexity beyond this project's current scope (Section 1). |
| Bad/revoked API key | Fail loud at plugin startup with a clear config error — never silently no-op |
| Property validation mismatch on write | Corrective tool-result error back to the LLM (see Section 4.2), not an exception |
| Anytype 5xx / rate limit | Bounded retry-with-backoff in `anytype_client.py`, not infinite |

## 7. Testing

- Unit tests for `anytype_client.py` (mocked HTTP) — the REST+SSE wrapper.
- Unit tests for the property-validation/normalization logic in the tool handlers — this is the correctness-critical piece.
- Unit tests for per-chat mention/reply-all filtering logic in the gateway.
- A real self-hosted Anytype instance in CI is likely impractical (needs a running server) — rely on thorough mocking, plus a documented manual verification checklist in the README for contributors rather than fabricated integration coverage.

## 8. Distribution

- MIT license.
- Confirmed real (not a guess): Hermes's official Docker image (`nousresearch/hermes-agent:latest`) mounts a single host directory → container `/opt/data`, which already covers `plugins/` alongside `skills/`/`sessions/`/etc — so dropping this plugin into `<host-dir>/plugins/` needs zero compose changes. **Caught by real-world install feedback:** the host-side path is whatever the operator's own compose file specifies for that mount — Hermes's own docs example uses `~/.hermes`, but that's just an example, not a guarantee, and this README originally (wrongly) assumed it as fact. Fixed to say "check your own compose file's volume line" instead of asserting a specific path. The image treats its install tree as immutable at runtime ("no lazy installs"), which is *why* `anytype_client.py` is built on `aiohttp` (already bundled with Hermes core) instead of `httpx`/`httpx-sse`: a plugin with its own extra pip dependency would force every Docker user to build and maintain a derived image just to use it. Directory-drop plugins (this one) also need no pip-install step at all, even outside Docker, since Hermes imports the plugin directory directly rather than via a package manager.
- README covers: prerequisites (running Hermes + Anytype with local API access — self-hosting the sync backend specifically is optional, orthogonal, and not required), install (drop-in, no pip step), the real env-var config from Section 3, and mention-mode vs. reply-all framing from Section 2.
- **Two more gaps caught by the same real-world beta pass, both about install mechanics rather than the plugin's actual code:**
  - Plugin loading is opt-in by default (`config.yaml`'s `plugins.enabled: []`, confirmed from `hermes_cli/plugins.py`) — dropping the directory in isn't sufficient on its own; `hermes plugins enable anytype-platform` is a required step the README originally omitted. Also worth calling out on its own: the name that command expects is `anytype-platform` (`plugin.yaml`'s `name:` field), not `hermes_anytype` (the directory/package name) — a real point of confusion since they don't match.
  - The README's original `cp .env.example .env` instruction is actively dangerous on a live instance — `<HERMES_HOME>/.env` already holds unrelated secrets for anyone with other plugins installed, and that command overwrites the whole file rather than merging. Hermes's own installer (`hermes plugins install` / `hermes plugins enable`, which drives `hermes_cli/plugins_cmd.py`'s `_prompt_plugin_env_vars` → `save_env_value()`) already does this safely, per-key, off the same `requires_env`/`optional_env` block in `plugin.yaml` — now the README's primary documented path, with manual `.env` editing demoted to an alternative that explicitly warns to merge, not overwrite.

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

Open question from Section 9 that turned out to matter architecturally: **Anytype has no bot-token concept.** Unlike Slack/Discord, there's no way to mint a credential scoped *inside* an existing account. Every space participant is a full any-sync identity (own keypair, own local node). The REST API key from the challenge flow (`POST /v1/auth/challenges` → `POST /v1/auth/api_keys`) just authenticates to whichever account is running the local node at the base URL you point at — it does not create an identity. **If `api_key` is minted against the user's own desktop Anytype app, every Hermes message posts under the human's own member profile, not "Hermes."**

To give Hermes a distinct profile, it needs its own account, running its own node, invited into the space as its own member. `anyproto/anytype-cli` is built for exactly this ("bot accounts") — it embeds `anytype-heart` headless, no desktop app required. Shipped as [`docker-compose.anytype-bot.yml`](../docker-compose.anytype-bot.yml) at the repo root, using the official `ghcr.io/anyproto/anytype-cli` image — the bootstrap-then-serve pattern in that file is confirmed against `anyproto/any-sync-dockercompose`'s own (optional, commented-out) `anytype-cli` service block, not invented:

1. `docker compose -f docker-compose.anytype-bot.yml up -d` — the bootstrap service runs the CLI headless just long enough to `auth create hermes` (mints a new account key; account-key auth only, no mnemonic login) if `/root/.anytype/config.json` doesn't already exist, then the main service takes over with `restart: unless-stopped`. That file is for the default Anytype Network case; if self-hosting your own any-sync backend instead, don't use it — uncomment the equivalent block already in your `any-sync-dockercompose` checkout so the bot shares your `network.yml` and actually lands on your network (an unrelated bot account with no `--network-config` pointed at the same deployment lands on an unreachable network).
2. **Immediately back up the account recovery key, then delete it from the container** — `docker compose ... exec anytype-cli cat /root/.anytype/ACCOUNT_RECOVERY_KEY.txt`, copy it into a password manager, then `docker compose ... exec anytype-cli rm /root/.anytype/ACCOUNT_RECOVERY_KEY.txt`. This key is the *only* way to recover this identity if the `anytype-cli-data` volume is ever lost — treat it like a wallet seed phrase, not a config value.
3. `docker compose ... exec anytype-cli anytype auth apikey create hermes-integration` — mints the API key scoped to *this* identity/node. This is `ANYTYPE_API_KEY`; `ANYTYPE_API_BASE_URL` is this container's published port (`http://127.0.0.1:31012` by default).
4. From the human's desktop app: Share Space → generate an invite link (approval-required by default; owner can enable auto-approve but Anytype's own docs advise against it for sensitive spaces).
5. `docker compose ... exec anytype-cli anytype space join "<invite-link>"`, then the human approves the join request in-app. Will likely need explicit promotion to Editor role to match the "free writes by default" decision in Section 2 — default post-join role isn't confirmed.

**Caught by real-world beta feedback (a from-scratch install attempt against a live Docker Hermes stack):** the compose file and this section originally used `/app/anytype` as the binary path in both the bootstrap entrypoint and these exec commands. Wrong — `/app` is just the image's empty `WorkingDir`; the actual binary is `/usr/local/bin/anytype`, already on `PATH` via the image's own `ENTRYPOINT`, so the right invocation is the bare `anytype` command. Compounding it: the bootstrap script swallowed the resulting failure (`|| true` / `exit 0` regardless), so `docker compose up -d` reported success, the main container came up "healthy" (the healthcheck only checked that the port was open, not that an account existed), and the empty-account failure was invisible short of reading `docker logs` directly. Fixed in both places: the bootstrap script now propagates a real failure (blocking the main container from ever starting via `depends_on: condition: service_completed_successfully`), and the healthcheck now also asserts `/root/.anytype/config.json` exists, not just that the port answers.

**Caught by a second round of the same beta feedback:** `auth create`'s own output — which includes the account's recovery key in plaintext — was left to flow into the bootstrap container's stdout, which `docker logs` captures. That's a far more widely readable, longer-retained surface than a wallet-seed-phrase-equivalent secret should ever land on (log aggregators, anyone with `docker logs` access, or just accidentally pasted into a chat/ticket while debugging — which is what actually happened). Fixed: that output is now redirected to `ACCOUNT_RECOVERY_KEY.txt` on the persistent volume instead (`chmod 600`), never touching stdout/logs; step 2 above was added so the key still gets extracted and backed up rather than silently orphaned in the volume. Also switched the bootstrap script from a single `entrypoint: /bin/sh -c "..."` scalar (which needs two correctly-nested layers of escaping — Compose's own `${...}` interpolation *and* shell quoting — to round-trip, and wasn't independently verifiable without Docker itself available to check against) to list-form `entrypoint`/`command`, which sidesteps that ambiguity entirely.

**Beta round 2 (2026-08-16, same-day retest): confirmed the path-bug fix works, hit a new blocker one step later.** After the fix above, `anytype auth create` now genuinely succeeds — a real `config.json` lands in the volume, the healthcheck passes for real. But the *main* `anytype-cli` container then fails to auto-login using that freshly-created identity:

```
Failed to auto-login with account key after 3 attempts: wallet recovery failed: rpc error: code = DeadlineExceeded desc = context deadline exceeded
authorize: rpc error: code = Unauthenticated desc = parse token: signature is invalid
```

...and `auth apikey create` subsequently fails the same way (`Unauthenticated: parse token: signature is invalid`). This is a live any-sync networking/protocol issue, not a documentation or shell-scripting bug — the kind of thing that genuinely needs an actual Docker host and live network access to iterate on, which isn't available to whoever's editing this doc from a plain source checkout. What the beta pass ruled out: TCP/443 reachability to the any-sync coordinator (confirmed fine, both raw IP and hostname), a stale/wrong image (same `latest` digest across both containers in the same test run), and firewall-blocks-everything (ruled out) — VPN interference (a `gluetun` container elsewhere on the same host) looked unlikely but wasn't ruled out with full certainty.

**Leading hypothesis, unconfirmed:** both containers log a QUIC buffer-size warning at startup (`failed to sufficiently increase receive buffer size (was: 208 kiB, wanted: 7168 kiB, got: 416 kiB)`), meaning any-sync uses QUIC/UDP for at least part of its protocol — only TCP reachability was verified above, so a UDP-path problem (or the OS-level receive-buffer ceiling the warning itself names) is the strongest untested lead. That ceiling (`net.core.rmem_max`/`net.core.wmem_max`) is a host-wide Linux kernel parameter, not something a container can raise for itself — if you want to try this, it has to happen on the Docker *host*, not in `docker-compose.anytype-bot.yml`:

```bash
sudo sysctl -w net.core.rmem_max=7500000
sudo sysctl -w net.core.wmem_max=7500000
# persist across reboots:
echo 'net.core.rmem_max=7500000' | sudo tee /etc/sysctl.d/99-anysync-quic.conf
echo 'net.core.wmem_max=7500000' | sudo tee -a /etc/sysctl.d/99-anysync-quic.conf
sudo sysctl --system
```

Two low-confidence mitigations landed alongside this note (docker-compose.anytype-bot.yml): the settle time between `auth create` succeeding and killing the temporary bootstrap server was bumped from 2s to 5s (in case the identity needs more time to propagate to the coordinator before the process holding that session is torn down), and a comment recommending pinning `ANYTYPE_CLI_VERSION` instead of trusting `latest` long-term, for reproducibility. Neither is a confirmed fix — flagged as such in the file itself. **Status: this now blocks reaching a live Anytype instance entirely, one step further than round 1's path bug but still before `ANYTYPE_API_KEY` exists.** Needs the sysctl experiment (or other live network debugging) run against the real environment to make further progress.

**Beta round 3 (2026-08-16, same-day retest): settle-time bump made no difference (as flagged it might not); sysctl experiment blocked by environment, not declined.** Same `DeadlineExceeded` failure, byte-for-byte, confirming the 2s→5s change wasn't the fix (expected, given how it was flagged above). More useful finding: the test host runs **Docker Desktop for Mac**, not a bare Linux host — so `sudo sysctl -w net.core.rmem_max=...` isn't reachable the normal way; Docker Desktop's VM isn't `ssh`-able, and applying a sysctl change there would mean a shared, host-wide change affecting every other container on that machine, not a scoped one. Correctly paused rather than making that change unprompted.

This actually strengthens the QUIC/UDP hypothesis rather than dead-ending it: Docker Desktop for Mac's user-space networking layer has long-standing, independently-documented UDP/QUIC reliability problems — this is corroborating evidence from an unrelated source, not just this one warning line (see the round 4 correction below on exactly which backend/citation applies). No client-side "prefer TCP" flag for `anytype-cli`/`anytype-heart` is documented anywhere findable (checked `any-sync-dockercompose`'s own Configuration wiki, which only covers self-hosted coordinator/node-side transport config, not client-side) — and self-hosting isn't the setup being tested here anyway (this is the default Anytype Network case), so there's no coordinator config to change even if there were a documented knob.

**Beta round 4 (2026-08-16): native test confirms Docker is the actual variable; further digging narrows down which Docker layer, and finds a real stay-in-Docker experiment.** Running `anytype-cli` natively on the same Mac (same OS, same network, same any-sync backend, outside Docker entirely) succeeded cleanly — `auth create` in under a second, no `DeadlineExceeded`, no QUIC buffer warning at all — while the still-running Docker container hit the identical round 2/3 failure concurrently, side by side. About as clean a controlled comparison as this gets.

Given other VPN/proxy layers already exist on this particular host, several more specific explanations for *why* Docker's networking breaks this were ruled out rather than accepting "it's just Docker Desktop" at face value:
- **gluetun** (a VPN container used by other services on this host) — `anytype-cli` sits on its own plain bridge network, not `network_mode: service:gluetun`; confirmed only two unrelated containers actually ride gluetun's network stack.
- **Tailscale, containerized** — separate network namespace, no path overlap.
- **Tailscale, host-level** — the one real lead here: a host-level Tailscale daemon had installed a route via a low-MTU (1280) interface. Checked whether it was acting as an exit node (which would tunnel *all* host/Docker-VM traffic through that constrained path) — `tailscale status --json` confirms `ExitNodeID: False`, so it only routes tailnet-range traffic, not everything. Ruled out with a real check, not assumed.
- **MTU mismatch** generally — host and Docker bridge interfaces both report a clean 1500, no mismatch.
- **DNS** — resolves correctly and quickly from inside the container.
- **Caddy** — no anytype-related config anywhere across this host's Caddyfiles.

**Correction to the round 2/3 citation above:** it names `vpnkit` specifically — wrong for this host. `vpnkit` isn't even running here; this Docker Desktop (4.86.0) is configured with the newer **gVisor** network backend (`networkType: gvisor`, the default since Docker Desktop 4.19), a different codebase with its own separate history of UDP/QUIC issues (`docker/desktop-feedback#133`, among others). "User-space network emulation instead of native kernel networking" is still the right general theory and still matches everything observed — the specific backend named just needs to be gVisor, not vpnkit, on this host.

**New, genuinely promising, stays-100%-in-Docker experiment:** Docker Desktop's network backend is switchable — `gvisor` (current default) vs. the older `vpnkit` — via one setting in `~/Library/Group Containers/group.com.docker/settings-store.json` (`"networkType": "vpnkit"`, then quit and restart Docker Desktop). Since gVisor and vpnkit have *different*, not necessarily equally bad, UDP/QUIC quirks — Docker's move to gVisor in 4.19 was for performance, not because vpnkit was universally worse — this is worth trying before concluding Docker Desktop can't run this at all. Fully reversible, no shared-VM access needed, no separate host process.

**Net status after round 4:** all other plausible explanations on this host (gluetun, Tailscale exit-node routing, MTU, DNS, Caddy) are now ruled out rather than merely untested, which if anything makes the underlying "Docker Desktop's user-space networking" theory more likely, not less.

**Beta round 5 (2026-08-16): the failure boundary narrows to one specific RPC, and the actual root cause is found.** Before trying the vpnkit-backend switch, the tester ruled out the remaining generic suspects (no third-party firewall/monitoring tool installed; `daemon.json` has no custom DNS/MTU/proxy config; the Docker Desktop VM is nowhere near resource-starved) and then built a real QUIC version-negotiation probe rather than continuing to infer UDP behavior from log lines. Findings:

- Generic UDP round-trips are **not** broken by Docker's network stack — the probe succeeds in ~13ms against Google's public QUIC endpoint, from inside a container on the same network as anytype-cli.
- A **fresh** `auth create` (brand-new identity, no prior state) succeeds in Docker in ~1 second. This directly contradicts "any-sync over QUIC just doesn't work in this Docker setup" — it does, for account creation.
- The failure is specifically in **recovering an existing stored account** — a different RPC than creation. Reproduced cleanly and repeatedly on a controlled identity: create succeeds, then a fresh container's auto-login against that same stored key hits the exact `DeadlineExceeded` from rounds 2/3.
- The receive-buffer warning is present in both the successful and failing runs, confirming it's a real difference from native (which shows none) but not sufficient alone to explain the failure — a contributing factor under heavier load, not the whole story.

This lines up with a genuine root cause found by reading the actual source (docs and web search had nothing — same pattern as finding Hermes's real plugin API earlier in this project): account **creation** and account **recovery/auto-login** are different RPCs in anytype-heart, and recovery has to pull back an existing account's history — a heavier, more timing-sensitive transfer than a brand-new account's near-empty initial sync. Traced three repos to confirm:

- `anyproto/any-sync`'s `net/peerservice/peerservice.go` has a real `PreferQuic(bool)` toggle controlling per-connection transport ordering (yamux/TCP vs. QUIC).
- `anyproto/anytype-heart`'s `core/anytype/config/config.go` calls `PreferQuic(true)` unconditionally unless a `PeferYamuxTransport` config flag is set — and the comment right next to it, written by their own engineers, says: *"used only in case client has some problems with QUIC."* That flag threads all the way from a real, correctly-spelled `PreferYamuxTransport` field on the `RpcAccountSelectRequest` protobuf message (confirmed field name and type directly in the generated `pb/commands.pb.go`).
- `anyproto/anytype-cli`'s `core/serviceprogram/serviceprogram.go` (`attemptAutoLogin()`, called when `anytype serve` finds a stored key on startup — this is the exact log line "Found stored account key, attempting auto-login...") calls `core/auth.go`'s `Authenticate()`, which builds an `AccountSelect` request that **never sets `PreferYamuxTransport`.** A separate `AccountSelect` call exists too, immediately after `AccountCreate` in a different function (`CreateAccount`) — used for brand-new accounts, and left alone since that path is confirmed already working over QUIC.

**Fix, patched and built (not yet verified live):** forked `anyproto/anytype-cli` to [`thisispete/anytype-cli`](https://github.com/thisispete/anytype-cli), with a one-line, narrowly-scoped patch setting `PreferYamuxTransport: true` on just the `AccountSelect` call inside `Authenticate()` — the one confirmed-broken RPC, not a blanket "avoid QUIC everywhere" change. The official image can't be reused with a patch applied: its release build cross-compiles from a macOS runner using Homebrew (`mingw-w64`, `musl-cross`) and links against `anyproto/tantivy-go`'s prebuilt native library, which for Linux is **musl-only** (`linux-${arch}-musl.tar.gz` — matching Alpine's libc, not Ubuntu's glibc, which is why a plain `golang:` build on a bare Ubuntu runner wasn't an option either). Built instead via a new GitHub Actions workflow on the fork ([`.github/workflows/build-patched-image.yml`](https://github.com/thisispete/anytype-cli/blob/main/.github/workflows/build-patched-image.yml)) that compiles natively per-architecture (`ubuntu-latest` for amd64, `ubuntu-24.04-arm` for arm64 — no cross-compilation needed) inside `golang:1.25-alpine` containers, matching the musl toolchain the tantivy release expects, then reuses the repo's own unmodified `Dockerfile` (which already just expects `anytype-linux-${TARGETARCH}` binaries in its build context) to assemble and push a multi-arch image. Verified via the real CI run, not assumed: all three jobs (`build-binary` × 2 archs, `build-image`) completed successfully, with a real pushed manifest digest (`sha256:80a4cc155e7bace398f764863477d4007f0501791f4731dd7a2412af36218449`) confirmed in the build logs. `docker-compose.anytype-bot.yml` now points at `ghcr.io/thisispete/anytype-cli:patched` instead of the official image.

**Still open:** the image builds and pushes successfully, but the actual fix — does auto-login now succeed against a real stored account — hasn't been tested live yet. That's the next beta pass. Also unverified: whether `ghcr.io/thisispete/anytype-cli` is set to public visibility (the token used to build it lacked the scope to check or set this; packages pushed via `GITHUB_TOKEN` don't reliably inherit the source repo's public visibility). Filing this upstream with `anyproto/anytype-cli` is deliberately deferred until the patch is confirmed working end-to-end, per explicit instruction — premature to ask them to review a fix that hasn't been proven yet.

**Unverified, needs empirical check before finalizing setup docs:** whether `auth create hermes` makes "Hermes" the actual display name/avatar shown in chat, or whether that has to be set separately once logged in as that identity. Same category of "verify against a running instance" caveat as the mention-mark shape in Section 9. (No beta pass has reached this question yet — round 1 was blocked at the path bug, rounds 2 and 3 at the auto-login failure above.)

This doesn't change the config shape in Section 3 — still one `api_key`/`api_base_url` pair — but adds a real manual prerequisite outside the plugin itself: standing up a second, bot-owned Anytype node before Hermes can appear as itself rather than as the human. Belongs in the README's prerequisites section (Section 8).

## 11. Alternatives considered (context, not decisions)

Captured for whoever picks this up later, since they were seriously discussed:

- **Claude-first via MCP instead of Hermes.** The object/schema capability layer already exists as an official server (`anyproto/anytype-mcp`) — client-agnostic, works with Claude Desktop (local MCP), claude.ai Connectors (remote MCP behind a reverse proxy, a common self-hosted deployment pattern), or Claude Code under a Pro/Max login — all without metered API billing. The one thing none of those paths can do is proactive/reactive chat presence (something has to sit listening and call an LLM on its own initiative, which structurally requires a metered API-backed process) — that's the actual reason a Hermes-side gateway is still worth building, not just inertia. Likely end state: both — Claude/Connectors for on-demand pull-based use, this plugin for the ambient/reactive chat presence Claude structurally can't provide under a subscription model.
- **Retiring Hermes entirely.** Partially reconsidered mid-design. Claude Code now has native scheduled-task support (a real replacement for simple cron-style recurring prompts), but Hermes's Discord-gateway model still provides ambient cross-device context continuity (same conversation reachable from any device with zero setup) that Claude Code's current session/machine-bound model doesn't fully replicate — claude.ai syncs conversational context across devices, but Claude Code sessions tied to a specific machine's filesystem still require deliberate remote setup (SSH, or Anthropic's newer Remote Control / cloud-session mechanism) rather than being automatic. Not resolved — flagged as an open question, not blocking this project.

## 12. Next steps

1. ~~Bootstrap the actual repo~~ — done: `git init`, MIT `LICENSE`, this file at `docs/design.md`, `.gitignore`.
2. ~~Confirm Hermes's exact plugin-authoring API surface~~ — done, against real source rather than docs (Hermes's own docs page for this was incomplete): cloned `NousResearch/hermes-agent` and read `hermes_cli/plugins.py` (`register_platform`/`register_tool` signatures), `gateway/platforms/base.py` (`BasePlatformAdapter`, `MessageEvent`, `SendResult`, the `Platform` enum's `_missing_()` dynamic-member hook), and the shipped `plugins/platforms/mattermost/` + `plugins/platforms/irc/` adapters as real templates. `adapter.py`/`tools.py`/`__init__.py`/`plugin.yaml` are now built against that, not a guess.
3. **Still open:** spin up a self-hosted Anytype instance to verify the mention-mark shape empirically (Section 4.1, Section 9) — the shipped adapter uses the substring-match fallback since this was never verified; upgrading to structural mention detection needs a live instance.
4. **Root cause found and patched, not yet verified live (beta round 5, Section 10):** the auto-login failure traces to `anytype-cli` never setting the `PreferYamuxTransport` flag anytype-heart's own code documents as existing "in case client has some problems with QUIC" — confirmed by reading the actual source across `any-sync`, `anytype-heart`, and `anytype-cli`, not inferred from logs. Patched in a fork (`thisispete/anytype-cli`), built and pushed via a real (green) CI run to `ghcr.io/thisispete/anytype-cli:patched`, and wired into `docker-compose.anytype-bot.yml`. **Still open:** run it live against a real stored account to confirm the fix actually works (the build succeeding isn't the same as the fix working); confirm the ghcr.io package is public; file upstream once confirmed. The gVisor/vpnkit backend-switch experiment from round 4 is superseded by this — no longer the next step unless the patch turns out not to fully resolve it.
5. **Still open, deliberately deprioritized (Section 1):** cursor-based SSE resume (`after_order_id`) instead of reconnecting to a fresh live stream on drop (Section 6). A real gap, not a blocker.
6. **Beta passes against a live Docker Hermes stack, same day (2026-08-16):** round 1 confirmed the plugin itself imports, registers, and passes `hermes plugins doctor` cleanly, and surfaced/got-fixed four install-mechanics bugs (wrong binary path, missing `plugins.enabled` step, unsafe `.env` overwrite, `provides_tools` sync — Section 8, Section 10). Rounds 2-3 confirmed the path-bug fix works and got one step further before hitting the auto-login failure in item 4 above; round 3 additionally found the sysctl lead untestable on this particular host (Docker Desktop for Mac) without a shared, invasive VM change. `adapter.py`/`anytype_client.py`'s actual runtime behavior against a real Anytype instance is **still unverified** — no round has reached that far yet.