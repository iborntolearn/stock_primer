"""Telegram notifier (§10): digest, alerts, and review tasks with one-tap decision buttons."""
from __future__ import annotations

from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

MAX_LEN = 4000
BUTTON_ACTIONS = ("approve", "reject", "close", "hold", "confirm", "dismiss", "exclude", "accept")


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:          # a single very long line
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def task_keyboard(task_id: int, actions: list[str]) -> Optional[InlineKeyboardMarkup]:
    buttons = [InlineKeyboardButton(a.title(), callback_data=f"hitl:{task_id}:{a}") for a in actions if a in BUTTON_ACTIONS]
    return InlineKeyboardMarkup([buttons]) if buttons else None


class TelegramNotifier:
    def __init__(self, token: str, owner_chat_id: int, bot: Optional[Bot] = None):
        self.bot = bot or Bot(token)
        self.chat_id = owner_chat_id

    async def send(self, channel: str, kind: str, text: str, payload: Optional[dict] = None) -> None:
        markup = None
        if kind == "hitl_task" and payload:
            markup = task_keyboard(payload["task_id"], payload.get("actions", []))
        parts = chunks(text)
        for i, part in enumerate(parts):
            await self.bot.send_message(self.chat_id, part, reply_markup=markup if i == len(parts) - 1 else None,
                                        disable_web_page_preview=True)
