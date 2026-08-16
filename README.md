# hermes-anytype

A [Hermes](https://github.com/) plugin that gives Hermes a live chat presence
inside a self-hosted [Anytype](https://anytype.io/) space, plus tools to
search/create/update that space's typed objects — driven by live schema
introspection, so it works against anyone's custom type setup with zero
manual mapping.

See [documents/design.md](documents/design.md) for the full design and
rationale. **Status: scaffolded, not yet functional** — the Anytype-side REST
client and tool logic are implemented and tested; the actual wiring into
Hermes's plugin API (`ctx.register_platform()` / `ctx.register_tool()`) is
stubbed pending confirmation of Hermes's literal plugin-authoring API
surface (see the `TODO(confirm-hermes-api)` markers in
[`hermes_anytype/__init__.py`](hermes_anytype/__init__.py) and
[`hermes_anytype/gateway.py`](hermes_anytype/gateway.py)).

## Prerequisites

1. A running Hermes instance.
2. A self-hosted Anytype instance with API access enabled.
3. **A separate Anytype identity for Hermes itself**, invited into your
   space as its own member — otherwise every message posts under your own
   profile, not "Hermes". See [design.md §10](documents/design.md#10-hermess-anytype-identity-bot-account-setup)
   for the full `anytype-cli` walkthrough. Short version:

   ```bash
   anytype serve                              # run Hermes's own headless node
   anytype auth create hermes                 # create the "hermes" identity
   anytype auth apikey create hermes-integration
   # from your desktop app: Share Space -> copy invite link
   anytype space join "<invite-link>"         # then approve the join request in-app
   ```

## Install

```bash
pip install -e ".[dev]"
```

Drop the resulting package into `~/.hermes/plugins/platforms/hermes-anytype/`,
or via Hermes's plugin installer if one exists by the time you read this —
verify against current Hermes docs.

## Configure

Copy [`.env.example`](.env.example) to `.env` and fill in the values from the
bot-account setup above (`ANYTYPE_API_KEY` / `ANYTYPE_API_BASE_URL` must
point at *Hermes's own* node, not your desktop app's).

```yaml
anytype:
  api_key: ${ANYTYPE_API_KEY}
  api_base_url: ${ANYTYPE_API_BASE_URL}
  space_id: ${ANYTYPE_SPACE_ID}
  channels:
    - chat_id: "..."
      mode: mention        # or: all
      trigger: "@hermes"   # only used when mode: mention
```

Each chat in the space gets its own response mode:

- **`mention`** (default) — Hermes only replies when addressed by the
  `trigger` string. Good for a shared team room.
- **`all`** — Hermes replies to everything in that chat. Good for a solo
  space, or a dedicated help room.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the Anytype REST client (mocked HTTP via `respx`), the
property-validation/normalization logic in `tools.py`, per-chat
mention/reply-all filtering in `gateway.py`, and config validation. There's
no CI coverage against a real Anytype instance (impractical to run one in
CI) — manual verification against a live self-hosted instance is still
needed before this is production-ready.

## License

MIT — see [LICENSE](LICENSE).
