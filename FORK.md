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

## 2026-09-04 — `main` → `dev` after upstream #102117 (facade + siblings)

- **Status:** active (fork-local merge note).
- **Summary:** upstream
  [#102117](https://github.com/NousResearch/hermes-agent/pull/102117) split the
  god files into facades plus `<stem>_<topic>.py` siblings. This merge takes
  **main's structure** and re-applies fork behavior onto the new homes. Do not
  restore dest's 1744-line `AGENTS.md` dump; the root file stays the ~424-line
  upstream split plus the Fork-local pointer below. In-tree code must import
  defining modules, not PLUGIN-COMPAT / `COMPAT_MANIFEST.md` shims.
- **Files:** remap of fork hotspots after the split:
  sticky/tier retry → `agent/turn_api_error.py`,
  `agent/turn_recovery.py`;
  logical-request accept (history-mutating continue / success only) →
  `agent/turn_tool_validation.py`, `agent/turn_empty_response.py`,
  `agent/turn_final_response.py`, `agent/turn_truncation.py`;
  native audio attach →
  `agent/vision_message_prep.py`, `gateway/run_inbound.py`,
  `gateway/run_turn_runner.py`; persist strip → `agent/turn_finalizer.py`;
  TUI usage RPC → `tui_gateway/methods_session.py`; update helpers stay on
  `hermes_cli/update_cmd.py` (`_resolve_update_branch` on
  `hermes_cli/main_install_repair.py`); delegate override lives in
  `tools/delegate_tool.py` + `tools/delegate_tool_config.py`.
- **Upstream disposition:** #102117 is on `upstream/main`. Fork features remain
  fork-local (see entries below).
- **Merge risk:** taking dest's god files would undo the refactor. After this
  merge re-run the fork suite listed in the 2026-09-01 / 2026-08-31 / audio /
  delegate / update entries (paths below still apply, with the remaps in
  **Files**).
- **Known limitations (accepted):** MerchantBench OpenRouter identity headers
  stay in `merchantbench_adapter/runtime.py`, not `run_agent.py`.

---

## 2026-09-01 — Sticky provider order: Hermes-side pin + cyclic rotation on provider failure

- **Status:** active (fork-local).
- **Summary:** opt-in `provider_routing.sticky_order` (`enabled`, `ttl_seconds`,
  defaults `false` / `600`). Rationale: OpenRouter disables its own sticky
  routing when a manual `provider.order` is set (their docs: "Sticky routing
  is not used when you specify a manual provider order"), so requests hop
  between upstream providers and every hop re-pays the full prompt prefill
  (prompt cache is per-provider; see OpenRouterTeam/ai-sdk-provider#499).
  With `sticky_order.enabled`, Hermes itself pins each request to ONE active
  slug from the pool — pool = resolved `order` (after the per-model
  `models.<id>.order` overlay, on surfaces where the overlay applies) ∩
  `only` (empty intersection → feature silently off + warning). On the wire
  (`extra_body.provider`): `order: [active]`, `allow_fallbacks: false`, plus
  `only: [active]` when the user configured `only`;
  `ignore`/`sort`/`require_parameters`/`data_collection` untouched. The
  active slug rotates cyclically ((i+1) mod len) when a request is
  classified `timeout` / `overloaded` / `server_error` — never on
  `rate_limit` (provider alive, cache warm), empty-content or
  invalid-response retries. Those 429 / invalid-response paths keep the
  pin and still follow the existing retry / model-fallback path (a 429
  or empty/malformed response can still trigger classic model fallback;
  they do not walk the pin pool). Rotation happens before the retry so
  the retry re-collects preferences and lands on the next slug; at most
  `len(pool)-1` rotations per logical request. Model-fallback deferral
  counts **attempts** (rotate-worthy errors this request), not rotations:
  eager transport-failure fallback stays off while
  `attempts_this_request < len(pool)`, so on timeout / overloaded /
  server_error every slug including the last gets a real request
  (`max_retries` is raised to at least `len(pool)` when live). Idle TTL:
  a gap between **logical requests** (turns / new API calls) longer than
  `ttl_seconds` returns the active index to `pool[0]` — the first
  eligible slug (every provider's cache is cold anyway, so back to the
  user's most-preferred eligible choice). TTL is checked only at
  `begin_sticky_logical_request` (outside the retry `while`), never on a
  prefs rebuild between in-request retries. `len(pool)==1` pins without rotation. Rotations are
  logged (slug, reason, index) to `agent.log`, never into the prompt.
  State is per-agent (`agent._sticky_provider_order`), bound in
  `agent_init` (so cron / subagents / CLI background get it from the same
  config; cron uses the flat `order` from config, while batch only gets a
  pool when `providers_order` is passed explicitly — pre-existing:
  `batch_runner` does not read `provider_routing` from config) and
  re-bound by `apply_provider_routing_to_agent` on
  `/model` / fallback resync (pool change via order or only → keep the
  previous active slug if it is still in the new pool, else index 0;
  same pool → index preserved). Live only on
  `api_mode == "chat_completions"` for OpenRouter / Nous Portal.
  Any other `api_mode` (`anthropic_messages`, `codex_responses`, …) and
  direct providers are a full no-op (no pin, no retry-floor, no fallback
  deferral).
- **Files:** `agent/sticky_provider_order.py` (new),
  `tests/agent/test_sticky_provider_order.py` (new),
  `hermes_constants.py` (`StickyOrderConfig`,
  `resolve_sticky_order_config`, bind call in
  `apply_provider_routing_to_agent`), `agent/agent_init.py` (bind),
  `agent/chat_completion_helpers.py`
  (`apply_sticky_order_to_preferences` in `_provider_preferences_for_agent`),
  `agent/conversation_loop.py` (retry budget + rotate + fallback deferral
  hooks), `cli-config.yaml.example`,
  `website/docs/user-guide/features/provider-routing.md`.
- **Upstream disposition:** no upstream counterpart. OpenRouter documents
  `session_id`-based sticky routing (which Hermes already sends via the
  OpenRouter profile's `build_extra_body`) but explicitly disables it under
  a manual `provider.order`; upstream issue #24493 / PR #24495 cover only
  the per-model schema, not client-side pinning.
- **Merge risk:** post-#102117 the rotate-after-`classify_api_error` hook
  lives in `agent/turn_api_error.py`; retry-budget / fallback gates remain
  in `agent/conversation_loop.py`. Re-run
  `tests/agent/test_sticky_provider_order.py` and
  `tests/run_agent/test_provider_parity.py` after each merge.
- **Known limitations (accepted):** rotation on a non-retryable timeout
  (stale circuit breaker) shifts the pin without a retry of that request —
  deliberate, the next request should avoid the timing-out provider;
  `server_error` rotates the pin but is not added to
  `_is_transport_failure` (no new eager fallback class when the feature is
  off); cron resolves only the flat `order` from config (pre-existing
  scope boundary — per-model overlays were never applied there, sticky
  follows the same boundary); batch does not read `provider_routing`
  from config at all (pre-existing) — sticky applies there only when
  `providers_order` is passed explicitly on the CLI; helper-level tests
  cover the gates, plus a
  `run_conversation` integration with a mocked API call that walks
  `[a,b,c]` then model-fallback; `note_attempt` ticks on the
  compression-summary request too (it is a real provider call, so the TTL
  semantics stay honest).

---

## 2026-08-31 — Per-model OpenRouter provider routing + service tier, opt-in per-turn tier escalation

- **Status:** active (fork-local). Follow-up 2026-09-01: `provider_routing`
  registered in the `hermes config set` validator schema
  (`_SCHEMA_KNOWN_DICT_KEYS`, `hermes_cli/config.py`) — the key was never in
  `DEFAULT_CONFIG`, so `config set` printed a false "not a recognized config
  key" warning without `--force`; regression tests in
  `tests/hermes_cli/test_set_config_value.py` (`TestProviderRoutingSchema`).
  Adds `hermes_cli/config.py` to this entry's weekly-merge conflict surface.
- **Summary:** three cooperating config axes for OpenRouter. (1)
  `provider_routing.models.<model>.<key>` — per-model overlay over the flat
  provider-routing keys (`only`/`ignore`/`order`/`sort`/
  `require_parameters`/`data_collection`): per-key precedence with
  fall-through to flat, exact model-id match, resolved by one shared
  resolver (`hermes_constants.resolve_provider_routing_for_model`) on all
  three config readers (CLI, gateway, tui_gateway) and re-resolved by
  `switch_model`. (2) `agent.service_tier_overrides` — per-model service
  tier; precedence session pin > per-model > global. An explicit session
  choice (`/fast`, including `/fast normal`) is a pin: it survives `/model`
  and clears only on session reset. (3) `agent.service_tier_escalation`
  (opt-in, default off) — per-turn ladder flex → default → priority driven
  by streaming time-to-first-token: `consecutive_slow_requests` slow
  observations climb one rung for the rest of the turn; the next user
  prompt resets to the configured tier. Escalation applies as the last step
  of `_effective_request_overrides` without mutating canonical
  `agent.service_tier`/`request_overrides`, holds a wire-tier snapshot so
  outer retries of the same logical request keep the attempt's original
  tier, and is hard-gated off for cron, batch, subagents, background tasks,
  and pinned sessions. Automatic provider fallback and
  `restore_primary_runtime` re-resolve routing + tier for the model that
  will actually serve the next request
  (`agent_runtime_helpers.resync_per_model_routing_and_tier`) and rebase
  the escalation ladder onto the new model's base tier — climbed rungs and
  the slow-streak carry over, the ladder is not reset. Gateway `/fast
  status` reports the effective tier (`fast`/`flex`/`normal`) without
  requiring the model to support Priority/Fast mode; only switching to
  `fast` stays capability-gated.
- **Files:** `hermes_constants.py`, `hermes_cli/config_defaults.py`,
  `agent/service_tier_escalation.py` (new), `agent/agent_init.py`,
  `agent/agent_runtime_helpers.py`, `agent/chat_completion_helpers.py`,
  `agent/conversation_loop.py`, `run_agent.py`, `batch_runner.py`,
  `cli.py`, `hermes_cli/cli_agent_setup_mixin.py`,
  `hermes_cli/cli_commands_mixin.py`, `gateway/run.py`,
  `gateway/slash_commands.py`, `gateway/platforms/api_server.py`,
  `tui_gateway/server.py`, `tui_gateway/methods_config.py`,
  `tui_gateway/methods_prompt.py`,
  `tests/test_per_model_routing_and_service_tier.py` (new),
  `tests/agent/test_service_tier_escalation.py` (new),
  `tests/test_hermes_constants.py`, `cli-config.yaml.example`,
  `locales/en.yaml` (+ sibling locale catalogs for the new
  `gateway.fast.status_flex` key),
  `tests/tui_gateway/test_fast_session_scope.py`,
  `tests/cli/test_fast_command.py`, `tests/gateway/test_fast_command.py`,
  `tests/gateway/test_choice_picker.py`,
  `tests/gateway/test_api_server.py`,
  `tests/gateway/test_session_override_thread_recovery.py`,
  `website/docs/user-guide/features/provider-routing.md`,
  `website/docs/user-guide/configuration.md`,
  `website/docs/reference/slash-commands.md`.
- **Upstream disposition:** the `provider_routing.models.<id>` schema
  deliberately mirrors open upstream PR
  [#24495](https://github.com/NousResearch/hermes-agent/pull/24495) (issue
  [#24493](https://github.com/NousResearch/hermes-agent/issues/24493)) so
  user configs stay compatible if it merges. That PR wires only the CLI
  reader at agent-init time; this fork resolves on all three surfaces and
  on mid-session `/model`. We do **not** absorb its
  `model.models.<id>.context_length` half (out of scope — `model_overrides`
  already covers per-model context windows here). Per-model service tier
  has no upstream counterpart:
  [#78097](https://github.com/NousResearch/hermes-agent/issues/78097) is
  per-provider and open. TTFT escalation has no upstream counterpart.
- **Merge risk:** when #24495 merges upstream, expect textual conflicts in
  `cli.py` (provider_routing read path) and `cli-config.yaml.example`;
  semantics are identical (per-model wins per key, fall-through), so the
  resolution is keep-both-shape. `agent/conversation_loop.py` is
  high-churn — after each weekly `main` merge re-run
  `tests/agent/test_service_tier_escalation.py` and
  `tests/test_per_model_routing_and_service_tier.py`.
- **Known limitations (accepted):** escalation observes only streaming
  main-conversation requests (the non-streaming fallback path produces no
  observation); length-continuation and compression/redirect restarts drop
  the in-flight observation (conservative under-escalation; the state
  self-heals on the next accepted response); TUI slash `/fast`
  on a live agent stays ephemeral across agent rebuilds (pre-existing: the
  slash mirror never persisted `create_service_tier_override`); TUI
  `config.get fast` still shows the global tier when a pinned-normal agent
  has no tier (pre-existing None-inherit display logic in
  `methods_config.py`); the classic CLI gates the whole `/fast` command
  behind `_fast_command_available()`, so `/fast status` stays unavailable
  there for models without Priority/Fast support (gateway and TUI report
  `flex` fine); cron sessions and `delegate_task` subagents do not apply
  the per-model overlays (conscious scope boundary — the requested surfaces
  were CLI, Telegram gateway, and Desktop); gateway `/fast` model identity
  after an auth-fallback reflects the fallback model only when the global
  `model.default` is empty — `last_resolved_model` is consulted solely as
  the empty-config fallback, so with a configured default the status tracks
  the primary model until the next turn runs (resolving the fallback
  identity is credential-gated and deliberately not done by `/fast`); the
  single-profile gateway reads the escalation config at process start
  (restart to apply edits; multiplex sessions re-read it per turn inside
  the profile scope); TUI   `_background_agent_kwargs` treats an explicit
  `None` tier (pinned normal) as "take from config" (pre-existing);
  TUI `preview.restart` agents deliberately stay outside the
  config-managed provenance flag (hidden one-shot preview, escalation
  disabled by omission) — if `_background_agent_kwargs` ever learns to
  pass escalation config, `preview.restart` must gain both flags too;
  gateway `/reasoning` resolves the model from the in-memory override only
  (pre-existing — the same class `/fast` had before this change);
  concurrency safety of the per-session tier flow is enforced by
  construction (turn locals, no runner-shared writes) without a
  worker-pool stress e2e; in
  multiplex mode both the
  agent turn and `/fast` read `channel_overrides` and the
  `last_resolved_model` recovery fallback from process-wide runner
  structures (pre-existing upstream architecture — secondary-profile
  channel overrides are consulted by neither path, and the shared `*` key
  is not profile-namespaced).

---

## 2026-08-30 — Audio-rejection NameError no longer kills the turn

- **Status:** active (fork-local). Live incident the same day: five Phase-1
  subagents died with `NameError: name '_err_lower' is not defined` after
  a stale-watchdog abort, instead of retrying.
- **Summary:** the native-audio 4xx recovery in
  `agent/conversation_loop.py` compared the provider error against
  `_err_lower` without assigning it. That line sits in the generic
  `except Exception as api_error` handler, so **any** API exception that
  was not an image-rejection (timeouts, connection kills, 5xx, unknown
  4xx) raised an uncaught `NameError` and killed the session. Phrase
  matching now lives in `looks_like_audio_content_rejection(_err_body)`
  next to `replace_audio_parts_with_placeholder`, matching the image
  helper. A stale-watchdog-shaped abort retries; a real audio 4xx still
  strips `input_audio` and retries text-only.
- **Files:** `agent/turn_recovery.py` (audio 4xx strip+retry),
  `agent/audio_routing.py`, `tests/agent/test_audio_routing.py`,
  `tests/run_agent/test_audio_rejection_fallback.py`.
- **Upstream disposition:** **no-request-found.** This recovery exists
  only because native audio is fork-local (2026-08-14 entry). Upstream
  `main` has no `_AUDIO_REJECTION_PHRASES` block.
  [#98419](https://github.com/NousResearch/hermes-agent/pull/98419) is
  Telegram STT echo HTML quotes — different code, no update needed.
- **Merge risk:** `agent/turn_recovery.py` is the post-#102117 home of the
  generic API `except`. After each weekly merge re-run
  `tests/run_agent/test_audio_rejection_fallback.py` and
  `tests/agent/test_audio_routing.py`.
- **Known limitations (accepted):** phrase matching is English-only
  (same as image rejection); a translated 4xx still uses the normal
  retry path.

---

## 2026-08-30 — Desktop Update honors `updates.branch` + re-probes `--keep-stash`

- **Status:** active (fork-local). Live incident the same day: in-app
  Update from Hermes Desktop failed with **exit 2** and left the install
  on upstream `main` while this fork lives on `dev`.
- **Summary:** two cooperating bugs. (1) Windows Desktop defaulted the
  hand-off to `--branch main` whenever `%APPDATA%\Hermes\updates.json`
  was missing, even though `config.yaml` already had `updates.branch:
  dev`. Explicit `--branch` overrides YAML, so parked-branch logic
  **switched the checkout to `main`** (tracking `upstream/main`, not
  fork `origin`). (2) The hand-off probed `--keep-stash` on `dev`, then
  reused that argv after the tree mutation. Upstream `main` has no
  `--keep-stash`; argparse exited **2**, which the script treats as the
  "close all Hermes windows" sentinel — no retry, relaunch of the old
  GUI against the older backend, then the **Backend out of date** toast.
  POSIX already pinned the hand-off to `HEAD`; Windows now resolves the
  branch the same way as Desktop (`updates.json` > `config.yaml
  updates.branch` > passed/`main`) **inside** `windows.ps1`, so a stale
  Electron binary that still passes `-Branch main` cannot downgrade the
  fork. Retry re-probes `--keep-stash` (and treats argparse-on-that-flag
  as retryable, not as the exit-2 lock sentinel).
- **Files:** `scripts/desktop-update/windows.ps1`,
  `scripts/desktop-update/posix.sh`,
  `apps/desktop/electron/update-branch.ts` (new),
  `apps/desktop/electron/update-branch.test.ts` (new),
  `apps/desktop/electron/main.ts` (`readDesktopUpdateConfig`),
  `tests/test_desktop_update_windows_keep_stash_retry.py`,
  `tests/test_desktop_update_shim_progress.py`.
- **Upstream disposition:** **no-request-found** for this exact pair.
  `--keep-stash` is fork-local (not on upstream `main`). `updates.branch`
  is the 2026-08-10 fork entry below; Desktop never read it until this
  change. Adjacent upstream pain: parked-branch switch + `--branch`
  override, and the Windows hand-off treating exit 2 as non-retryable.
- **Merge risk:** `windows.ps1` / `posix.sh` are high-churn (shim lock,
  pipe drain, python `-m` hand-off). `main.ts` `readDesktopUpdateConfig`
  is touched by every Desktop update-UI PR. After each weekly merge
  re-run `tests/test_desktop_update_windows_keep_stash_retry.py`,
  `tests/test_desktop_update_windows_pipe_drain.py`,
  `tests/test_desktop_update_windows_python_handoff.py`, and
  `apps/desktop` `npx vitest run --project electron electron/update-branch.test.ts`.
- **Known limitations (accepted):** an explicit `updates.json`
  `"branch": "main"` still wins over YAML — do not pin `main` in
  Settings → Updates if this checkout tracks `dev`. The running Desktop
  binary only picks up the Electron-side fallback after a Desktop
  rebuild; until then `windows.ps1` is the guard, and it only runs from
  the **live** install tree (`HERMES_HOME\hermes-agent`). ZIP-fallback
  still refuses non-`main` (same as the 2026-08-10 entry).

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
- **Files:** `tools/delegate_tool.py`, `tools/delegate_tool_config.py`,
  `tools/async_delegation.py`,
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

## 2026-08-14 — Native audio input + `audio_analyze`

- **Status:** active (fork-local).
- **Summary:** inbound voice notes and audio files can ride the native
  `input_audio` wire when the active model can hear, instead of STT-only
  text. `agent.audio_input_mode` is `auto` (models.dev `supports_audio` /
  Gemini 3.5+ slug heuristic), `native`, or `text`. At most one clip is
  attached per turn; STT transcripts stay as durable evidence; bytes are
  stripped after persist and on provider rejection. `audio_analyze` is a
  listen tool (native envelope or STT fallback) in a named `audio` toolset,
  also folded into several platform cores. Gateway session-scoped buffering
  sits beside native images. Anthropic / Bedrock / Codex adapters are
  placeholders.
- **Files:** `agent/audio_routing.py` (new; absent on `upstream/main`),
  `agent/conversation_loop.py` (audio-rejection recovery),
  `tools/audio_tools.py` (new), `toolsets.py`, `gateway/run.py`,
  `gateway/session_state.py`, `run_agent.py`, `agent/image_routing.py`
  (`_supports_capability_override`), `agent/gemini_native_adapter.py`,
  `hermes_cli/config_defaults.py`, `hermes_cli/tools_config.py`
  (`_RECENTLY_SHIPPED_TOOLSETS` includes `audio`),
  `apps/desktop/src/app/settings/constants.ts`,
  `tests/agent/test_audio_routing.py`,
  `tests/run_agent/test_audio_rejection_fallback.py`,
  `tests/tools/test_audio_analyze.py`,
  `tests/gateway/test_native_audio_buffer_isolation.py`.
- **Upstream disposition:** **mixed — not on main; open competing PRs.**
  `upstream/main` has `supports_audio_input()` metadata and STT inbound
  (`_enrich_message_with_transcription`) but no `audio_routing.py`, no
  `agent.audio_input_mode`, and no `audio_analyze`. Closest live effort:
  [#90206](https://github.com/NousResearch/hermes-agent/pull/90206) (open)
  — native gateway `input_audio` via `agent/media_routing.py`. Architectural
  twin [#37149](https://github.com/NousResearch/hermes-agent/pull/37149)
  (`agent/audio_routing.py`) was self-closed unmerged; API-server
  [#18975](https://github.com/NousResearch/hermes-agent/pull/18975) used
  the same `audio_input_mode` key and was closed as stale after an
  api_server rewrite. `audio_analyze` exists only in
  [#27412](https://github.com/NousResearch/hermes-agent/pull/27412) (open;
  sweeper `keep_open` — SSRF / sync URL / base-URL issues) and
  [#26158](https://github.com/NousResearch/hermes-agent/pull/26158)
  (closed per maintainer request). Video got the analogue tool
  (`video_analyze`, merged
  [#19301](https://github.com/NousResearch/hermes-agent/pull/19301);
  [#42145](https://github.com/NousResearch/hermes-agent/issues/42145)
  closed `implemented_on_main`); audio did not. Adjacent, not this
  feature: STT echo [#58859](https://github.com/NousResearch/hermes-agent/pull/58859)
  (merged) and this fork's Telegram quote wrapping
  [#98419](https://github.com/NousResearch/hermes-agent/pull/98419);
  outbound TTS routing
  [#17833](https://github.com/NousResearch/hermes-agent/pull/17833)
  (merged). A 2026-08-30 search found **no** merged implementation of the
  full bundle (queries: `audio_analyze`, `audio_input_mode`, `native audio`,
  `audio_routing`). If #90206 or #27412 land, rebase onto that shape
  rather than keeping a third routing module.
- **Merge risk:** `gateway/run_inbound.py`, `gateway/run_turn_runner.py`,
  `agent/vision_message_prep.py`, `agent/turn_finalizer.py`, `toolsets.py`,
  and `image_routing.py` (`_supports_capability_override`) are high-churn.
  `audio_routing.py` / `audio_tools.py` are fork-only until upstream adds
  the same path. After each weekly merge
  re-run `tests/agent/test_audio_routing.py`,
  `tests/run_agent/test_audio_rejection_fallback.py`,
  `tests/tools/test_audio_analyze.py`, and
  `tests/gateway/test_native_audio_buffer_isolation.py`.
- **Known limitations (accepted):** one native clip per turn; native audio
  is current-turn-only (not persisted); `video_analyze` stays opt-in and
  this path never sends a native video container; Anthropic / Bedrock /
  Codex native attach is unimplemented; STT echo still runs alongside
  native attach.

---

## 2026-08-14 — Opt-in video frame extraction

- **Status:** active (fork-local).
- **Summary:** inbound video has no native container on this fork's main
  turn: visuals are sampled stills (`image_url`) and the soundtrack uses
  `agent.audio_input_mode`. `video.frame_extract` is **off by default**.
  Empty `provider` uses a best-effort ffmpeg fallback; named
  `video.providers.<name>` command providers mirror STT command providers
  (write stills into `{output_dir}`, print a JSON manifest). Frames are
  tagged `_hermes_ephemeral: video_frame`, persist as `[video]`, strip
  after the turn, and are omitted from provider API copies.
- **Files:** `agent/video_frame_extract.py` (new; absent on `upstream/main`),
  `agent/agent_runtime_helpers.py`, `agent/audio_routing.py`,
  `agent/turn_finalizer.py`, `gateway/run.py`, `gateway/session_state.py`,
  `hermes_cli/config_defaults.py` (`video.frame_extract` / `video.providers`),
  `tests/agent/test_video_frame_extract.py`,
  `tests/gateway/test_native_video_frame_buffer.py`.
- **Upstream disposition:** **mixed — exact feature never requested;
  adjacent `video_analyze` chose the opposite design.** Merged
  [#19301](https://github.com/NousResearch/hermes-agent/pull/19301) ships
  `video_analyze` as whole-video `video_url` with **no ffmpeg, no frame
  extraction**. Sweeper closed
  [#42145](https://github.com/NousResearch/hermes-agent/issues/42145) /
  [#41366](https://github.com/NousResearch/hermes-agent/issues/41366) as
  `implemented_on_main`: gateway injects a cached path + note, the agent
  must call the tool. teknium1 on #42145: "The existing implementation
  uses native multimodal video input rather than ffmpeg frame sampling."
  The ffmpeg-first tool PR
  [#2294](https://github.com/NousResearch/hermes-agent/pull/2294) was
  closed unmerged, superseded by #19301. Frame extraction is reappearing
  only as a **`video_analyze` transport fallback** when providers reject
  `video_url`:
  [#72275](https://github.com/NousResearch/hermes-agent/issues/72275)
  (open), [#82996](https://github.com/NousResearch/hermes-agent/pull/82996)
  (open, local VLMs), [#97318](https://github.com/NousResearch/hermes-agent/pull/97318)
  (open, dialect ladder). Adjacent, not this feature: native-video-to-main-model
  requests [#88141](https://github.com/NousResearch/hermes-agent/issues/88141)
  / [#49565](https://github.com/NousResearch/hermes-agent/issues/49565);
  BFL FLUX 3 **generation** promo tools added in
  [#74963](https://github.com/NousResearch/hermes-agent/pull/74963) and
  removed in [#94599](https://github.com/NousResearch/hermes-agent/pull/94599).
  A 2026-08-30 search found **no** `video.frame_extract`,
  `video_frame_extract.py`, `_hermes_ephemeral: video_frame`, or
  `video.providers` in issues/PRs. If #97318 lands, that is still tool-side
  fallback, not this gateway/ephemeral still path — keep them distinct.
- **Merge risk:** `gateway/run.py` and `turn_finalizer.py` are shared with
  the native-audio entry. `video_frame_extract.py` is fork-only. After each
  weekly merge re-run `tests/agent/test_video_frame_extract.py` and
  `tests/gateway/test_native_video_frame_buffer.py`.
- **Known limitations (accepted):** extraction is opt-in and budget-capped;
  no native video container on the main turn; ffmpeg must be on PATH unless
  a command provider is configured; if Telegram/gateway rejects or the
  sampler fails, the agent still sees the path note only.

---

## 2026-08-14 — `AGENTS.md` doctrine / map / contracts overlay

- **Status:** active (fork-local docs overlay).
- **Summary:** this checkout's `AGENTS.md` adds four sections that
  `upstream/main` does not have: **Short Doctrine**, **Documentation Map**,
  **Domain Contracts**, and **Operational logs**. They compress upstream
  invariants (cache, session toolsets, config loaders, profiles, slash
  commands, plugins, cron durability) into cards, index canonical docs,
  and add a `hermes logs` symptom table plus Windows
  `%LOCALAPPDATA%\hermes` paths. Runtime behavior is unchanged. The
  *Fork-local AE2* subsection in the same file is a separate ledger entry.
- **Files:** `AGENTS.md` only (Documentation Map also points at this
  `FORK.md`).
- **Upstream disposition:** **no-request-found for the four named
  sections; overlapping specialist docs live on the website; upstream
  wants a smaller `AGENTS.md`, not a larger one.** Merged
  [#81146](https://github.com/NousResearch/hermes-agent/pull/81146) added
  `website/docs/developer-guide/codebase-ownership.md` — the closest
  subsystem routing index, not an `AGENTS.md` Documentation Map.
  Architecture, prompt assembly, compression/caching, gateway internals,
  plugins, and `hermes logs` CLI flags already exist under `website/docs/`
  / `docs/session-lifecycle.md`. Open work aims to **split or shrink**
  `AGENTS.md` because it is loaded into session context:
  [#52821](https://github.com/NousResearch/hermes-agent/issues/52821),
  [#50165](https://github.com/NousResearch/hermes-agent/issues/50165),
  [#57554](https://github.com/NousResearch/hermes-agent/pull/57554),
  [#72450](https://github.com/NousResearch/hermes-agent/pull/72450),
  [#63854](https://github.com/NousResearch/hermes-agent/pull/63854). A
  2026-08-30 search found **no** `"Short Doctrine"`, `"Documentation Map"`,
  `"Domain Contracts"` (as this index), or operational-logs symptom table
  (queries: those phrases, `expand AGENTS.md`, `hermes logs symptom`).
  Treat as a local overlay; do not expect it to merge as-is.
- **Merge risk:** every weekly `main` → `dev` merge of `AGENTS.md`
  conflicts here **and** on the Fork-local AE2 subsection. After merge,
  keep the four overlay sections, the AE2 recipe, and the `FORK.md`
  pointer; take upstream wording for shared sections.
- **Known limitations (accepted):** the overlay can drift from website
  specialist docs; it increases `AGENTS.md` size against upstream's
  truncation concern (#52821).

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
  on this payload. This fork proposed the wrapping upstream as
  [#98419](https://github.com/NousResearch/hermes-agent/pull/98419)
  (open, 2026-08-30). No earlier third-party request for collapsed-quote
  STT wrapping was found (queries: `echo_transcripts`, `stt echo`,
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
- **Merge risk:** `gateway/run_inbound.py` echo sites (immediate + pending)
  and `plugins/platforms/telegram/adapter.py` `send()` / HTML path are
  high-churn. `gateway/stt_echo.py` is fork-only — conflict only if
  upstream adds the same path. After each weekly merge re-run
  `tests/gateway/test_stt_echo_format.py`,
  `tests/gateway/test_telegram_format.py`, and
  `tests/gateway/test_telegram_voice_v0_regressions.py`.
- **Known limitations (accepted):** non-Telegram platforms still get the
  full `🎙️ "…"` line. If Telegram rejects the HTML parse, the adapter
  strips tags and the collapse is lost on that fallback. Expandable
  quotes need Telegram Bot API 7.3+.

---

## 2026-08-11 — Flex service tier + OpenRouter `:variant` slugs

- **Status:** active (fork-local). Follow-up 2026-08-30 (`d7b2a48b0c`)
  keeps unknown `:suffix` accepted when the listed base matches.
- **Summary:** `agent.service_tier` understands OpenRouter/OpenAI-compatible
  `flex` as well as `priority` (`fast`/`on` stay aliases for `priority`).
  `parse_service_tier` / `strip_model_variant_suffix` live in
  `hermes_constants.py`. `resolve_service_tier_overrides` gives OpenRouter
  `flex`/`priority` without the fast-model gate; other providers keep the
  gated mapping. `_effective_request_overrides` applies the tier in
  `build_api_kwargs` for every API entry point. Auxiliary tasks gain
  shortcuts `service_tier` and `providers` (OpenRouter provider-order).
  Catalog validation, `/model`, custom-provider matching, and reasoning
  overrides accept `vendor/model:variant` slugs.
- **Files:** `hermes_constants.py`, `hermes_cli/models.py`,
  `hermes_cli/model_switch.py`, `agent/auxiliary_client.py`,
  `agent/chat_completion_helpers.py`, `agent/agent_init.py`, `cli.py`,
  `gateway/run.py`, `tui_gateway/server.py`,
  `hermes_cli/config_defaults.py`, `hermes_cli/cli_agent_setup_mixin.py`,
  `tests/test_hermes_constants.py`, `tests/agent/test_auxiliary_client.py`,
  `tests/cli/test_fast_command.py`, `tests/gateway/test_fast_command.py`,
  `tests/hermes_cli/test_model_validation.py`,
  `website/docs/user-guide/configuration.md`,
  `website/docs/user-guide/features/provider-routing.md`.
- **Upstream disposition:** **mixed — variant whitelist merged; flex
  tier and unknown-suffix acceptance are not.** Merged
  [#94103](https://github.com/NousResearch/hermes-agent/pull/94103)
  teaches `/model` that `:nitro`/`:floor`/`:exacto`/`:online` are
  request-time modifiers on a listed base; **unknown suffixes stay
  rejected** (teknium1: "unknown suffixes and unknown bases are still
  rejected"). That is OpenRouter-only `_openrouter_variant_base`, not
  `strip_model_variant_suffix`. On main, `agent.service_tier` +
  `resolve_fast_mode_overrides` is **priority-only**; `flex` is warned
  and ignored. Flex is an open cluster: teknium1 on
  [#16335](https://github.com/NousResearch/hermes-agent/pull/16335)
  rejected a parallel `api_service_tier` key — "Adding `flex` is
  naturally just one more accepted value on the same key";
  [#37059](https://github.com/NousResearch/hermes-agent/pull/37059)
  (open) follows that shape for OpenAI + Gemini;
  [#12700](https://github.com/NousResearch/hermes-agent/issues/12700)
  (open) is the Gemini request;
  [#5157](https://github.com/NousResearch/hermes-agent/pull/5157)
  (open) is the older broad service-tier PR;
  [#83398](https://github.com/NousResearch/hermes-agent/pull/83398)
  (open) is TUI tier propagation (priority on current main). Adjacent:
  [#97820](https://github.com/NousResearch/hermes-agent/issues/97820) /
  [#97839](https://github.com/NousResearch/hermes-agent/pull/97839)
  (open) — strip routing suffixes before models.dev lookup. A 2026-08-30
  search found **no** `parse_service_tier`,
  `resolve_service_tier_overrides`, or `_effective_request_overrides` on
  main. `_routing_variant_catalog_base` (delegate-model entry) overlaps
  this file; do not conflate them. If #37059 lands, drop the fork parser
  in favor of upstream's extended `_parse_service_tier_config` and keep
  only the unknown-suffix divergence if still wanted.
- **Merge risk:** `hermes_cli/models.py` is shared with the delegate-model
  entry and with merged #94103. `gateway/run.py` / `cli.py` service-tier
  loaders churn. After each weekly merge re-run
  `tests/hermes_cli/test_model_validation.py`,
  `tests/test_hermes_constants.py`, and the CLI/gateway `test_fast_command`
  files.
- **Known limitations (accepted):** unknown `:suffix` against a listed
  base is accepted here and **rejected on upstream** (#94103). Auxiliary
  `providers` is OpenRouter-only. ZIP/docs still describe flex as
  OpenRouter/OpenAI-compatible, not a guarantee every provider honors it.

---

## 2026-08-11 — MerchantBench Hermes adapter

- **Status:** active (fork-local). Follow-up 2026-08-29 brings `/act`
  env parity (trace cursor, sibling env calls, usage capture, history
  sanitization).
- **Summary:** in-tree `merchantbench_adapter/` is a public reconstruction
  of the unpublished SDK contract MerchantBench's Hermes launcher expects:
  env tools registered into Hermes, forwarded via `/act`, with
  `end_of_step`, HTTP 425 stale steps, observation retries, and
  `max_observations` shutdown. A persistent `AIAgent` loop consumes
  observations; OpenRouter identity headers (`HTTP-Referer`, `X-Title`)
  can show MerchantBench on the dashboard
  (`OPENROUTER_HTTP_REFERER` / `OPENROUTER_X_TITLE`). `run_agent.py` also
  adds `google/gemini-3` and `stealth/` to reasoning-prefix fallback.
- **Files:** `merchantbench_adapter/` (`__init__.py`, `__main__.py`,
  `bridge.py`, `history.py`, `runtime.py`, `usage_capture.py`),
  `tests/test_merchantbench_adapter.py`, `run_agent.py` (reasoning
  prefixes), `REPO_CULTURE_HERMES.md` (research note only).
- **Upstream disposition:** **no-request-found for MerchantBench itself;
  custom OpenRouter attribution via env vars was explicitly dropped.** A
  2026-08-30 search of NousResearch/hermes-agent issues, PRs, and code
  found **zero** hits for `merchantbench`, `MerchantBench`,
  `end_of_step`, `max_observations`, `OPENROUTER_HTTP_REFERER`, or
  `OPENROUTER_X_TITLE`. Adjacent OpenRouter branding: merged
  [#1105](https://github.com/NousResearch/hermes-agent/pull/1105) /
  [#20282](https://github.com/NousResearch/hermes-agent/pull/20282)
  (salvage of [#13649](https://github.com/NousResearch/hermes-agent/pull/13649);
  **dropped the env-var override** per ".env is for secrets only" —
  "If that feature is wanted later, it should come back under
  `openrouter.title` / `openrouter.referer` in config.yaml");
  [#61732](https://github.com/NousResearch/hermes-agent/pull/61732)
  reapplies headers after `/model`. Config-based header merge is still
  open as [#21588](https://github.com/NousResearch/hermes-agent/issues/21588);
  fix PRs [#21590](https://github.com/NousResearch/hermes-agent/pull/21590)
  / [#22485](https://github.com/NousResearch/hermes-agent/pull/22485)
  closed unmerged. Generic environment-adapter SDK
  [#93305](https://github.com/NousResearch/hermes-agent/issues/93305) was
  closed into research index
  [#93319](https://github.com/NousResearch/hermes-agent/issues/93319)
  (`needs-decision`, no implementation). Keep the adapter fork-local;
  do not upstream `OPENROUTER_*` env vars.
- **Merge risk:** the package is fork-only (conflict only if upstream
  adds a same-named tree). `run_agent.py` reasoning-prefix edits sit on
  a hot file. After each weekly merge re-run
  `tests/test_merchantbench_adapter.py`.
- **Known limitations (accepted):** reconstructed unpublished contract —
  MerchantBench launcher changes can break it without a Hermes signal.
  Dashboard branding uses env vars that upstream already refused for
  Hermes itself; they apply only inside this adapter process.

---

## 2026-08-10 — `hermes update`: `updates.branch` + shallow apply fetch

- **Status:** active (fork-local). Follow-up 2026-08-18 keeps apply-path
  fetches at `--depth 1` on shallow checkouts and covers that with tests
  that stay off the npm/backup path.
- **Summary:** two update-pipeline patches for this checkout. (1)
  `updates.branch` in `config.yaml` (default `main`) is the target when
  `hermes update` omits `--branch`; explicit `--branch` still wins;
  ZIP-fallback still refuses non-`main`. (2) Shared shallow helpers
  `_is_shallow_repository` / `_shallow_fetch_depth_args` so apply-path
  `git fetch origin <branch>` matches the check path. A bare fetch
  against a shallow **local** origin fails with `Could not read SHA` /
  `did not send all necessary objects` (this tree is often
  `G:/GitHubImports/Hermes`).
- **Files:** `hermes_cli/main.py` (`_resolve_update_branch`),
  `hermes_cli/update_cmd.py`, `hermes_cli/subcommands/update.py`,
  `hermes_cli/config_defaults.py` (`updates.branch`),
  `tests/hermes_cli/test_cmd_update.py`.
- **Upstream disposition:** **(1) proposed once, closed unmerged, no
  maintainer thread. (2) same apply-path `--depth 1` idea was closed as
  implemented-on-main via a different mechanism; current upstream
  direction argues against `--depth 1` fetches.** Identical
  `updates.branch` PR
  [#44422](https://github.com/NousResearch/hermes-agent/pull/44422)
  (jakehewitt) was self-closed 2026-06-25 with zero reviews. Adjacent
  fork-deploy pain:
  [#72789](https://github.com/NousResearch/hermes-agent/issues/72789)
  (open) — update banner when `origin` ≠ upstream or default branch ≠
  `main`. For shallow: check path on main still inlines `--depth 1`;
  apply path is still a **bare** `git fetch`.
  [#80124](https://github.com/NousResearch/hermes-agent/pull/80124)
  proposed adding `--depth 1` to apply (same as this fork);
  teknium1 closed it pointing at merged
  [#86318](https://github.com/NousResearch/hermes-agent/pull/86318)
  (compare-API count recovery, not depth on apply): "rather than adding
  `--depth 1` to the apply fetch." Open
  [#94477](https://github.com/NousResearch/hermes-agent/issues/94477) /
  [#94680](https://github.com/NousResearch/hermes-agent/pull/94680)
  argue `--depth 1` **poisons** the shallow boundary and that putting it
  on apply "would make the breakage permanent" (Halldrix). This fork
  keeps apply `--depth 1` because the failure mode here is a shallow
  *local origin* (`Could not read SHA`), which #86318 does not address.
  A 2026-08-30 search found **no** `_shallow_fetch_depth_args` /
  `_is_shallow_repository` helpers on main. Revisit (2) if #94680 lands.
- **Merge risk:** `hermes_cli/update_cmd.py` is one of the hottest files
  in the weekly merge. Conflict hotspots: `_resolve_update_branch`, the
  apply-path `git fetch origin` argv, and the check-path shallow block.
  After each weekly merge re-run `tests/hermes_cli/test_cmd_update.py`.
- **Known limitations (accepted):** ZIP-fallback cannot use a non-`main`
  `updates.branch`. Apply `--depth 1` diverges from the maintainer-stated
  direction on #80124 / #94680; it is kept for local-clone origin
  topology, not for GitHub.com installer clones. **Desktop Update used to
  ignore this key** (hardcoded `--branch main` when `updates.json` was
  missing) — that is the 2026-08-30 Desktop-update entry at the top of
  this ledger.

---

## 2026-08-10 — Fork-local AE2 local CI pipeline

- **Status:** active (fork-local tooling).
- **Summary:** `run.ps1` is the public entry; `build.ps1` orchestrates
  fast/full profiles, stage caching, and AE2-style `report.json`.
  `scripts/ci/ae2_local.py` runs Python stages (`changed`, `lint`,
  `typecheck`, `compile`) and reuses upstream
  `scripts/ci/classify_changes.py` for lane selection. Canonical Python
  tests still go through `scripts/run_tests.sh`; frontend through
  `npm run check`. This is **not** an upstream Hermes contract — see
  `AGENTS.md` *Fork-local AE2*.
- **Files:** `run.ps1`, `build.ps1`, `scripts/ci/ae2_local.py`,
  `tests/test_ae2_local.py`, `.gitignore` (`.ci_cache/`, `.enforcer/`,
  `.temporary/`), `AGENTS.md` (Fork-local AE2 subsection only).
- **Upstream disposition:** **no-request-found.** A 2026-08-30 search
  found **zero** issues/PRs for `AgentEnforcer`, `AgentEnforcer2`,
  `ae2_local`, root `run.ps1` / `build.ps1` CI, `.ci_cache`, or
  `.enforcer/` as a cache dir. Upstream standardizes on
  `scripts/run_tests.sh` ([#11577](https://github.com/NousResearch/hermes-agent/pull/11577)
  merged; open [#83388](https://github.com/NousResearch/hermes-agent/pull/83388)
  refuses bare `pytest`). Closest adjacent ask is a **Windows test
  launcher only**:
  [#84437](https://github.com/NousResearch/hermes-agent/issues/84437) /
  [#84546](https://github.com/NousResearch/hermes-agent/pull/84546)
  (open `scripts/run_tests.ps1`). OS CI lanes
  [#77992](https://github.com/NousResearch/hermes-agent/pull/77992)
  (merged) are GitHub Actions, not a local orchestrator. No maintainer
  rejection of AE2 specifically — it was never proposed. Do not
  upstream this stack; if #84546 lands, keep calling `run_tests.sh` (or
  that PS1) rather than duplicating hermetic isolation.
- **Merge risk:** `AGENTS.md` is shared with the doctrine-overlay entry.
  `scripts/ci/classify_changes.py` is upstream — consume it, do not
  fork it. After each weekly merge re-run `tests/test_ae2_local.py` and
  a `./run.ps1 -Fast -SkipLaunch` smoke if the classifier or
  `run_tests.sh` changed.
- **Known limitations (accepted):** the fast profile runs only changed
  Python test files and does not infer tests from production-source
  changes. It never replaces task-specific `scripts/run_tests.sh`
  verification.
