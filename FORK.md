# FORK.md — Fork-Local Change Ledger

Ledger of changes that exist **only in this fork** and deliberately diverge from
upstream [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
Newest entry first. This file is fork-local tooling, not an upstream Hermes
contract (same status as `run.ps1` / Fork-local AE2).

**Why a file and not GitHub Issues:** one ledger view, versioned together with
the code it describes, zero merge-conflict surface on weekly `main` merges
(upstream will never add `FORK.md`), visible in the IDE and to future agent
sessions via the `AGENTS.md` Documentation Map pointer. Open an Issue on the
fork only if a change needs a discussion thread.

**Entry format:**

- **Date / Status** — when landed; `active`, `merged-upstream`, `dropped`.
- **Summary** — what and why, one paragraph.
- **Files** — the footprint a weekly `main` merge must watch.
- **Upstream disposition** — what upstream did/said about this exact change,
  with issue/PR links.
- **Merge risk** — conflict hotspots and what to re-check after each merge.
- **Known limitations** — accepted gaps, with reasons.

---

## 2026-08-30 — `delegate_task` per-call model override

- **Status:** active (fork-local).
- **Summary:** the parent agent can pass `model` per task in `delegate_task`
  (plus an unadvertised top-level `model` batch default). The name resolves
  through the same chain as the `/model` command and is validated fail-closed
  against the authenticated-provider inventory — the same list the Desktop
  model picker shows; unknown or unavailable models are rejected before any
  child spawns. No agent-facing `provider`/`base_url`/`api_key`/`api_mode`:
  the agent cannot invent endpoints or credentials. Runtime kill-switch:
  `delegation.allow_model_override` (default `true`). Priority:
  per-task `model` > top-level `model` > `delegation.*` config > parent
  inherit. Parent runtime state is snapshot-tested unchanged on
  success/error/timeout paths (guard against upstream bug
  [#62665](https://github.com/NousResearch/hermes-agent/issues/62665)).
- **Files:** `tools/delegate_tool.py`, `tools/async_delegation.py`,
  `tools/process_registry.py`, `hermes_cli/models.py` (`_routing_variant_catalog_base`),
  `run_agent.py` (+1 line), `hermes_cli/config_defaults.py`,
  `tests/tools/test_delegate_model_override.py`,
  `website/docs/user-guide/configuration.md`,
  `website/docs/user-guide/features/delegation.md`.
- **Upstream disposition:** **rejected deliberately.** The per-call `model`
  parameter was removed in upstream commit `fb0f579b1`; maintainer teknium1 on
  [#35437](https://github.com/NousResearch/hermes-agent/issues/35437): "We do
  not want this". Merged docs PR
  [#49489](https://github.com/NousResearch/hermes-agent/pull/49489) states the
  subagent model is config-level, not per-call; a standing
  `delegation-model-routing` policy forbids agent-selected inference routes
  after credit-burn incidents ([#49332](https://github.com/NousResearch/hermes-agent/issues/49332)).
  30+ closed unmerged PRs proposed the same (#12715, #81799, #63461, #50465,
  #50435, #61007, #34472, #17581, #34650, #46981). The upstream-blessed
  direction is [#94133](https://github.com/NousResearch/hermes-agent/issues/94133)
  (open): user-owned session-scoped delegation routes — the user authorizes a
  provider/model/effort route, the agent references it opaquely. Our design
  addresses the stated objection differently: the agent can only pick models
  already authenticated in this instance (picker inventory allowlist), never
  arbitrary routes or credentials. If upstream ever ships #94133, evaluate
  rebasing this feature onto it.
- **Merge risk:** `tools/delegate_tool.py` is high-churn upstream. Conflict
  hotspots: the `DELEGATE_TASK_SCHEMA` `tasks[]` properties block, the
  credential-resolution section of `delegate_task()`, the async dispatch
  metadata, and `_routing_variant_catalog_base` call sites in
  `hermes_cli/models.py`. After each weekly merge re-run
  `tests/tools/test_delegate_model_override.py` and
  `tests/tools/test_delegate.py`.
- **Known limitations (accepted):** the allowlist reads the on-disk config and
  does not overlay live parent session state (`/model --once`, live-only
  models on custom endpoints); legacy `custom_providers` slug aliases
  (`provider_key`, names with spaces/parens) are not matched. Both are out of
  scope for this fork's surfaces (Telegram Gateway, Hermes Desktop, OpenRouter;
  Gemini works via the static catalog). Marked in code with
  `# ! Known limitation` at the inventory check.

---

## 2026-08-12 — Telegram STT echo as a collapsed quote

- **Status:** active (fork-local). Follow-up 2026-08-29 keeps the quote
  intact when a long transcript is split across Telegram's length cap.
- **Summary:** inbound voice STT is still echoed when
  `stt.echo_transcripts` is true (that toggle is upstream). On Telegram
  the echo is an HTML expandable blockquote
  (`<blockquote expandable>…</blockquote>`) so the transcript sits in a
  collapsed quote instead of a full `🎙️ "…"` line; other platforms keep
  the classic line. The body is HTML-escaped and sent with
  `metadata["telegram_html"]` so MarkdownV2 never sees the transcript
  (markdown characters in speech cannot break parse, and the `**>`
  expandable-quote path is not used). Chunks longer than Telegram's
  budget are re-wrapped so every continuation is still a complete
  collapsed quote, with `(N/M)` outside the tags.
- **Files:** `gateway/stt_echo.py` (new; absent on `upstream/main`),
  `plugins/platforms/telegram/adapter.py` (`telegram_html` →
  `_send_html_message`, STT-aware HTML chunking), `gateway/run.py`
  (both echo send sites), `hermes_cli/config_defaults.py` (comment
  only), `website/docs/user-guide/configuration.md`,
  `tests/gateway/test_stt_echo_format.py`,
  `tests/gateway/test_telegram_format.py`,
  `tests/gateway/test_telegram_voice_v0_regressions.py`.
- **Upstream disposition:** **echo exists; collapsed-quote wrapping does
  not.** The visible transcript echo and `stt.echo_transcripts` landed
  via [#58859](https://github.com/NousResearch/hermes-agent/pull/58859)
  (salvage of #58697 + #53038); request
  [#9656](https://github.com/NousResearch/hermes-agent/issues/9656) is
  closed by sweeper as `implemented_on_main` (GitHub `stateReason`:
  `NOT_PLANNED`). On `upstream/main` the send is still
  `🎙️ "{transcript}"` through the normal MarkdownV2 path — no
  `gateway/stt_echo.py`, no `telegram_html`, no `<blockquote expandable>`
  on this payload. A 2026-08-30 search of NousResearch/hermes-agent
  issues and PRs found **no** request or implementation to wrap STT
  echoes in a collapsed quote (queries: `echo_transcripts`, `stt echo`,
  `collapsed quote`, `blockquote expandable` + transcript/STT/voice,
  `stt_echo`, `telegram_html`). Adjacent, not this feature:
  [#7368](https://github.com/NousResearch/hermes-agent/issues/7368) /
  [#7369](https://github.com/NousResearch/hermes-agent/pull/7369)
  (open) — collapsible quote for **reasoning**, not transcripts;
  [#9605](https://github.com/NousResearch/hermes-agent/pull/9605)
  (merged) — MarkdownV2 `**> … ||` expandable quotes in
  `format_message()`;
  [#90773](https://github.com/NousResearch/hermes-agent/issues/90773) /
  [#90781](https://github.com/NousResearch/hermes-agent/pull/90781)
  (open) — that MarkdownV2 `**>` marker is eaten by bold conversion,
  which is why this fork sends HTML instead;
  [#86040](https://github.com/NousResearch/hermes-agent/pull/86040)
  (open) — reply-anchor the agent answer onto the echo message;
  [#87396](https://github.com/NousResearch/hermes-agent/pull/87396)
  (open) — per-platform echo on/off. A [#9656](https://github.com/NousResearch/hermes-agent/issues/9656)
  comment calls the plain `🎙️ "…"` echo noisy; that is the UX this
  wrapping addresses, but upstream asked for a kill-switch, not a
  collapsed quote.
- **Merge risk:** `gateway/run.py` echo sites (immediate + pending) and
  `plugins/platforms/telegram/adapter.py` `send()` / HTML path are
  high-churn. `gateway/stt_echo.py` is fork-only — conflict only if
  upstream adds the same path. After each weekly merge re-run
  `tests/gateway/test_stt_echo_format.py`,
  `tests/gateway/test_telegram_format.py`, and
  `tests/gateway/test_telegram_voice_v0_regressions.py`.
- **Known limitations (accepted):** non-Telegram platforms still get the
  full `🎙️ "…"` line. If Telegram rejects the HTML parse, the adapter
  strips tags and the collapse is lost on that fallback. Expandable
  quotes need Telegram Bot API 7.3+.
