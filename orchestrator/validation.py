"""Deterministic validation of recommendation cards (§7.1).

LLM output is never trusted. Each check has a defined action: repair (fix in
place), drop (remove the offending claim/sentence), downgrade, route to HITL,
or reject. V1 (schema) happens at parse time; V10 (duplicates) at publish time.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from .config import Config
from .evidence import EvidenceItem
from .schemas import RecommendationCard

BANNED = re.compile(
    r"\b(sure[- ]shot|can'?t lose|cannot lose|guarantee[sd]?|risk[- ]free|multibagger|"
    r"buy now|sell now|must buy|jackpot|to the moon|no[- ]brainer)\b", re.I)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUM = re.compile(r"(?<![\w.])[-+]?(?:₹\s?)?\d[\d,]*(?:\.\d+)?\s?(?:%|x\b|×)?")
# Conventional parameters that appear in prose ("200-DMA", "52-week", "RSI 14") rather than as data.
_CONVENTIONAL = {14.0, 20.0, 50.0, 52.0, 55.0, 100.0, 200.0}


@dataclass
class Violation:
    code: str
    message: str
    action: str  # repaired | dropped | downgraded | hitl | rejected


@dataclass
class ValidationReport:
    card: RecommendationCard
    violations: list[Violation] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        actions = {v.action for v in self.violations}
        if "rejected" in actions:
            return "reject"
        if "hitl" in actions:
            return "hitl"
        return "ok"

    def add(self, code: str, message: str, action: str) -> None:
        self.violations.append(Violation(code, message, action))

    def summary(self) -> list[dict]:
        return [v.__dict__ for v in self.violations]


def _numbers(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out += _numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += _numbers(v)
    elif isinstance(obj, str):
        try:
            out.append(float(obj.replace(",", "")))
        except ValueError:
            pass
    return out


@dataclass
class _Num:
    value: float
    pct: bool
    raw: str


def extract_numbers(text: str) -> list[_Num]:
    text = _ISO_DATE.sub(" ", text)
    found = []
    for m in _NUM.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"[^\d.\-+]", "", raw.replace(",", ""))
        try:
            val = float(digits)
        except ValueError:
            continue
        pct = raw.endswith("%")
        if not pct and (val in _CONVENTIONAL or (val.is_integer() and abs(val) <= 12)):
            continue
        found.append(_Num(val, pct, raw))
    return found


def _matches(n: _Num, candidates: Iterable[float], cfg: Config) -> bool:
    tol = cfg.numeric_tolerance
    for c in candidates:
        if n.pct:
            # Evidence may hold percentages as 12.3 or as fractions 0.123.
            if abs(n.value - c) <= tol["pct_pp"] or abs(n.value - c * 100) <= tol["pct_pp"]:
                return True
        else:
            rel = tol["price"] if abs(c) >= 100 else tol["ratio"]
            if c == 0:
                if n.value == 0:
                    return True
            elif abs(n.value - c) / abs(c) <= rel:
                return True
            # Prose rounds: allow a match on the value rounded to the precision written.
            decimals = len(n.raw.split(".")[1].rstrip("%x× ")) if "." in n.raw else 0
            if round(c, decimals) == n.value:
                return True
    return False


def validate_card(card: RecommendationCard, *, quant: dict, levels: dict, scored: dict, critique: Optional[dict],
                  views: dict[str, Optional[dict]], evidence: dict[str, EvidenceItem], decision_time: datetime,
                  cfg: Config) -> ValidationReport:
    card = card.model_copy(deep=True)
    rep = ValidationReport(card)

    # Values the quant engine registered as derived calcs (§7.1 V3) - the card may quote these freely.
    derived = _numbers(levels) + _numbers({k: scored[k] for k in ("quant_score", "final_score", "max_confidence")}) + \
        _numbers({k: v for k, v in quant.items() if k != "gates"})

    # V2 evidence resolution --------------------------------------------------
    kept = []
    for claim in card.claims:
        bad = [e for e in claim.evidence_ids if e not in evidence or evidence[e].as_of > decision_time]
        if not claim.evidence_ids or bad:
            rep.add("V2", f"claim dropped, unresolved evidence {bad or '[]'}: {claim.text[:80]}", "dropped")
            continue
        kept.append(claim)
    card.claims = kept
    for risk in card.key_risks:
        risk.evidence_ids = [e for e in risk.evidence_ids if e in evidence]
    if not card.claims:
        rep.add("V2", "no supported claims remain; thesis unsupported", "rejected")

    # V3 numeric grounding ----------------------------------------------------
    grounded_numbers: list[float] = list(derived)
    kept = []
    for claim in card.claims:
        pool = derived + [x for e in claim.evidence_ids for x in _numbers(evidence[e].data)]
        blob = " ".join(json.dumps(evidence[e].data, default=str) for e in claim.evidence_ids)
        bad_nums = [n.raw for n in extract_numbers(claim.text) if not _matches(n, pool, cfg)]
        bad_dates = [d for d in _ISO_DATE.findall(claim.text) if d not in blob]
        if bad_nums or bad_dates:
            rep.add("V3", f"ungrounded {bad_nums + bad_dates} in claim: {claim.text[:80]}", "dropped")
            continue
        grounded_numbers += [n.value for n in extract_numbers(claim.text)]
        kept.append(claim)
    if card.claims and not kept:
        rep.add("V3", "every claim was ungrounded; thesis unsupported", "rejected")
    card.claims = kept

    def ungrounded(text: str) -> list[str]:
        return [n.raw for n in extract_numbers(text) if not _matches(n, grounded_numbers, cfg)]

    if ungrounded(card.thesis) or ungrounded(card.headline):
        rep.add("V3", f"thesis/headline has ungrounded numbers {ungrounded(card.thesis + ' ' + card.headline)}", "rejected")
    for field_name in ("bull_case", "bear_case", "what_to_watch"):
        items = getattr(card, field_name)
        clean = [s for s in items if not ungrounded(s)]
        if len(clean) != len(items):
            rep.add("V3", f"{len(items) - len(clean)} {field_name} item(s) with ungrounded numbers removed", "dropped")
            setattr(card, field_name, clean)

    # V4 level consistency (levels are not editable by the LLM) ---------------
    lv = card.levels
    for k in ("entry_low", "entry_high", "stop", "target"):
        if abs(getattr(lv, k) - float(levels[k])) > 1e-6:
            rep.add("V4", f"{k} {getattr(lv, k)} differs from quant {levels[k]}", "rejected")
    if not (levels["stop"] < levels["entry_low"] <= levels["entry_high"] < levels["target"]):
        rep.add("V4", "quant levels not ordered stop < entry_low <= entry_high < target", "rejected")
    rr = float(levels.get("rr") or 0)
    if card.verdict != "avoid" and rr < cfg.rr_floor[card.horizon]:
        rep.add("V4", f"reward:risk {rr} below floor {cfg.rr_floor[card.horizon]}", "rejected")
    atr = quant.get("atr")
    if atr and card.horizon == "short":
        dist = levels["entry_low"] - levels["stop"]
        if not (0.5 * atr <= dist <= 4 * atr):
            rep.add("V4", f"stop distance {dist:.2f} outside 0.5-4 ATR ({atr})", "rejected")

    # V5 gate re-check --------------------------------------------------------
    if not scored["gates_passed"] and card.verdict != "avoid":
        rep.add("V5", "hard gate failed but verdict is not avoid", "rejected")

    # V6 freshness ------------------------------------------------------------
    stale = []
    for claim in card.claims:
        for e in claim.evidence_ids:
            item = evidence[e]
            max_days = cfg.max_evidence_age_days.get(item.kind)
            if max_days is not None and (decision_time - item.as_of).days > max_days:
                stale.append(e)
    if stale:
        card.confidence = max(0.0, card.confidence - 0.1)
        rep.add("V6", f"stale evidence {sorted(set(stale))}; confidence lowered", "downgraded")

    # V8 verdict <-> score <-> critic ----------------------------------------
    if card.verdict != scored["verdict"]:
        rep.add("V8", f"card verdict {card.verdict} != computed {scored['verdict']}", "rejected")
    if (card.verdict == "avoid") != (card.action == "avoid"):
        rep.add("V8", f"action {card.action} inconsistent with verdict {card.verdict}", "rejected")
    if card.confidence > scored["max_confidence"] + 1e-9:
        rep.add("V8", f"confidence {card.confidence} capped to {scored['max_confidence']}", "repaired")
        card.confidence = scored["max_confidence"]
    if critique:
        if critique.get("verdict") == "reject":
            rep.add("V8", "critic rejected", "rejected")
        elif critique.get("verdict") == "needs_human":
            rep.add("V8", "critic asked for human review", "hitl")
        high = [c["argument"] for c in critique.get("strongest_counterarguments", []) if c.get("severity") == "high"]
        if high and not card.bear_case and not card.key_risks:
            rep.add("V8", "high-severity critic arguments missing from bear case", "rejected")

    # V9 language policy ------------------------------------------------------
    text = " ".join([card.headline, card.thesis, *card.bull_case, *card.bear_case, *card.what_to_watch])
    hits = sorted({m.group(0).lower() for m in BANNED.finditer(text)})
    if hits:
        rep.add("V9", f"banned phrasing {hits}", "rejected")

    # V11 cross-agent contradiction -------------------------------------------
    tech = views.get("technical") or {}
    if tech.get("trend") == "up" and quant.get("above_200dma") is False:
        if not re.search(r"200[- ]?DMA|200[- ]day", text, re.I):
            rep.add("V11", "technical says uptrend but price is below the 200-DMA; card does not address it", "hitl")
    fund = views.get("fundamental") or {}
    if card.verdict == "favourable" and fund.get("earnings_trajectory") == "deteriorating" and card.horizon == "long":
        rep.add("V11", "favourable long-term verdict with deteriorating earnings", "hitl")

    # V12 security ------------------------------------------------------------
    flagged = [n for n, v in views.items() if v and v.get("security_flag")]
    if flagged:
        rep.add("V12", f"security flag raised by {flagged}; contribution stripped", "repaired")

    return rep
