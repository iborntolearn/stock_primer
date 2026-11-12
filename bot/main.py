"""Owner-only Telegram bot (feature E1).

    python -m bot.main          # needs STOCK_PRIMER_TELEGRAM_BOT_TOKEN and _OWNER_CHAT_ID in .env

Commands: /analyze, /discover, /events, /picks, /inbox, /portfolio, /whatif, /compare, /pause, /resume, /help.
Review-task buttons (approve/reject/...) call the HITL service. Messages from any
other chat are ignored.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from sqlalchemy import select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from core import models as m
from core.settings import get_settings

from .format import DISCLAIMER, HELP, analysis_message, inr
from .notifier import task_keyboard

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
HORIZONS = ("short", "medium", "long")


def parse_analyze_args(args: list[str]) -> tuple[str, list[str]]:
    horizons = [a.lower() for a in args if a.lower() in HORIZONS]
    query = " ".join(a for a in args if a.lower() not in HORIZONS).strip()
    return query, horizons or list(HORIZONS)


def picks_message(f, horizon: str | None = None) -> str:
    with f() as s:
        q = select(m.Recommendation, m.Instrument.symbol).join(m.Instrument).where(
            m.Recommendation.status.in_(("published", "active")))
        if horizon:
            q = q.where(m.Recommendation.horizon == horizon)
        rows = s.execute(q.order_by(m.Recommendation.published_at.desc()).limit(15)).all()
    if not rows:
        return "No open picks." + (f" ({horizon})" if horizon else "") + f"\n\n{DISCLAIMER}"
    lines = [f"<b>{sym}</b> {r.horizon} · conf {float(r.confidence or 0):.2f} · Entry {inr(float(r.entry_low))}–"
             f"{inr(float(r.entry_high))} · Stop {inr(float(r.stop))} · Target {inr(float(r.target))}"
             for r, sym in rows]
    return "\n".join(lines) + f"\n\n{DISCLAIMER}"


def whatif_message(f, text: str) -> str:
    """/whatif crude hits $110 -> estimated sector and portfolio impact (deterministic, no LLM)."""
    from simulator.stress import FACTORS, PRESETS, Shock, StressEngine, parse_scenario

    with f() as s:
        e = StressEngine(s)
        if e.as_of is None:
            return "No price data yet."
        key = text.strip().lower().replace(" ", "_")
        if key in PRESETS:
            name, shocks, notes = PRESETS[key]["name"], [Shock(**x) for x in PRESETS[key]["shocks"]], []
        else:
            shocks, notes = parse_scenario(text, e.latest_levels())
            name = text.strip()
        if not shocks:
            return ("Couldn't find a shock in that. Examples:\n/whatif crude hits $110\n/whatif rupee falls 3% and US yields +50bp\n"
                    "/whatif nifty falls 10%\nPresets: " + ", ".join(PRESETS))
        r = e.run(shocks, name=name, notes=notes)
    shock_txt = ", ".join(f"{FACTORS[x.factor][0]} {x.change:+.4g}{FACTORS[x.factor][1]}" for x in shocks)
    lines = [f"<b>What if: {escape(name)}</b>", escape(shock_txt),
             f"Market (NIFTY 500): {r.market['move_pct']:+.1f}% ({escape(r.market['basis'])})"]
    if r.sectors:
        worst, best = r.sectors[:3], r.sectors[-3:][::-1]
        lines.append("Hit hardest: " + ", ".join(f"{escape(x['sector'])} {x['move_pct']:+.1f}%" for x in worst))
        lines.append("Most resilient: " + ", ".join(f"{escape(x['sector'])} {x['move_pct']:+.1f}%" for x in best))
        clashes = [f"{x['sector']} ({c['factor']})" for x in r.sectors for c in x["map"] if c["agreement"] == "disagrees"]
        if clashes:
            lines.append("History disagrees with the map for: " + escape(", ".join(clashes[:4])))
    for pf in r.portfolios:
        if pf["holdings"]:
            lines.append(f"<b>{escape(pf['name'])}</b>: {inr(pf['pnl'])} ({pf['pnl_pct']:+.2f}% of NAV)"
                         + (f", {pf['stops_breached']} stop(s) breached" if pf["stops_breached"] else ""))
    hit = [x for x in r.open_picks if x["breaches_stop"]]
    if hit:
        lines.append("Open picks that would hit their stop: " + ", ".join(f"{x['symbol']} ({x['horizon']})" for x in hit))
    lines += [escape(n) for n in r.notes[:3]]
    lines.append("Estimate from weekly history, linear; not a forecast.")
    return "\n".join(lines) + f"\n\n{DISCLAIMER}"


def compare_message(f, quant, symbols: list[str]) -> str:
    from quant.compare import compare
    from quant.engine import UnknownSymbol

    with f() as s:
        from sqlalchemy import func

        as_of = s.scalar(select(func.max(m.PriceDaily.trade_date)))
        try:
            r = compare(s, quant, symbols, as_of)
        except (UnknownSymbol, ValueError) as e:
            return f"Can't compare: {escape(str(e))}. Usage: /compare INFY TCS [HCLTECH ...] (2 to 5 symbols)"

    def pct(x):
        return "–" if x is None else f"{x:+.1f}%"
    lines = [f"<b>Compare</b> (as of {r['as_of']})"]
    for x in r["items"]:
        p, fu = x["prices"] or {}, x["fundamentals"] or {}
        scores = " ".join(f"{h[0].upper()}:{v['quant_score']:.0f}" for h, v in x["scores"].items() if v)
        lines.append(f"<b>{x['symbol']}</b> {escape(x['sector'] or '')}\n  3m {pct(p.get('ret_3m_pct'))} · 1y {pct(p.get('ret_1y_pct'))}"
                     f" · vol {p.get('vol_ann_pct', '–')}% · beta {p.get('beta', '–')}"
                     f"\n  quant {scores or '–'} · P/E {fu.get('pe', '–')} · PAT YoY {pct(fu.get('pat_yoy_pct'))}")
    return "\n".join(lines) + f"\n\n{DISCLAIMER}"


def portfolio_message(f) -> str:
    from simulator.metrics import performance

    with f() as s:
        ps = s.scalars(select(m.Portfolio).where(m.Portfolio.archived.is_(False))).all()
        if not ps:
            return "No simulated portfolios yet."
        out = []
        for p in ps:
            met = performance(s, p.id)["metrics"]
            if not met:
                out.append(f"<b>{p.name}</b> ({p.type}): no NAV yet")
                continue
            bench = f" vs {p.benchmark} {met['benchmark_return_pct']:+.2f}%" if met["benchmark_return_pct"] is not None else ""
            out.append(f"<b>{p.name}</b> ({p.type}): NAV ₹{inr(met['nav'])} · {met['total_return_pct']:+.2f}%{bench}"
                       f" · max DD {met['max_drawdown_pct']:.1f}% · {met['closed_trades']} closed")
    return "\n".join(out)


class BotApp:
    def __init__(self, deps_cm, factory=None, owner_chat_id: int = 0):
        self.owner_chat_id = owner_chat_id
        self._deps_cm = deps_cm          # async context manager yielding Deps (kept open for the bot's life)
        self.deps = None
        self.f = factory
        self._tasks: set[asyncio.Task] = set()

    async def start(self, app: Application) -> None:
        self.deps = await self._deps_cm.__aenter__()
        if self.f is None:
            from core.db import session_factory

            self.f = session_factory()

    async def stop(self, app: Application) -> None:
        await self._deps_cm.__aexit__(None, None, None)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query, horizons = parse_analyze_args(context.args or [])
        if not query:
            await update.effective_message.reply_text("Usage: /analyze HDFCBANK [short|medium|long]")
            return
        working = await update.effective_message.reply_text(f"Analysing {query} ({', '.join(horizons)})… this takes 1–2 minutes.")
        task = asyncio.create_task(self._run_analysis(working, query, horizons))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_analysis(self, working, query: str, horizons: list[str]) -> None:
        from orchestrator.graphs import analyze
        from orchestrator.wiring import fresh_budget

        try:
            fresh_budget(self.deps)
            out = await analyze(self.deps, query, datetime.now(IST), horizons)
            await working.edit_text(analysis_message(query, out), parse_mode=ParseMode.HTML)
        except Exception as e:   # tell the owner; details go to the log, not the chat
            log.exception("analysis of %s failed", query)
            await working.edit_text(f"Analysis of {query} failed ({type(e).__name__}). See the server log.")

    async def discover(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        h = (context.args or ["short"])[0].lower()
        if h not in HORIZONS:
            await update.effective_message.reply_text("Usage: /discover [short|medium|long]")
            return
        working = await update.effective_message.reply_text(f"Looking for {h}-term ideas… this takes a few minutes.")
        task = asyncio.create_task(self._run_discover(working, h))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_discover(self, working, horizon: str) -> None:
        from orchestrator.graphs import discover
        from orchestrator.wiring import fresh_budget

        try:
            fresh_budget(self.deps)
            out = await discover(self.deps, datetime.now(IST), horizon=horizon, max_ideas=3)
            await working.edit_text(analysis_message(f"{horizon}-term ideas", out), parse_mode=ParseMode.HTML)
        except Exception as e:
            log.exception("discover failed")
            await working.edit_text(f"Discovery failed ({type(e).__name__}). See the server log.")

    async def events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from datetime import timedelta as td

        evs = sorted(self.deps.store.active_events(datetime.now(timezone.utc) - td(days=3)), key=lambda e: -e["severity"])[:10]
        if not evs:
            await update.effective_message.reply_text("No active events in the last 3 days.")
            return
        lines = []
        for e in evs:
            imp = ", ".join(f"{i['sector']} {'▲' if i['direction'] > 0 else '▼'}" for i in e.get("impacts", [])[:4])
            lines.append(f"<b>sev {e['severity']}</b> {escape(e['title'])}" + (f"\n   {escape(imp)}" if imp else ""))
        await update.effective_message.reply_text("\n".join(lines) + f"\n\n{DISCLAIMER}", parse_mode=ParseMode.HTML)

    async def ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Free text -> grounded chat (router, then Q&A / analysis / discovery)."""
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        working = await update.effective_message.reply_text("Looking into it…")
        task = asyncio.create_task(self._run_chat(working, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_chat(self, working, text: str) -> None:
        from orchestrator.graphs import chat
        from orchestrator.wiring import fresh_budget

        try:
            fresh_budget(self.deps)
            out = await chat(self.deps, text, datetime.now(IST))
            if out.get("results"):
                await working.edit_text(analysis_message(text, {"run_id": out.get("run_id", 0), "results": out["results"]}),
                                        parse_mode=ParseMode.HTML)
            else:
                await working.edit_text(f"{escape(out.get('reply') or 'No answer.')}\n\n{DISCLAIMER}", parse_mode=ParseMode.HTML)
        except Exception as e:
            log.exception("chat failed")
            await working.edit_text(f"Sorry, that failed ({type(e).__name__}).")

    async def picks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        h = (context.args or [None])[0]
        await update.effective_message.reply_text(picks_message(self.f, h if h in HORIZONS else None),
                                                  parse_mode=ParseMode.HTML)

    async def portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(portfolio_message(self.f), parse_mode=ParseMode.HTML)

    async def inbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from hitl.service import HitlService

        tasks = HitlService(self.f).list_tasks("pending", 20)
        if not tasks:
            await update.effective_message.reply_text("Review inbox is empty.")
            return
        for t in tasks:
            headline = (t["payload"].get("card") or {}).get("headline") or t["payload"].get("symbol") or ""
            await update.effective_message.reply_text(
                f"#{t['id']} {t['task_type'].replace('_', ' ')}: {headline}\nWhy: {t['reason']}\n"
                f"AI suggests: {t['ai_recommendation']}", reply_markup=task_keyboard(t["id"], t["allowed_actions"]))

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from hitl.service import DecisionError, HitlService

        q = update.callback_query
        # Callback queries can't be filtered by chat in the handler, so check ownership here.
        if q.message is None or q.message.chat.id != self.owner_chat_id:
            await q.answer("Not allowed", show_alert=True)
            return
        await q.answer()
        try:
            _, tid, action = q.data.split(":")
            res = HitlService(self.f, self.deps.cfg.rr_floor if self.deps else None).decide(int(tid), action,
                                                                                            decided_by="owner:telegram")
            await q.edit_message_text(f"{q.message.text}\n\n✔ {action}: {'; '.join(res.effects)}")
        except (DecisionError, ValueError) as e:
            await q.edit_message_text(f"{q.message.text}\n\n✖ {e}")

    async def whatif(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args or [])
        if not text:
            await update.effective_message.reply_text("Usage: /whatif crude hits $110 (or a preset: crude_spike, rupee_slide, "
                                                      "us_yields_up, correction, risk_off)")
            return
        msg = await asyncio.to_thread(whatif_message, self.f, text)
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def compare(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from quant.engine import QuantEngine

        if getattr(self, "quant", None) is None:
            self.quant = QuantEngine(self.f, get_settings())
        syms = [a for arg in (context.args or []) for a in arg.replace(",", " ").split()]
        await update.effective_message.reply_text("Comparing…")
        msg = await asyncio.to_thread(compare_message, self.f, self.quant, syms)
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
        from orchestrator.wiring import load_config

        cfg = load_config(self.f)          # latest approved, so a change approved elsewhere isn't overwritten
        cfg.kill_switch = enabled
        with self.f() as s, s.begin():
            s.add(m.ConfigVersion(config=cfg.model_dump(mode="json"), created_by="kill_switch:telegram", approved=True))
        self.deps.cfg = cfg
        await update.effective_message.reply_text(
            "⏸ Publishing paused: everything goes to the review inbox." if enabled else "▶ Publishing resumed.")


def build_app(token: str, owner_chat_id: int, deps_cm, factory=None) -> Application:
    bot = BotApp(deps_cm, factory, owner_chat_id)
    app = Application.builder().token(token).post_init(bot.start).post_shutdown(bot.stop).build()
    owner = filters.Chat(chat_id=owner_chat_id)
    app.add_handler(CommandHandler(["start", "help"], bot.help, filters=owner))
    app.add_handler(CommandHandler("analyze", bot.analyze, filters=owner))
    app.add_handler(CommandHandler("picks", bot.picks, filters=owner))
    app.add_handler(CommandHandler("discover", bot.discover, filters=owner))
    app.add_handler(CommandHandler("events", bot.events, filters=owner))
    app.add_handler(CommandHandler("portfolio", bot.portfolio, filters=owner))
    app.add_handler(CommandHandler("inbox", bot.inbox, filters=owner))
    app.add_handler(CommandHandler("whatif", bot.whatif, filters=owner))
    app.add_handler(CommandHandler("compare", bot.compare, filters=owner))
    app.add_handler(CommandHandler("pause", lambda u, c: bot.kill(u, c, True), filters=owner))
    app.add_handler(CommandHandler("resume", lambda u, c: bot.kill(u, c, False), filters=owner))
    app.add_handler(CallbackQueryHandler(bot.on_button, pattern=r"^hitl:\d+:\w+$"))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, bot.ask))
    app.bot_data["owner_chat_id"] = owner_chat_id
    return app


def main() -> None:
    from core.net import setup_tls
    from orchestrator.wiring import real_deps

    setup_tls()
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_owner_chat_id:
        raise SystemExit("Set STOCK_PRIMER_TELEGRAM_BOT_TOKEN and STOCK_PRIMER_TELEGRAM_OWNER_CHAT_ID in .env")
    build_app(s.telegram_bot_token, s.telegram_owner_chat_id, real_deps(llm="live")).run_polling()


if __name__ == "__main__":
    main()
