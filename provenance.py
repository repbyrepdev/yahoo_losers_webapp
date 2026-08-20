"""Data provenance primitives.

Every externally-sourced value carries where it came from, when it was fetched,
and whether the fetch actually succeeded. The rule this module exists to enforce:

    A failed fetch must never be substituted with an invented value.

Before this, a dead upstream API silently became `current_price * 1.15` and
rendered as an analyst price target. A caller that cannot get real data now
returns `Sourced.unavailable(...)`, which the UI renders as an em dash.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# Credential query parameters that providers put in URLs. HTTPError text
# embeds the full request URL, so any exception string that might be
# stored, logged, or rendered must pass through redact_secrets first.
_SECRET_PARAM_RE = re.compile(r"""(?i)\b(apikey|token)=[^&\s"'<>]+""")


def redact_secrets(text) -> str:
    """Strip credential values from provider-originated text."""
    return _SECRET_PARAM_RE.sub(r"\1=REDACTED", str(text))

# What the UI shows when we have no real value. Never a number.
UNAVAILABLE_DISPLAY = "—"


@dataclass(frozen=True)
class Sourced:
    """A value plus the provenance needed to decide whether to trust it."""

    value: Any
    source: str
    as_of: datetime
    ok: bool = True
    reason: Optional[str] = None

    @classmethod
    def live(cls, value: Any, source: str) -> "Sourced":
        """A value fetched directly from an upstream provider."""
        return cls(value, source, datetime.now(timezone.utc), True, None)

    @classmethod
    def derived(cls, value: Any, source: str) -> "Sourced":
        """A value computed from other data rather than fetched.

        Still honest, but the UI labels it so it is never mistaken for a
        reported figure.
        """
        return cls(value, f"derived:{source}", datetime.now(timezone.utc), True, None)

    @classmethod
    def unavailable(cls, source: str, reason: str) -> "Sourced":
        """No real value could be obtained. Callers must not invent one."""
        return cls(None, source, datetime.now(timezone.utc), False, reason)

    @property
    def is_derived(self) -> bool:
        return self.source.startswith("derived:")

    def format(self, spec: str = "", prefix: str = "", suffix: str = "") -> str:
        """Render for display. An unavailable value can never render as a number."""
        if not self.ok or self.value is None:
            return UNAVAILABLE_DISPLAY
        body = format(self.value, spec) if spec else str(self.value)
        return f"{prefix}{body}{suffix}"


def safe_ratio(numerator: float, denominator: float, default: Optional[float] = None) -> Optional[float]:
    """Divide without raising on a zero or missing denominator.

    Yahoo's chart endpoint returns 0-volume bars outside regular hours, which
    previously reached an unguarded division and crashed the request.
    Returns `default` (None unless specified) rather than a fabricated number.
    """
    if not denominator:
        return default
    try:
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return default
