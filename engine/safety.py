"""
Token safety gate — the piece the old bot was missing.

Momentum strategies on Solana memecoins die not from bad entries but from rugs:
mint/freeze authority still live, unlocked liquidity, or a single wallet holding
most of the supply. We run RugCheck (https://rugcheck.xyz) on the finalists only
(after cheap quant ranking) so request volume stays low.

The check is *fail-open with a penalty*: if RugCheck is unreachable we don't
hard-block, we just withhold the safety bonus — so the engine still runs when the
API is down, but strongly prefers verified-safe tokens when it's up.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger("engine.safety")

_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
_session = requests.Session()
_session.headers.update({"User-Agent": "moonshot-engine/2.0"})

# Risk-name fragments that are serious enough to hard-reject a buy.
_CRITICAL = (
    "mint authority",
    "freeze authority",
    "rugged",
)
# Risk-name fragments that dock points but don't auto-reject.
_HEAVY = (
    "unlocked",
    "single holder",
    "top 10 holders",
    "low liquidity",
    "low amount of lp",
)

_cache: dict[str, "SafetyReport"] = {}
_CACHE_TTL = 600.0  # seconds


@dataclass
class SafetyReport:
    mint: str
    ok: bool                       # passed the gate (safe enough to trade)
    score: float                   # 0-100, higher = safer
    rugged: bool
    checked: bool                  # did we actually reach RugCheck?
    flags: list = field(default_factory=list)
    reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "score": round(self.score, 0),
            "rugged": self.rugged,
            "checked": self.checked,
            "flags": self.flags[:6],
            "reason": self.reason,
        }


def _unknown(mint: str, reason: str) -> SafetyReport:
    # Fail-open: allowed through, but with a neutral-low safety score so verified
    # tokens are preferred when the API is healthy.
    return SafetyReport(mint=mint, ok=True, score=45.0, rugged=False,
                        checked=False, flags=[], reason=reason)


def check(mint: str, *, strict: bool = True, timeout: float = 6.0) -> SafetyReport:
    """
    Return a SafetyReport for a mint. Cached for _CACHE_TTL seconds.

    strict=True hard-rejects critical risks (live mint/freeze authority, rugged).
    """
    cached = _cache.get(mint)
    if cached and (time.time() - cached.ts) < _CACHE_TTL:
        return cached

    try:
        resp = _session.get(_RUGCHECK.format(mint=mint), timeout=timeout)
        if resp.status_code == 429:
            return _unknown(mint, "rugcheck rate-limited")
        if not resp.ok:
            return _unknown(mint, f"rugcheck {resp.status_code}")
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.debug("RugCheck failed for %s: %s", mint, e)
        return _unknown(mint, "rugcheck unreachable")

    rugged = bool(data.get("rugged"))
    raw_risks = data.get("risks") or []
    # RugCheck's `score_normalised` is 0-100 where higher = riskier.
    risk_norm = data.get("score_normalised")
    if risk_norm is None:
        # Fall back to raw score (also higher = riskier, unbounded-ish).
        risk_norm = min(100.0, float(data.get("score") or 0) / 10.0)
    safety_score = max(0.0, 100.0 - float(risk_norm))

    flags: list[str] = []
    critical_hit = False
    for r in raw_risks:
        name = str(r.get("name", "")).strip()
        level = str(r.get("level", "")).lower()
        if not name:
            continue
        flags.append(name)
        low = name.lower()
        if level == "danger" or any(c in low for c in _CRITICAL):
            critical_hit = True
        elif any(h in low for h in _HEAVY):
            safety_score -= 8

    safety_score = max(0.0, min(100.0, safety_score))

    ok = True
    reason = "ok"
    if rugged:
        ok, reason, critical_hit = False, "rugged", True
    elif strict and critical_hit:
        ok = False
        reason = "; ".join(flags[:3]) or "critical risk"
    elif safety_score < 25:
        ok = False
        reason = f"safety score {safety_score:.0f}"

    report = SafetyReport(
        mint=mint, ok=ok, score=safety_score, rugged=rugged,
        checked=True, flags=flags, reason=reason,
    )
    _cache[mint] = report
    return report
