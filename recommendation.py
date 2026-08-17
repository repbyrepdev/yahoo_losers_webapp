"""Rebound scoring for stocks that have sold off.

Design rules, and the reasoning behind each:

1. A factor contributes only when its underlying data is real. The previous
   model assigned a neutral 50 to missing inputs, which is itself a fabricated
   observation -- it asserts "we looked and it was average" when nothing was
   looked at, and it drags every score toward the middle.

2. Weights renormalise across whichever factors are available. Three real
   factors produce a score built from three real factors, not a score diluted
   by three imaginary ones.

3. Coverage is reported alongside the score, because a score from two inputs
   and a score from six are not the same claim.

4. Below MIN_FACTORS_FOR_SCORE, no score is produced at all. Refusing to answer
   is the correct output when the inputs are not there.

None of this predicts returns. It ranks how well a name matches conditions that
tend to accompany mean reversion, which is a narrower and more honest claim.
"""

import math
from dataclasses import dataclass, asdict
from typing import List, Optional

# Below this many real factors, no recommendation is issued.
MIN_FACTORS_FOR_SCORE = 3

# Nominal weights. These are renormalised over available factors at scoring time.
WEIGHTS = {
    "analyst_upside": 0.28,
    "technical_reversion": 0.24,
    "analyst_ratings": 0.16,
    "options_positioning": 0.14,
    "short_interest": 0.10,
    "volume_capitulation": 0.08,
}


@dataclass
class Factor:
    key: str
    label: str
    weight: float
    score: Optional[float]
    detail: str
    source: str

    @property
    def available(self) -> bool:
        return self.score is not None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _score_analyst_upside(target_mean, current_price, analyst_count) -> Factor:
    """Distance to analyst consensus, damped by how many analysts contributed.

    Upside is mapped on a concave curve: the difference between 0% and 25% is
    treated as more meaningful than the difference between 100% and 125%, since
    very large consensus gaps usually reflect a thesis break rather than a
    proportionally larger opportunity.
    """
    key, label, source = "analyst_upside", "Analyst consensus upside", "yfinance:targetMeanPrice"
    if not target_mean or not current_price:
        return Factor(key, label, WEIGHTS[key], None, "no analyst consensus published", source)

    upside = (target_mean - current_price) / current_price
    # 0% -> 40, 25% -> 62, 50% -> 74, 100% -> 88, capped at 92.
    raw = 40 + 52 * (1 - math.exp(-2.2 * max(upside, 0)))
    if upside < 0:
        raw = _clamp(40 + upside * 80, 5, 40)

    # Thin coverage pulls the score toward neutral rather than being trusted whole.
    confidence = min(1.0, (analyst_count or 0) / 10)
    score = 50 + (raw - 50) * (0.55 + 0.45 * confidence)
    return Factor(
        key, label, WEIGHTS[key], _clamp(score),
        f"{upside:+.1%} to ${target_mean:,.2f} consensus across {analyst_count} analysts",
        source,
    )


def _score_technical_reversion(tech: Optional[dict]) -> Factor:
    """Oversold conditions: RSI, position in the Bollinger band, gap below the 20-day MA."""
    key, label, source = "technical_reversion", "Technical mean reversion", "yfinance:history"
    if not tech:
        return Factor(key, label, WEIGHTS[key], None, "insufficient price history", source)

    parts, notes = [], []

    rsi = tech.get("rsi14")
    if rsi is not None:
        # RSI 30 is the conventional oversold threshold; 70 overbought.
        parts.append(_clamp(100 - (rsi - 20) * (100 / 50)))
        notes.append(f"RSI {rsi:.0f}")

    percent_b = tech.get("percent_b")
    if percent_b is not None:
        # At/below the lower band (%B <= 0) is maximally stretched.
        parts.append(_clamp(100 - percent_b * 100))
        notes.append(f"%B {percent_b:.2f}")

    gap = tech.get("pct_from_ma20")
    if gap is not None:
        # 15% or more below the 20-day mean is a wide gap to close.
        parts.append(_clamp(50 + (-gap) * 333))
        notes.append(f"{gap:+.1%} vs 20d MA")

    if not parts:
        return Factor(key, label, WEIGHTS[key], None, "indicators unavailable", source)

    return Factor(key, label, WEIGHTS[key], _clamp(sum(parts) / len(parts)), ", ".join(notes), source)


def _score_analyst_ratings(spread: Optional[dict]) -> Factor:
    """Net analyst posture, weighting strong calls double."""
    key, label, source = "analyst_ratings", "Analyst rating spread", "yfinance:recommendations"
    if not spread or not spread.get("total"):
        return Factor(key, label, WEIGHTS[key], None, "no ratings published", source)

    bulls = spread.get("strongBuy", 0) * 2 + spread.get("buy", 0)
    bears = spread.get("strongSell", 0) * 2 + spread.get("sell", 0)
    holds = spread.get("hold", 0)
    denom = bulls + bears + holds
    if denom == 0:
        return Factor(key, label, WEIGHTS[key], None, "no ratings published", source)

    score = _clamp(50 + ((bulls - bears) / denom) * 50)
    return Factor(
        key, label, WEIGHTS[key], score,
        f"{spread.get('strongBuy',0)} strong buy, {spread.get('buy',0)} buy, "
        f"{holds} hold, {spread.get('sell',0)} sell, {spread.get('strongSell',0)} strong sell",
        source,
    )


