---
title: Provider Routing
description: Configure OpenRouter or Nous Portal provider preferences to optimize for cost, speed, or quality.
sidebar_label: Provider Routing
sidebar_position: 7
---

# Provider Routing

When using [OpenRouter](https://openrouter.ai) or [Nous Portal](/integrations/nous-portal) as your LLM provider, Hermes Agent supports **provider routing** — fine-grained control over which underlying AI providers handle your requests and how they're prioritized.

OpenRouter routes requests to many providers (e.g., Anthropic, Google, AWS Bedrock, Together AI). Provider routing lets you optimize for cost, speed, quality, or enforce specific provider requirements.

:::tip
Traffic routed through Nous Portal respects the same provider preferences — and Portal subscribers get 10% off token-billed providers.
:::

## Configuration

Add a `provider_routing` section to your `~/.hermes/config.yaml`:

```yaml
provider_routing:
  sort: "price"           # How to rank providers
  only: []                # Whitelist: only use these providers
  ignore: []              # Blacklist: never use these providers
  order: []               # Explicit provider priority order
  require_parameters: false  # Only use providers that support all parameters
  data_collection: null   # Control data collection ("allow" or "deny")
```

:::info
Provider routing only applies when using OpenRouter or Nous Portal. It has no effect with direct provider connections (e.g., connecting directly to the Anthropic API).
:::

## Options

### `sort`

Controls how OpenRouter ranks available providers for your request.

| Value | Description |
|-------|-------------|
| `"price"` | Cheapest provider first |
| `"throughput"` | Fastest tokens-per-second first |
| `"latency"` | Lowest time-to-first-token first |

```yaml
provider_routing:
  sort: "price"
```

### `only`

Whitelist of provider slugs. When set, **only** these providers will be used. All others are excluded. Use the lowercase slug shown by OpenRouter for each provider.

```yaml
provider_routing:
  only:
    - "anthropic"
    - "google"
```

### `ignore`

Blacklist of provider names. These providers will **never** be used, even if they offer the cheapest or fastest option.

```yaml
provider_routing:
  ignore:
    - "together"
    - "deepinfra"
```

### `order`

Explicit priority order. Providers listed first are preferred. Unlisted providers are used as fallbacks.

```yaml
provider_routing:
  order:
    - "anthropic"
    - "google"
    - "amazon-bedrock"
```

### `require_parameters`

When `true`, OpenRouter will only route to providers that support **all** parameters in your request (like `temperature`, `top_p`, `tools`, etc.). This avoids silent parameter drops.

```yaml
provider_routing:
  require_parameters: true
```

### `data_collection`

Controls whether providers can use your prompts for training. Options are `"allow"` or `"deny"`.

```yaml
provider_routing:
  data_collection: "deny"
```

## Practical Examples

### Optimize for Cost

Route to the cheapest available provider. Good for high-volume usage and development:

```yaml
provider_routing:
  sort: "price"
```

### Optimize for Speed

Prioritize low-latency providers for interactive use:

```yaml
provider_routing:
  sort: "latency"
```

### Optimize for Throughput

Best for long-form generation where tokens-per-second matters:

```yaml
provider_routing:
  sort: "throughput"
```

### Lock to Specific Providers

Ensure all requests go through a specific provider for consistency:

```yaml
provider_routing:
  only:
    - "anthropic"
```

### Avoid Specific Providers

Exclude providers you don't want to use (e.g., for data privacy):

```yaml
provider_routing:
  ignore:
    - "together"
    - "lepton"
  data_collection: "deny"
```

### Preferred Order with Fallbacks

Try your preferred providers first, fall back to others if unavailable:

```yaml
provider_routing:
  order:
    - "anthropic"
    - "google"
  require_parameters: true
```

### Per-Model Routing Overrides

Use `provider_routing.models.<model>` when one model needs its own routing without changing the flat defaults every other model inherits. The key is the exact model id (as sent to the API); its entries win over the flat keys **per key** — anything you don't list falls through to the flat defaults:

```yaml
provider_routing:
  sort: throughput                    # flat default for all models
  models:
    google/gemini-3.7-flash:
      only: ["google-ai-studio"]      # hard-pin this model to one provider
    qwen/qwen3.8-27b:
      order: ["reka/fp8"]             # soft preference, fallbacks allowed
```

Per-model overrides apply on every surface (CLI, messaging gateway, TUI, Desktop) and are re-resolved automatically on a mid-session `/model` switch. Pinning a provider per model also keeps OpenRouter's prompt cache warm: repeatedly hitting the same upstream provider preserves the cached prefix, while load-balancing across providers loses it.

### Sticky Order

A manual `order` turns off [OpenRouter's own sticky routing](https://openrouter.ai/docs/guides/best-practices/prompt-caching). Without a pin, the aggregator may hop to a different upstream provider on every request. Prompt cache is **not** shared across those providers, so a long agent session can repay the full prefill on each hop.

`sticky_order` is an opt-in pin (default **off**). Hermes sends only the current slug from the resolved `order` — intersected with `only` if you set one — and rotates to the next slug when that provider fails. Per-model overlay (`models.<id>.order`) applies on CLI, messaging gateway, TUI, and Desktop; cron uses the flat `order` from config. Batch does not load `provider_routing` from config — it only has an order (and therefore a sticky pool) when `providers_order` is passed explicitly. Turn it on for long agent sessions that use `order` and pay a meaningful prefill.

```yaml
provider_routing:
  order: ["z-ai/fp8", "novita/fp8"]
  sticky_order:
    enabled: true      # default false — opt-in
    ttl_seconds: 600   # default 600
```

| Event | What happens |
|-------|----------------|
| Timeout, overload, or server error (5xx) | Rotate; the retry (if the error is retryable) goes to the next slug. Every slug is attempted before eager model fallback |
| Rate limit (429) or empty/invalid response | Stay on the current slug. Normal retry / model-fallback still applies — these errors do not walk the pin pool |
| Every slug has failed with timeout / overload / 5xx | Eager transport-failure model fallback may fire (not before the last slug has failed) |
| Idle longer than `ttl_seconds` between requests | Reset to the first slug (every provider's cache is already cold). In-request retry backoff does not count as idle |
| A single eligible slug (pool of one) | Pins without rotation |

If `order ∩ only` is empty, `sticky_order` silently disables with a warning in the log.

The pin applies on CLI, gateway, TUI, Desktop, cron, and subagents. Batch gets it only when `providers_order` is passed explicitly (batch does not read `provider_routing` from config). Per-model overlay applies on CLI, messaging gateway, TUI, and Desktop; cron uses the flat `order` from config. State is per-agent. Only the OpenRouter / Nous Portal **chat-completions** path is active; any other `api_mode` (`anthropic_messages`, `codex_responses`, and future modes) and direct provider connections are a no-op.

## How It Works

Provider routing preferences are passed to OpenRouter or Nous Portal on agent chat requests and iteration-limit summaries via the `extra_body.provider` field. (`extra_body` is the OpenAI Python SDK argument; it becomes the top-level `provider` object in the JSON request.) Auxiliary tasks are configured independently: `auxiliary.<task>.providers` is the concise ordered-provider form for OpenRouter, while `auxiliary.<task>.extra_body` remains available for the full routing object.

- **CLI mode** — configured in `~/.hermes/config.yaml`, loaded at startup
- **Gateway mode** — same config file, loaded when the gateway starts

The routing config is read from `config.yaml` and passed as parameters when creating the `AIAgent`:

```
providers_allowed  ← from provider_routing.only
providers_ignored  ← from provider_routing.ignore
providers_order    ← from provider_routing.order
provider_sort      ← from provider_routing.sort
provider_require_parameters ← from provider_routing.require_parameters
provider_data_collection    ← from provider_routing.data_collection
```

:::tip
You can combine multiple options. For example, sort by price but exclude certain providers and require parameter support:

```yaml
provider_routing:
  sort: "price"
  ignore: ["together"]
  require_parameters: true
  data_collection: "deny"
```
:::

## Default Behavior

When no `provider_routing` section is configured (the default), the aggregator uses its own default routing logic, which generally balances cost and availability automatically.

:::tip Provider Routing vs. Fallback Models
Provider routing controls which **sub-providers behind OpenRouter or Nous Portal** handle your requests. For automatic failover to an entirely different provider when your primary model fails, see [Fallback Providers](/user-guide/features/fallback-providers).
:::
