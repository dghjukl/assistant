# External Inference — Multi-Provider (Optional Feature)

This document describes the optional Budget-Governed External Inference feature in EOS. It covers what the feature does, how to configure it, and what happens under every operating condition.

## What This Feature Does

EOS runs entirely locally by default. No external API calls are made, no API key is required, and no money is ever spent without explicit user configuration.

The External Inference feature allows you to optionally allow EOS to call external inference providers when local inference produces a poor or failed result. All external calls are:

- Governed by a user-set monthly spend budget
- Limited to localhost-origin requests only
- Triggered only when local inference fails to produce a usable response
- Routed through a deterministic multi-provider router
- Tracked in a persistent local ledger
- Fully optional — the system operates identically without them

## Provider Support

EOS supports six inference backends through a unified provider abstraction layer:

| Provider | Type | Cost Tier | Quality | Default Model |
|----------|------|-----------|---------|---------------|
| `local` | Local (llama-server) | Free | — | Configured primary model |
| `huggingface` | Remote | Cheap | Budget | mistralai/Mistral-7B-Instruct-v0.2 |
| `openrouter` | Remote | Cheap | Standard | meta-llama/llama-3.1-8b-instruct:free |
| `openai` | Remote | Moderate | Premium | gpt-4o-mini |
| `anthropic` | Remote | Moderate | Premium | claude-haiku-4-5-20251001 |
| `gemini` | Remote | Cheap | Premium | gemini-2.0-flash |

**OpenRouter** is the recommended gateway for open-weight hosted models (Llama, Mistral, DeepSeek, Qwen, and others). A single OpenRouter API key provides access to all of them.

## Default State

External inference is **disabled by default**. None of the following will ever happen without your explicit configuration:

- No external API calls are made
- No API key is required at startup
- No budget is consumed
- No external network traffic is generated

## Localhost-Only Enforcement

External inference can only be triggered by requests that originate from localhost (127.0.0.1 or ::1). This is enforced in the backend policy engine, not only in the UI.

The following origins **can never trigger external inference**, regardless of configuration:

- Discord bot messages
- LAN clients (any 192.168.x.x, 10.x.x.x, 172.16.x.x address)
- Remote web sessions
- External API clients
- Connectors or relays of any kind

If a non-local origin somehow reaches the escalation decision point, the policy engine will deny the request with reason `non_local_origin` and write a denial record to the ledger.

## Configuration

The external inference settings live in `config.json` under `external_inference`. You can also edit most settings through the Admin panel under the **Ext. Inference** tab.

### Global controls

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Master gate. Must be `true` for any external calls to occur. |
| `routing_mode` | `default` | How the router selects a provider (see Routing Modes). |
| `default_provider` | `huggingface` | Provider used when `routing_mode` is `default`. |
| `fallback_order` | `[huggingface, openrouter, openai, anthropic, gemini]` | Provider order for `fallback` routing. |
| `enabled_providers` | `[huggingface]` | Only listed providers are considered by the router. Add a provider here after setting its API key. |
| `monthly_budget_usd` | `0.0` | Monthly spend cap in USD. `0` = no spend permitted. |
| `monthly_budget_override_usd` | `null` | Override for the current billing cycle only. |
| `per_request_cap_usd` | `null` | Maximum cost for a single request. `null` = no per-request cap. |
| `daily_request_cap` | `null` | Maximum non-denied requests per calendar day. `null` = unlimited (budget still applies). |
| `approval_mode` | `ask_for_paid_calls` | `never` / `ask_for_paid_calls` / `always` |
| `escalation_mode` | `disabled` | `disabled` / `emergency_only` / `constrained` / `balanced` / `permissive` |
| `current_billing_cycle_start` | `null` | YYYY-MM-DD. `null` = auto-set to the 1st of the current month. |

### Per-provider sub-configs

Each provider can be tuned individually under its own sub-key:

```json
"external_inference": {
  "huggingface": {
    "model_id": "mistralai/Mistral-7B-Instruct-v0.2",
    "timeout_sec": 30.0,
    "max_retries": 1
  },
  "openai": {
    "model_id": "gpt-4o-mini",
    "timeout_sec": 30.0,
    "max_retries": 1
  },
  "anthropic": {
    "model_id": "claude-haiku-4-5-20251001",
    "timeout_sec": 60.0,
    "max_retries": 1
  },
  "gemini": {
    "model_id": "gemini-2.0-flash",
    "timeout_sec": 30.0,
    "max_retries": 1
  },
  "openrouter": {
    "model_id": "meta-llama/llama-3.1-8b-instruct:free",
    "timeout_sec": 30.0,
    "max_retries": 1
  }
}
```

## Routing Modes

The router selects an ordered candidate list based on the `routing_mode`:

| Mode | Behaviour |
|------|-----------|
| `default` | Use the single configured `default_provider`. No fallback. |
| `explicit` | Use the exact provider and model specified per-call. Bypasses enabled list. |
| `cheapest` | Order `enabled_providers` by cost tier, cheapest first (free → cheap → moderate). |
| `best_quality` | Order `enabled_providers` by quality tier, best first (premium → standard → budget). |
| `local_only` | Restrict candidates to local (llama-server) providers only. |
| `remote_only` | Restrict candidates to remote (cloud) providers only. |
| `fallback` | Follow `fallback_order` in sequence; skip providers not in `enabled_providers`. |

For all modes, any candidate is automatically skipped if:
- Its API key is missing (for non-local providers)
- The estimated call cost exceeds the remaining budget
- The provider is not registered in the adapter registry

On skip or failure, the router logs the reason and tries the next candidate. On complete exhaustion, the result is `ok=False` with `error_code="all_providers_failed"`.

## Setting API Keys

Each provider uses a named key in the OS system keyring. Keys are **never** stored in `config.json` or any plaintext file and are never returned by any API endpoint after they are saved.

| Provider | Keyring key name |
|----------|-----------------|
| `huggingface` | `huggingface_api_key` |
| `openai` | `openai_api_key` |
| `anthropic` | `anthropic_api_key` |
| `gemini` | `gemini_api_key` |
| `openrouter` | `openrouter_api_key` |

### Via the Admin panel

1. Open the Admin panel → **Ext. Inference** tab → **Providers**
2. Select the provider you want to configure
3. Paste its API key and click **Save Key**

The Admin panel shows only whether a key is configured (✓ or ✗) — never the key value.

### Via the admin API

```http
POST /admin/external-inference/api-key
Content-Type: application/json

{"provider": "openai", "api_key": "sk-..."}
```

`provider` defaults to `huggingface` if omitted (backward compatible).

To delete a key:

```http
DELETE /admin/external-inference/api-key?provider=openai
```

### Via environment variable

At startup, the following environment variables are read as fallbacks when a keyring entry is absent:

```
EOS_HUGGINGFACE_API_KEY=hf_...
EOS_OPENAI_API_KEY=sk-...
EOS_ANTHROPIC_API_KEY=sk-ant-...
EOS_GEMINI_API_KEY=AIza...
EOS_OPENROUTER_API_KEY=sk-or-...
```

## Enabling a Provider

Adding a provider to `enabled_providers` alone is not enough — the API key must also be set. The router will skip any provider that has no key configured (even if it is listed in `enabled_providers`).

Recommended minimum setup for a single provider (e.g. OpenRouter):

1. Set the API key: `POST /admin/external-inference/api-key` with `{"provider": "openrouter", "api_key": "..."}`
2. Add to `enabled_providers`: `PATCH /admin/external-inference/config` with `{"enabled_providers": ["openrouter"]}`
3. Set `default_provider` to `openrouter` (or set `routing_mode` to `fallback`)
4. Set a non-zero `monthly_budget_usd`
5. Set `escalation_mode` to `constrained` or higher
6. Set `enabled` to `true`

## Budget Controls

All budget enforcement happens **locally** before any external call is made. The system never relies on provider-side billing limits.

**How the budget is enforced:**

1. Before each potential external call, the policy engine estimates the cost conservatively.
2. It queries the local ledger for total spend in the current billing cycle.
3. If `estimated_cost + spent >= budget`, the request is denied with `budget_exceeded`.
4. The estimate is intentionally high to avoid under-counting.

