# External Inference — Hugging Face (Optional Feature)

This document describes the optional Budget-Governed External Inference feature in EOS. It covers what the feature does, how to configure it, and what happens under every operating condition.

## What This Feature Does

EOS runs entirely locally by default. No external API calls are made, no API key is required, and no money is ever spent without explicit user configuration.

The External Inference feature allows you to optionally allow EOS to call the Hugging Face Inference API when local inference produces a poor or failed result. All external calls are:

- Governed by a user-set monthly spend budget
- Limited to localhost-origin requests only
- Triggered only when local inference fails to produce a usable response
- Tracked in a persistent local ledger
- Fully optional — the system operates identically without them

## Provider Support

Only Hugging Face is supported. There is no other provider in this implementation.

## Default State

External inference is **disabled by default**. None of the following will ever happen without your explicit configuration:

- No HuggingFace API calls are made
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

The external inference settings live in `config.json` under `external_inference`. You can also edit all settings through the Admin panel under the **Ext. Inference** tab.

Key fields:

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Master gate. Must be `true` for any external calls to occur. |
| `monthly_budget_usd` | `0.0` | Monthly spend cap in USD. `0` = no spend permitted. Enter any numeric value. |
| `monthly_budget_override_usd` | `null` | Override for the current billing cycle only. |
| `per_request_cap_usd` | `null` | Maximum cost for a single request. `null` = no per-request cap. |
| `daily_request_cap` | `null` | Maximum non-denied requests per calendar day. `null` = unlimited (budget still applies). |
| `approval_mode` | `ask_for_paid_calls` | `never` / `ask_for_paid_calls` / `always` |
| `escalation_mode` | `disabled` | `disabled` / `emergency_only` / `constrained` / `balanced` / `permissive` |
| `current_billing_cycle_start` | `null` | YYYY-MM-DD. `null` = auto-set to the 1st of the current month. |
| `huggingface.model_id` | `mistralai/Mistral-7B-Instruct-v0.2` | HuggingFace model repo ID. |
| `huggingface.timeout_sec` | `30.0` | Per-request timeout in seconds. |

## Setting the API Key

The HuggingFace API key is stored in the OS system keyring (Windows Credential Manager on Windows, macOS Keychain on macOS, Secret Service on Linux). It is **never** stored in `config.json` or any plaintext file.

To set the key via the Admin panel:
1. Open the Admin panel → **Ext. Inference** tab
2. Paste your HuggingFace token (starts with `hf_`) in the API Key field
3. Click **Save Key**

The key is stored immediately and cannot be retrieved through the UI after save. The panel shows only whether a key is configured (`✓` or `✗`).

To set via environment variable (alternative):
```
EOS_HUGGINGFACE_API_KEY=hf_yourtoken
```

The `EOS_HUGGINGFACE_API_KEY` environment variable is read at startup and takes precedence over the keyring value when both are present.

## Budget Controls

All budget enforcement happens **locally** before any external call is made. The system never relies on HuggingFace-side billing limits.

**How the budget is enforced:**
1. Before each potential external call, the policy engine estimates the cost conservatively (uses ~$0.005 per 1K tokens by default).
2. It queries the local ledger for total spend in the current billing cycle.
3. If `estimated_cost + spent >= budget`, the request is denied with `budget_exceeded`.
4. The estimate is intentionally high to avoid under-counting.

**When monthly_budget_usd is 0.0:**
All external calls are denied with reason `zero_budget`. This is the default state.

**Billing cycle:**
By default the cycle resets on the 1st of each month. You can set `current_billing_cycle_start` to any YYYY-MM-DD date to align the cycle with your HuggingFace invoice.

## Runtime Invocation

External inference is invoked automatically by the orchestrator when **all** of the following are true:

1. Local inference produced a poor or failed result (classified by severity — see Escalation Modes)
2. The configured `escalation_mode` permits escalation for that severity level
3. The policy engine allows the call (enabled, key present, budget remaining, origin is localhost)

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
| `ask_for_paid_calls` | Each policy-passing call is queued as a pending approval. The operator must confirm or deny it via the Admin panel (Ext. Inference → Pending Approvals) before the external call is made. The turn response tells the user the call is pending approval. |
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

## Pending Approvals API

The following admin endpoints manage the pending approval queue:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/external-inference/pending` | List all pending approvals |
| `POST` | `/admin/external-inference/pending/{approval_id}/confirm` | Execute the held call and return response |
| `POST` | `/admin/external-inference/pending/{approval_id}/deny` | Discard the held call |

These endpoints are only accessible to authenticated admin sessions.

## What Happens When the Provider Is Unavailable

If HuggingFace is unreachable, rate-limiting, or returns an error:

- The system records the failure in the ledger.
- EOS falls back to local behaviour for that turn.
- No retry storm occurs (`max_retries` defaults to 1).
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
- Model ID used
- Estimated and actual cost
- Token counts (if available)
- Whether the call was denied, succeeded, or failed
- Denial reason (if denied)
- Response latency

The ledger is the authoritative source for spend calculations. It is never truncated automatically.

The `external_inference_ledger` table is created by the ledger module on first use. For existing installs upgrading from a version before this feature, the table is added automatically by migration `m005_add_external_inference_ledger` on next startup.

## Admin Panel

The **Ext. Inference** tab in the Admin panel provides:

- Master enable/disable toggle
- Budget summary with visual progress bar and warning thresholds
- Editable monthly budget (free-form numeric input, any value)
- Current-cycle override field
- Per-request and daily caps
- Escalation and approval mode selectors
- Model ID and timeout settings
- API key management (save, delete, connection test)
- Recent usage history table (last 50 attempts)
- Pending Approvals section (visible when `approval_mode` is `ask_for_paid_calls`)

## Security Notes

- The API key is stored in the OS keyring, not in any config file or database.
- The API key is never returned by any API endpoint after it is saved.
- The API key is never logged.
- The `config.json` persistence path strips the API key before writing.
- All external responses are marked `is_external=True` in the result object — they are treated as untrusted external input, not as guaranteed local output.

## Testing

Run the external inference tests with:

```bash
pytest tests/unit/test_external_inference_policy.py -v
pytest tests/unit/test_external_inference_ledger.py -v
pytest tests/unit/test_ei_escalation_and_approval.py -v
pytest tests/integration/test_external_inference_integration.py -v
```

Tests do not make real HTTP calls. The HuggingFace provider is mocked at the HTTP layer.

## Limitations

- Cost estimates are conservative approximations. Actual HuggingFace billing may differ.
- The system does not retrieve real-time per-model pricing from the HuggingFace API. The estimate is based on a configurable flat rate.
- Pending approvals (`ask_for_paid_calls` mode) are held in memory only and are lost if the server restarts. The operator must re-trigger the original conversation turn after a restart.
- Only the HuggingFace Inference API is supported. No other provider will be added without an explicit architecture decision.

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