def _score_options_positioning(put_call_ratio: Optional[float]) -> Factor:
    """Options positioning, read contrarian at the extremes.

    A very high put/call ratio on an already-sold-off name is treated as
    capitulation rather than as continuing weakness -- the standard contrarian
    reading. Mid-range ratios carry little information and score near neutral.
    """
    key, label, source = "options_positioning", "Options positioning", "yfinance:option_chain"
    if put_call_ratio is None:
        return Factor(key, label, WEIGHTS[key], None, "no options chain listed", source)

    if put_call_ratio < 0.7:
        score, note = _clamp(70 + (0.7 - put_call_ratio) * 40), "call-heavy positioning"
    elif put_call_ratio > 2.0:
        score, note = _clamp(60 + (put_call_ratio - 2.0) * 10), "put extreme, read as capitulation"
    elif put_call_ratio > 1.3:
        score, note = _clamp(50 - (put_call_ratio - 1.3) * 30), "put-heavy positioning"
    else:
        score, note = 50.0, "balanced positioning"

    return Factor(key, label, WEIGHTS[key], score, f"put/call {put_call_ratio:.2f}, {note}", source)


def _score_short_interest(short_pct_float: Optional[float]) -> Factor:
    """Elevated short interest raises the odds of a sharp bounce on good news."""
    key, label, source = "short_interest", "Short interest", "yfinance:shortPercentOfFloat"
    if short_pct_float is None:
        return Factor(key, label, WEIGHTS[key], None, "short interest not reported", source)

    # ~5% of float is unremarkable; 20%+ is heavily shorted.
    score = _clamp(45 + short_pct_float * 250)
    return Factor(key, label, WEIGHTS[key], score, f"{short_pct_float:.1%} of float short", source)


def _score_volume_capitulation(volume_ratio: Optional[float]) -> Factor:
    """Heavy volume on a down move often marks exhaustion rather than continuation."""
    key, label, source = "volume_capitulation", "Volume confirmation", "yfinance:history"
    if volume_ratio is None:
        return Factor(key, label, WEIGHTS[key], None, "volume history unavailable", source)

    score = _clamp(40 + (volume_ratio - 1.0) * 30)
    descriptor = "elevated" if volume_ratio > 1.5 else "normal" if volume_ratio > 0.7 else "light"
    return Factor(key, label, WEIGHTS[key], score, f"{volume_ratio:.2f}x 20-day average ({descriptor})", source)


def score_rebound(
    current_price: Optional[float],
    target_mean: Optional[float] = None,
    analyst_count: Optional[int] = None,
    ratings: Optional[dict] = None,
    technicals: Optional[dict] = None,
    put_call_ratio: Optional[float] = None,
    short_pct_float: Optional[float] = None,
) -> dict:
    """Produce a rebound score, or decline to when coverage is too thin."""
    volume_ratio = (technicals or {}).get("volume_ratio_20d")

    factors: List[Factor] = [
        _score_analyst_upside(target_mean, current_price, analyst_count),
        _score_technical_reversion(technicals),
        _score_analyst_ratings(ratings),
        _score_options_positioning(put_call_ratio),
        _score_short_interest(short_pct_float),
        _score_volume_capitulation(volume_ratio),
    ]

    available = [f for f in factors if f.available]
    coverage = len(available) / len(factors)

    if len(available) < MIN_FACTORS_FOR_SCORE:
        return {
            "scored": False,
            "reason": f"only {len(available)} of {len(factors)} inputs available "
                      f"(minimum {MIN_FACTORS_FOR_SCORE})",
            "recommendation": "Insufficient data",
            "recommendation_color": "#6c757d",
            "coverage": round(coverage, 2),
            "factors": [asdict(f) for f in factors],
            "methodology": METHODOLOGY,
        }

    # Renormalise weights across the factors that actually have data.
    total_weight = sum(f.weight for f in available)
    score = sum(f.score * (f.weight / total_weight) for f in available)

    # Confidence reflects how much of the model was actually observable.
    if coverage >= 0.83:
        confidence = "High"
    elif coverage >= 0.66:
        confidence = "Moderate"
    else:
        confidence = "Low"

    # Ratings are deliberately conservative: a strong call requires both a high
    # score and enough coverage to justify it.
    if score >= 70 and coverage >= 0.66:
        recommendation, color = "Strong rebound setup", "#28a745"
    elif score >= 58:
        recommendation, color = "Constructive", "#5cb85c"
    elif score >= 45:
        recommendation, color = "Neutral", "#ffc107"
    elif score >= 32:
        recommendation, color = "Weak setup", "#fd7e14"
    else:
        recommendation, color = "Avoid", "#dc3545"

    # Report the renormalised weight too. Showing only the nominal weight next
    # to a contribution computed from the renormalised one makes the arithmetic
    # look wrong to anyone checking it.
    contributions = [
        {
            **asdict(f),
            "effective_weight": round(f.weight / total_weight, 3),
            "contribution": round(f.score * (f.weight / total_weight), 2),
        }
        for f in available
    ]
    contributions.sort(key=lambda c: c["contribution"], reverse=True)

    return {
        "scored": True,
        "score": round(score, 1),
        "recommendation": recommendation,
        "recommendation_color": color,
        "confidence": confidence,
        "coverage": round(coverage, 2),
        "factors_used": len(available),
        "factors_total": len(factors),
        "factors": contributions,
        "missing": [{"key": f.key, "label": f.label, "reason": f.detail}
                    for f in factors if not f.available],
        "methodology": METHODOLOGY,
    }


METHODOLOGY = (
    "Weighted score over six factors: analyst consensus upside, technical mean "
    "reversion (Wilder RSI-14, Bollinger %B, gap to the 20-day mean), analyst "
    "rating spread, options positioning read contrarian at extremes, short "
    "interest as a proxy for squeeze potential, and volume as a capitulation "
    "signal. Only factors with real data contribute; weights are renormalised "
    "over those, and no score is issued below three available factors. "
    "This measures similarity to historical mean-reversion conditions. It is "
    "not a return forecast and not investment advice."
)
