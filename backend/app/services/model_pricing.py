"""Provider-price metadata and best-effort real-cost calculation."""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

DEFAULT_CREDIT_VALUE_CNY = Decimal("0.10")
USD_TO_CNY = Decimal("7.25")


def _usage_number(usage: dict, *keys: str) -> Decimal:
    for key in keys:
        value = usage.get(key)
        if value not in (None, ""):
            return Decimal(str(value))
    return Decimal("0")


def _cached_input_tokens(usage: dict) -> Decimal:
    cached = _usage_number(usage, "cached_tokens", "cached_input_tokens")
    if cached:
        return cached
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            value = details.get("cached_tokens") or details.get("cached_input_tokens")
            if value not in (None, ""):
                return Decimal(str(value))
    return Decimal("0")


def estimate_credits_from_usage(extra: dict | None, usage: dict | None) -> int | None:
    pricing = ((extra or {}).get("official_pricing") or {})
    if not pricing or not usage:
        return None
    try:
        credit_value = Decimal(str(pricing.get("credit_value_cny") or DEFAULT_CREDIT_VALUE_CNY))
        currency = str(pricing.get("currency") or "CNY").upper()
        fx = Decimal(str(pricing.get("usd_to_cny") or USD_TO_CNY))
        multiplier = Decimal("1")
        if currency == "USD":
            multiplier = fx
        unit = str(pricing.get("unit") or "per_1m_tokens")
        if unit != "per_1m_tokens":
            return None
        input_tokens = _usage_number(usage, "prompt_tokens", "input_tokens")
        cached_tokens = _cached_input_tokens(usage)
        output_tokens = _usage_number(usage, "completion_tokens", "output_tokens")
        total_tokens = _usage_number(usage, "total_tokens")
        input_rate = Decimal(str(pricing.get("input_per_1m") or 0))
        cached_rate = Decimal(str(pricing.get("cached_input_per_1m") or 0))
        output_rate = Decimal(str(pricing.get("output_per_1m") or 0))
        total_rate = Decimal(str(pricing.get("total_per_1m") or 0))
        amount = Decimal("0")
        if total_rate and total_tokens:
            amount += total_tokens * total_rate / Decimal(1_000_000)
        else:
            non_cached_input = max(Decimal("0"), input_tokens - cached_tokens)
            amount += non_cached_input * input_rate / Decimal(1_000_000)
            amount += cached_tokens * cached_rate / Decimal(1_000_000)
            amount += output_tokens * output_rate / Decimal(1_000_000)
        credits = (amount * multiplier / credit_value).quantize(Decimal("1"), rounding=ROUND_CEILING)
        return max(1 if amount > 0 else 0, int(credits))
    except Exception:
        return None


def usage_from_response(data: dict | None) -> dict | None:
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if isinstance(usage, dict):
        return usage
    for key in ("token_usage", "tokens", "billing"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    raw = data.get("raw")
    if isinstance(raw, dict):
        return usage_from_response(raw)
    return None
