from __future__ import annotations

from dataclasses import dataclass

from .budget import BudgetManager
from .config import Config
from .ports import LLM, AllowlistedTools, Notifier, Store


@dataclass
class Deps:
    """Everything a graph node needs. Built once per run and closed over by the graph builders."""

    cfg: Config
    tools: AllowlistedTools
    store: Store
    llm: LLM
    notifier: Notifier
    budget: BudgetManager
