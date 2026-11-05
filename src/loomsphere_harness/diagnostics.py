"""Run diagnostics: failure-safe usage accounting and budget warnings.

Pure functions over the counters a run already records in its evidence, so the
executor and the CLI cannot disagree about what a number means. Nothing here
reads the file system, the model, or the environment, and nothing here stores
tool arguments, tool results, file contents, prompts, or conversation messages.

Two rules shape the output:

* A count that was measured is reported as a number, including a real zero. A
  value that could not be measured is ``None``, never ``0``, so an unavailable
  provider figure is never read as "this run used nothing".
* Budget warnings are computed after the run from recorded usage. They are
  observations, not enforcement: they are labelled as such everywhere they
  appear and are never presented as a cap on spending.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Fraction of a configured limit at which a run is reported as near its budget.
DIAGNOSTIC_THRESHOLD = 0.8

#: Where the recorded counters come from, so no reader has to guess whether a
#: figure was measured by the runtime or inferred afterwards.
USAGE_SOURCE = "pydantic-ai RunUsage accumulator"

MEASURED = "measured"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"

#: Usage field -> the run limit that bounds it. The two names differ, so the
#: mapping is explicit: a lookup that used the usage name against the limit table
#: silently produced no entries at all.
LIMIT_FIELDS: dict[str, str] = {
    "requests": "request_limit",
    "tool_calls": "tool_call_limit",
    "total_tokens": "total_token_limit",
}

#: Stated once, stored with the evidence, so a reader of a single run record
#: knows what the warnings are and are not.
DIAGNOSTIC_NOTE = (
    "Observed diagnostic warnings computed after the run from recorded usage. They "
    "are not enforced interrupts and are not a hard cap on spending: limits are "
    "checked between steps, so a run can finish slightly above one."
)

COST_UNAVAILABLE: Mapping[str, str] = {
    "status": UNAVAILABLE,
    "reason": (
        "No verified billing units or prices are configured for this endpoint, so no "
        "cost estimate is reported. Recorded token counts are the auditable figures."
    ),
}


def _count(value: Any) -> int | None:
    """A non-negative integer counter, or ``None`` when the value is not a count."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def usage_snapshot(usage: Any) -> dict[str, Any]:
    """Bounded, non-sensitive usage for one run.

    Safe to call after an exception: it only reads the accumulator, never the
    messages. ``requests`` counts the model responses the run acted on and
    ``tool_calls`` the tool calls that actually executed, so both are known even
    when the provider reported no token usage at all.
    """

    requests = _count(getattr(usage, "requests", None))
    tool_calls = _count(getattr(usage, "tool_calls", None))
    unmeasured = _count(getattr(usage, "unmeasured_requests", None)) or 0
    input_tokens = _count(getattr(usage, "input_tokens", None))
    output_tokens = _count(getattr(usage, "output_tokens", None))
    total_tokens = _count(getattr(usage, "total_tokens", None))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    if requests is None:
        availability = UNAVAILABLE
    elif unmeasured <= 0:
        availability = MEASURED
    elif unmeasured < requests:
        availability = PARTIAL
    else:
        availability = UNAVAILABLE
    if availability == UNAVAILABLE:
        # The provider reported no usable usage. "Unknown" must not be stored as
        # zero, or a failed run looks like a free one.
        input_tokens = output_tokens = total_tokens = None

    return {
        "requests": requests,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "availability": availability,
        "unmeasured_requests": unmeasured,
        "source": USAGE_SOURCE,
    }


def budget_report(
    usage: Any,
    limits: Any,
    *,
    threshold: float = DIAGNOSTIC_THRESHOLD,
) -> dict[str, Any]:
    """Observed consumption per limit, with near/exceeded warnings.

    Reported from what was actually recorded; an unmeasured counter produces no
    entry rather than a misleading percentage.
    """

    entries: list[dict[str, Any]] = []
    for name, limit_name in LIMIT_FIELDS.items():
        used = usage.get(name) if isinstance(usage, Mapping) else None
        allowed = limits.get(limit_name) if isinstance(limits, Mapping) else None
        if _count(used) is None or _count(allowed) in (None, 0):
            continue
        ratio = round(used / allowed, 4)
        if used >= allowed:
            state = "exceeded"
        elif ratio >= threshold:
            state = "near"
        else:
            state = "within"
        entries.append(
            {
                "name": name,
                "used": used,
                "allowed": allowed,
                "ratio": ratio,
                "state": state,
                "message": (
                    f"{name} used {used} of {allowed} ({ratio * 100:.1f}% of the "
                    f"configured limit)"
                ),
            }
        )
    return {
        "threshold": threshold,
        "entries": entries,
        "warnings": [entry for entry in entries if entry["state"] != "within"],
        "note": DIAGNOSTIC_NOTE,
        "cost": dict(COST_UNAVAILABLE),
    }


__all__ = [
    "COST_UNAVAILABLE",
    "DIAGNOSTIC_NOTE",
    "DIAGNOSTIC_THRESHOLD",
    "LIMIT_FIELDS",
    "MEASURED",
    "PARTIAL",
    "UNAVAILABLE",
    "USAGE_SOURCE",
    "budget_report",
    "usage_snapshot",
]
