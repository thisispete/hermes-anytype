"""Pure env/config helpers, deliberately free of any Hermes-internal import.

adapter.py imports `gateway.config` / `gateway.platforms.base`, which only
exist inside a real Hermes install -- so nothing that imports adapter.py can
be unit-tested in this standalone repo (there's no pip-installable Hermes
package; the real gateway package is a sibling in Hermes's own monorepo, not
a dependency of ours). The logic actually worth unit-testing -- mention
filtering, env-var parsing, required-config checks -- lives here instead,
with no such import, so it stays covered even though the adapter class
itself can only be exercised inside a live Hermes environment (same
can't-run-the-real-thing-in-CI situation docs/design.md §7 already accepts
for Anytype itself).
"""

from __future__ import annotations

from typing import Any


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def env_bool(*values: Any, default: bool) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text:
            return text in ("1", "true", "yes", "on")
    return default


def should_respond(message_text: str, *, require_mention: bool, trigger: str) -> bool:
    """Per-chat mention/reply-all filter (docs/design.md §2, §4.1).

    Structural mention detection (via TextMark spans) isn't wired in -- the
    mark shape needs empirical verification against a running instance
    (docs/design.md §9/§12). Falls back to a case-insensitive substring
    match on the trigger string.
    """
    if not require_mention:
        return True
    return trigger.lower() in message_text.lower()


def has_required_config(api_key: str, base_url: str, space_id: str) -> bool:
    """docs/design.md §6: fail loud at startup on bad/missing config, never
    silently no-op."""
    return bool(api_key.strip() and base_url.strip() and space_id.strip())
