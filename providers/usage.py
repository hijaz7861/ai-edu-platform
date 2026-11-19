"""Strict normalization of provider-reported token usage.

This module deliberately stores no raw provider response, prompt, credential,
request identifier, or price. Missing/partial/invalid usage must never be
silently estimated or treated as zero.
"""

MAX_TOKEN_COUNT = (1 << 63) - 1

_PROVIDER_FIELDS = {
    "gemini-native": ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"),
    "openai-compatible": ("prompt_tokens", "completion_tokens", "total_tokens"),
    "mistral-agent": ("prompt_tokens", "completion_tokens", "total_tokens"),
}


def _token_count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_TOKEN_COUNT:
        return None
    return value


def normalize_provider_usage(raw, *, provider, model=None):
    """Return a bounded, vendor-neutral usage record.

    status is one of reported, partial, unreported, or invalid. Only a fully
    consistent input/output/total triplet is reported. A total larger than
    input + output is retained because providers may include reasoning, tool,
    or connector tokens in their total. No missing count is estimated.
    """
    safe_model = model.strip()[:128] if isinstance(model, str) and model.strip() else None
    result = {
        "schema_version": 1,
        "provider": str(provider)[:64],
        "model": safe_model,
        "status": "unreported",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "unclassified_tokens": None,
    }

    fields = _PROVIDER_FIELDS.get(provider)
    if raw is None:
        return result
    if not isinstance(raw, dict):
        result["status"] = "invalid"
        return result
    if fields is None:
        result["status"] = "invalid"
        return result

    present = [field in raw and raw[field] is not None for field in fields]
    if not any(present):
        return result

    parsed = []
    for field, exists in zip(fields, present):
        value = _token_count(raw.get(field)) if exists else None
        if exists and value is None:
            result["status"] = "invalid"
            return result
        parsed.append(value)

    input_tokens, output_tokens, total_tokens = parsed
    result["input_tokens"] = input_tokens
    result["output_tokens"] = output_tokens
    result["total_tokens"] = total_tokens

    if input_tokens is not None and output_tokens is not None and total_tokens is not None:
        minimum_total = input_tokens + output_tokens
        if minimum_total > MAX_TOKEN_COUNT or total_tokens < minimum_total:
            result["status"] = "invalid"
            result["input_tokens"] = None
            result["output_tokens"] = None
            result["total_tokens"] = None
            return result
        result["unclassified_tokens"] = total_tokens - minimum_total
        result["status"] = "reported"
    else:
        result["status"] = "partial"

    return result
