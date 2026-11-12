import asyncio
from types import SimpleNamespace

from telegram.ext import CommandHandler

from bot.format import analysis_message, card_message, inr
from bot.main import BotApp, build_app, parse_analyze_args, picks_message
from bot.notifier import TelegramNotifier, chunks, task_keyboard


def test_inr_grouping():
    assert inr(1234567.5) == "12,34,567.50"
    assert inr(950) == "950.00"
    assert inr(-12345) == "-12,345.00"


def test_parse_analyze_args():
    assert parse_analyze_args(["HDFC", "Bank", "short"]) == ("HDFC Bank", ["short"])
    assert parse_analyze_args(["TCS"]) == ("TCS", ["short", "medium", "long"])


CARD = {"headline": "Breakout <with> volume", "levels": {"entry_low": 1610, "entry_high": 1650, "stop": 1540, "target": 1820},
        "bull_case": ["Volume rose"], "bear_case": ["Results inside window"],
        "invalidation": [{"condition": "Close below 200-DMA for 3 sessions"}]}


def test_card_message_escapes_and_shows_levels():
    msg = card_message({"symbol": "HDFCBANK", "horizon": "medium", "verdict": "favourable", "confidence": 0.62,
                        "status": "validated", "card": CARD})
    assert "&lt;with&gt;" in msg and "1,610.00" in msg and "Close below 200-DMA" in msg


def test_analysis_message_orders_horizons_and_shows_gaps():
    results = [{"symbol": "X", "horizon": h, "verdict": "neutral", "confidence": 0.5, "status": "rejected",
                "card": None, "violations": [{"message": "no supported claims"}], "gaps": ["fundamental: none"]}
               for h in ("long", "short")]
    msg = analysis_message("X", {"run_id": 7, "results": results})
    assert msg.index("· short") < msg.index("· long")
    assert "Data gaps: fundamental" in msg and "Run #7" in msg and "Not investment advice" in msg


def test_commands_are_owner_only():
    app = build_app("123:abc", 42, deps_cm=None)
    cmds = [h for group in app.handlers.values() for h in group if isinstance(h, CommandHandler)]
    assert len(cmds) >= 6 and all(42 in h.filters.chat_ids for h in cmds)


def test_buttons_from_other_chats_are_refused():
    bot = BotApp(None, None, owner_chat_id=42)
    answered = []

    async def answer(text=None, show_alert=False):
        answered.append((text, show_alert))
    q = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=99), text="x"), data="hitl:1:approve",
                        answer=answer, edit_message_text=None)
    asyncio.run(bot.on_button(SimpleNamespace(callback_query=q), None))
    assert answered == [("Not allowed", True)]


def test_task_keyboard_and_chunks():
    kb = task_keyboard(5, ["approve", "edit", "reject"])
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["hitl:5:approve", "hitl:5:reject"]
    parts = chunks("line\n" * 2000, size=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("line") == 2000


def test_notifier_attaches_buttons_to_last_chunk():
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, disable_web_page_preview=True):
            sent.append((chat_id, len(text), reply_markup is not None))
    n = TelegramNotifier("t", 42, bot=FakeBot())
    asyncio.run(n.send("telegram", "hitl_task", "x\n" * 3000, {"task_id": 3, "actions": ["approve", "reject"]}))
    assert sent[-1][2] and not any(b for *_, b in sent[:-1]) and all(c == 42 for c, *_ in sent)


def test_picks_message_empty():
    from .synth import synthetic_db
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=30)
    assert "No open picks" in picks_message(f)
