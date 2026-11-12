"""Telegram message formatting (HTML parse mode). Telegram and web render from the same card JSON (§9.1)."""
from __future__ import annotations

from html import escape

DISCLAIMER = "⚠ Decision-support only. Not investment advice."
HORIZON_ORDER = {"short": 0, "medium": 1, "long": 2}
STATUS_NOTE = {"validated": "", "hitl_pending": " · needs review", "rejected": " · failed checks", "failed": " · failed"}


def inr(x) -> str:
    """Indian digit grouping: 1234567.5 -> 12,34,567.50"""
    if x is None:
        return "–"
    neg, x = x < 0, abs(float(x))
    whole, frac = f"{x:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    out = ",".join(groups + [tail]) if groups else tail
    return f"{'-' if neg else ''}{out}.{frac}"


def card_message(r: dict) -> str:
    card = r.get("card") or {}
    head = (f"<b>{escape(r['symbol'])}</b> · {r['horizon']} — <b>{escape(r['verdict'].title())}</b> "
            f"(conf {r['confidence']:.2f}){STATUS_NOTE.get(r['status'], '')}")
    if not card:
        why = "; ".join(v["message"] for v in r.get("violations", [])[:2]) or "no card produced"
        return f"{head}\n<i>{escape(why)}</i>"
    lv = card["levels"]
    lines = [head, escape(card["headline"]),
             f"Entry {inr(lv['entry_low'])}–{inr(lv['entry_high'])} · Stop {inr(lv['stop'])} · Target {inr(lv['target'])}"]
    if card.get("bull_case"):
        lines.append("▲ " + escape(card["bull_case"][0]))
    if card.get("bear_case"):
        lines.append("▼ " + escape(card["bear_case"][0]))
    inval = [i["condition"] for i in card.get("invalidation", [])][:2]
    if inval:
        lines.append("✕ Invalid if: " + escape("; ".join(inval)))
    return "\n".join(lines)


def analysis_message(query: str, out: dict) -> str:
    if out.get("clarification"):
        return escape(out["clarification"])
    results = sorted(out.get("results", []), key=lambda r: HORIZON_ORDER.get(r["horizon"], 9))
    if not results:
        why = "; ".join(out.get("errors") or []) or "data or gates unavailable"
        return f"No analysis could be produced for {escape(query)} ({escape(why)}).\n\n{DISCLAIMER}"
    gaps = sorted({g.split(":")[0] for r in results for g in r.get("gaps", [])})
    body = "\n\n".join(card_message(r) for r in results)
    note = f"\n\n<i>Data gaps: {escape(', '.join(gaps))}</i>" if gaps else ""
    return f"{body}{note}\n\nRun #{out['run_id']}\n{DISCLAIMER}"


HELP = ("<b>Commands</b>\n/analyze &lt;symbol or name&gt; [short|medium|long] — per-horizon verdict with levels\n"
        "/discover [short|medium|long] — what to look at now\n/events — active market events\n"
        "/picks [horizon] — open picks\n/inbox — review tasks\n/portfolio — simulated portfolios\n"
        "/whatif &lt;scenario&gt; — e.g. crude hits $110; estimated sector and portfolio impact\n"
        "/compare &lt;2-5 symbols&gt; — side by side\n"
        "/pause, /resume — kill switch\n/help — this message\n\n" + DISCLAIMER)