Default conservative rate estimates (combined per 1K tokens):

| Provider | Estimated rate |
|----------|---------------|
| local | $0.000 (free) |
| huggingface | $0.005 |
| openrouter | $0.002 |
| openai | $0.010 |
| anthropic | $0.015 |
| gemini | $0.003 |

**When monthly_budget_usd is 0.0:**
All external calls are denied with reason `zero_budget`. This is the default state.

**Billing cycle:**
By default the cycle resets on the 1st of each month. You can set `current_billing_cycle_start` to any YYYY-MM-DD date to align the cycle with your provider invoice.

## Runtime Invocation

External inference is invoked automatically by the orchestrator when **all** of the following are true:

1. Local inference produced a poor or failed result (classified by severity — see Escalation Modes)
2. The configured `escalation_mode` permits escalation for that severity level
3. The policy engine allows the call (enabled, at least one viable provider, budget remaining, origin is localhost)

The orchestrator classifies local inference outcomes into four severity levels:

| Severity | Condition |
|----------|-----------|
| `hard_fail` | Primary server unreachable, timeout, or empty response |
| `failed` | Response returned but is an error bracket or structured failure |
| `degraded` | Response returned but is suspiciously short (< 20 characters) |
| `success` | Usable response produced |

Only when the local severity is at or below the threshold configured by `escalation_mode` will the orchestrator attempt EI fallback. If the EI call succeeds, the local failed response is replaced in both the final reply and the conversation history.

## Escalation Modes

Escalation modes control which local failure severities permit an external inference attempt.

| Mode | Local outcomes that trigger EI |
|------|--------------------------------|
| `disabled` | None — external inference is never used |
| `emergency_only` | `hard_fail` only (server unreachable, timeout, empty response) |
| `constrained` | `hard_fail` or `failed` (complete failures and structured errors) |
| `balanced` | `hard_fail`, `failed`, or `degraded` (includes very short responses) |
| `permissive` | Any local outcome — EI runs whenever budget and policy allow |

The recommended starting mode is `disabled`. Increase only when you have a specific use case and a budget you are comfortable with.

**Note:** even in `permissive` mode, all other policy checks still apply (origin, budget, API key, approval mode).

## Approval Modes

| Mode | Behaviour |
|------|-----------|
| `never` | All external inference attempts are denied, regardless of other settings. |
| `ask_for_paid_calls` | Each policy-passing call is queued as a pending approval. The operator must confirm or deny it via the Admin panel before the external call is made. |
| `always` | All policy-passing calls are executed immediately without manual confirmation. |

### ask_for_paid_calls in detail

When `approval_mode` is `ask_for_paid_calls` and a local failure triggers potential escalation:

1. The orchestrator pre-checks whether the call would pass all other policy gates (budget, origin, caps).
2. If the pre-check passes, the call is placed in the pending approvals queue with a unique `approval_id`.
3. The turn response returned to the user includes the `approval_id` and a notice that operator confirmation is needed.
4. The operator sees the pending entry in the Admin panel and can **Confirm** or **Deny** it.
5. On **Confirm**, the external call is made exactly once and the response is returned to the admin panel.
6. On **Deny**, the approval is discarded. No external call is made.
7. Pending approvals are held in memory only. They do not survive a server restart.

## Admin API Reference

### Status and configuration

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/external-inference/status` | Full config, budget state, and key status |
| `PATCH` | `/admin/external-inference/config` | Partially update external_inference config |
| `GET` | `/admin/external-inference/budget` | Budget summary for the current cycle |
| `GET` | `/admin/external-inference/providers` | Per-provider capabilities and key status |

### API keys

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/external-inference/api-key` | Set a provider API key (`{"provider": "...", "api_key": "..."}`) |
| `DELETE` | `/admin/external-inference/api-key?provider=<id>` | Remove a provider API key |
| `POST` | `/admin/external-inference/test?provider=<id>` | Test connectivity for a provider |

