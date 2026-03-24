"""
EOS — Provider Cost Policy
============================
Conservative cost estimation for multi-provider routing and budget enforcement.

Design rules
------------
* Cost values are policy metadata, not live pricing data.
  Never hardcode exact vendor prices — they change without notice.
* All estimates are intentionally conservative (high) to prevent
  under-counting spend against the monthly budget.
* Operators may override per-provider rates in config.
* Local providers always cost $0.00.

Default conservative rates (USD per 1K tokens, input + output combined)
------------------------------------------------------------------------
  huggingface  $0.005  — HF Serverless worst-case upper bound
  openai       $0.010  — GPT-4o class (conservative; use gpt-4o-mini for less)
  anthropic    $0.015  — Claude 3 Haiku class (conservative)
  gemini       $0.003  — Gemini Flash class (conservative)
  openrouter   $0.002  — open-weight models via OR (conservative)
  local        $0.000  — free (llama-server)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# ── Default conservative rates ────────────────────────────────────────────────

# These cover BOTH input and output at the same rate (conservative).
# Actual pricing typically charges less for input tokens.
_DEFAULT_COMBINED_RATE: Dict[str, float] = {
    "huggingface": 0.005,
    "openai":      0.010,
    "anthropic":   0.015,
    "gemini":      0.003,
    "openrouter":  0.002,
    "local":       0.000,
}

# Fallback token assumptions for pre-call estimates
DEFAULT_ASSUMED_INPUT_TOKENS:  int = 512
DEFAULT_ASSUMED_OUTPUT_TOKENS: int = 256


@dataclass
class CostPolicy:
    """
    Per-provider cost estimation policy.

    cost_per_1k_input  — USD per 1K prompt tokens
    cost_per_1k_output — USD per 1K completion tokens

    Set both values to the same conservative rate if you only have a
    blended rate figure.
    """
    provider_id:        str
    cost_per_1k_input:  float = 0.005
    cost_per_1k_output: float = 0.005

    @classmethod
    def default_for(cls, provider_id: str) -> "CostPolicy":
        """Return a default CostPolicy for the given provider."""
        rate = _DEFAULT_COMBINED_RATE.get(provider_id, 0.005)
        return cls(provider_id=provider_id,
                   cost_per_1k_input=rate,
                   cost_per_1k_output=rate)


def estimate_cost(
    *,
    provider_id:      str,
    tokens_input:     Optional[int] = None,
    tokens_output:    Optional[int] = None,
    cost_overrides:   Optional[Dict[str, CostPolicy]] = None,
) -> float:
    """
    Conservative pre-call cost estimate in USD.

    Falls back to DEFAULT_ASSUMED_INPUT_TOKENS / OUTPUT_TOKENS when
    exact counts are not yet known.

    Parameters
    ----------
    provider_id     — which provider is being estimated
    tokens_input    — prompt token count (None = use default assumption)
    tokens_output   — completion token count (None = use default assumption)
    cost_overrides  — optional per-provider policy overrides from config

    Returns
    -------
    float — estimated cost in USD, rounded to 6 decimal places
    """
    if cost_overrides and provider_id in cost_overrides:
        policy = cost_overrides[provider_id]
    else:
        policy = CostPolicy.default_for(provider_id)

    inp = tokens_input  if tokens_input  is not None else DEFAULT_ASSUMED_INPUT_TOKENS
    out = tokens_output if tokens_output is not None else DEFAULT_ASSUMED_OUTPUT_TOKENS

    return round(
        (inp / 1000.0) * policy.cost_per_1k_input +
        (out / 1000.0) * policy.cost_per_1k_output,
        6,
    )


def build_cost_overrides(ei_cfg: dict) -> Dict[str, CostPolicy]:
    """
    Build a per-provider CostPolicy map from the external_inference config.

    Looks for config keys of the form:
      external_inference.<provider_id>.cost_per_1k_input
      external_inference.<provider_id>.cost_per_1k_output

    Returns an empty dict if no overrides are configured.
    """
    overrides: Dict[str, CostPolicy] = {}
    known_providers = list(_DEFAULT_COMBINED_RATE.keys())
    for pid in known_providers:
        pcfg = ei_cfg.get(pid, {})
        if not isinstance(pcfg, dict):
            continue
        rate_in  = pcfg.get("cost_per_1k_input")
        rate_out = pcfg.get("cost_per_1k_output")
        if rate_in is not None or rate_out is not None:
            default  = CostPolicy.default_for(pid)
            overrides[pid] = CostPolicy(
                provider_id        = pid,
                cost_per_1k_input  = float(rate_in)  if rate_in  is not None else default.cost_per_1k_input,
                cost_per_1k_output = float(rate_out) if rate_out is not None else default.cost_per_1k_output,
            )
    return overrides