### Usage and approvals

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/external-inference/usage-history` | Recent call history from the ledger |
| `GET` | `/admin/external-inference/pending` | List pending approvals |
| `POST` | `/admin/external-inference/pending/{approval_id}/confirm` | Execute the held call |
| `POST` | `/admin/external-inference/pending/{approval_id}/deny` | Discard the held call |

## What Happens When a Provider Is Unavailable

If a provider is unreachable, rate-limiting, or returns an error:

- The router logs the failure and tries the next candidate (in modes that support fallback).
- In `default` mode (single provider), the attempt fails immediately.
- The system records the failure in the ledger.
- EOS falls back to local behaviour for that turn.
- No exception propagates to the main app.
- Subsequent turns are unaffected.

The main EOS process never hard-fails due to external inference failure.

## What Happens When the Budget Is Exhausted

When the estimated cost of a new request would exceed the remaining budget:

- The request is denied with reason `budget_exceeded`.
- A denial record is written to the ledger.
- EOS falls back to local models for that turn.
- No external call is made.
- The budget counter is not incremented for denied calls.

## Persistent Ledger

Every external inference attempt (allowed, denied, or failed) is recorded in the local SQLite database (`data/entity_state.db`, table `external_inference_ledger`). Each row includes:

- Timestamp and billing cycle
- Request origin (tier and IP)
- Provider and model ID used
- Estimated and actual cost
- Token counts (if available)
- Whether the call was denied, succeeded, or failed
- Denial reason (if denied)
- Response latency

The ledger is the authoritative source for spend calculations. It is never truncated automatically.

## Security Notes

- API keys are stored in the OS keyring, not in any config file or database.
- API keys are never returned by any API endpoint after they are saved.
- API keys are never logged.
- The `config.json` persistence path strips all provider API key fields before writing.
- All external responses are marked `is_external=True` in the result object — they are treated as untrusted external input, not as guaranteed local output.
- The localhost-only enforcement is backend-enforced, not UI-enforced.

## Testing

Run the external inference tests with:

```bash
pytest tests/unit/test_external_inference_policy.py -v
pytest tests/unit/test_external_inference_ledger.py -v
pytest tests/unit/test_ei_escalation_and_approval.py -v
pytest tests/unit/test_provider_registry_and_router.py -v
pytest tests/unit/test_provider_adapter_formats.py -v
pytest tests/integration/test_external_inference_integration.py -v
```

Tests do not make real HTTP calls. All provider adapters and the router are mocked at the HTTP layer or through direct unit testing of format helpers.

## Provider Layer Architecture

The multi-provider system is structured as follows:

```
ExternalInferencePolicy
  └── InferenceRouter          (runtime/providers/router.py)
        ├── ProviderRegistry   (runtime/providers/registry.py)
        │     ├── LocalAdapter       (adapters/local.py)
        │     ├── HuggingFaceAdapter (adapters/huggingface.py)
        │     ├── OpenAIAdapter      (adapters/openai.py)
        │     ├── AnthropicAdapter   (adapters/anthropic.py)
        │     ├── GeminiAdapter      (adapters/gemini.py)
        │     └── OpenRouterAdapter  (adapters/openrouter.py)
        └── CostPolicy         (runtime/providers/cost.py)
```

All adapters implement `BaseProvider` (runtime/providers/base.py) and return `ProviderResult`. The router selects adapters based on routing mode, capability requirements, budget, and key availability. The policy converts the final `ProviderResult` back to `HFInferenceResult` for backward compatibility with existing orchestrator code.

## Limitations

- Cost estimates are conservative approximations. Actual provider billing may differ.
- The system does not retrieve real-time per-model pricing from any provider API. The estimate is based on configurable flat rates.
- Pending approvals (`ask_for_paid_calls` mode) are held in memory only and are lost if the server restarts.
- The `local` provider adapter requires a running llama-server; it is not automatically started if down.

## What Is Not Allowed

- External inference triggered by Discord, LAN, or any remote origin
- External inference when `monthly_budget_usd` is 0
- External inference when `enabled` is false
- External inference when `escalation_mode` is `disabled`
- External inference when `approval_mode` is `never`
- External inference when estimated cost exceeds remaining budget
- External inference when estimated cost exceeds `per_request_cap_usd`
- External inference when today's request count meets or exceeds `daily_request_cap`
- Spending beyond the configured monthly cap
