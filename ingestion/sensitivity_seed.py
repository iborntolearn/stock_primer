"""Initial macro-factor -> sector transmission map (§3.4 sector_sensitivity, feature B2).

A starting point written from widely understood Indian-market sensitivities, using
NSE's own industry names. Sectors with mixed exposure (e.g. "Oil Gas & Consumable
Fuels" holds both upstream producers and refiners) are deliberately left out:
event impact there falls back to LLM inference, which is capped and reviewed.
Rows are approved so the pipeline can use them, but marked approved_by="seed: review me".
Edits later go through LLM-13 proposals and the approve_map_change review task.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m
from core.store import SqlStore

# (factor code, sector, direction, magnitude, rationale)
SEED_MAP = [
    ("crude_up", "Automobile and Auto Components", -1, 0.4, "Fuel costs dent demand; plastics and rubber inputs get dearer"),
    ("crude_up", "Chemicals", -1, 0.4, "Crude-derived feedstock costs rise faster than prices"),
    ("crude_up", "Consumer Durables", -1, 0.4, "Paints and durables use crude derivatives; margins compress"),
    ("crude_up", "Fast Moving Consumer Goods", -1, 0.2, "Packaging and freight costs rise"),
    ("crude_up", "Services", -1, 0.3, "Aviation and logistics fuel costs rise"),
    ("crude_up", "Construction Materials", -1, 0.3, "Cement kiln fuel and freight costs rise"),
    ("usdinr_up", "Information Technology", 1, 0.5, "Dollar revenues translate into more rupees"),
    ("usdinr_up", "Healthcare", 1, 0.3, "Pharma exports benefit from a weaker rupee"),
    ("usdinr_up", "Textiles", 1, 0.2, "Export competitiveness improves"),
    ("us10y_up", "Realty", -1, 0.3, "Higher global yields push up funding costs and pull FII money out"),
    ("us10y_up", "Information Technology", -1, 0.2, "Long-duration growth valuations de-rate; FII outflows"),
    ("us10y_up", "Financial Services", -1, 0.2, "Foreign-portfolio outflows hit the most FII-owned sector"),
    ("repo_up", "Realty", -1, 0.5, "Home-loan rates rise; demand and developer funding tighten"),
    ("repo_up", "Automobile and Auto Components", -1, 0.3, "Vehicle financing costs rise"),
    ("repo_up", "Consumer Durables", -1, 0.2, "Consumer financing costs rise"),
    ("monsoon_weak", "Fast Moving Consumer Goods", -1, 0.4, "Rural demand weakens"),
    ("monsoon_weak", "Automobile and Auto Components", -1, 0.3, "Tractor and two-wheeler demand is rural"),
    ("monsoon_weak", "Chemicals", -1, 0.3, "Agrochemical demand falls"),
]


def seed_sensitivity_map(s: Session) -> int:
    if s.scalar(select(m.SectorSensitivity.id).limit(1)) is not None:
        return 0
    factors = {f.code: f.id for f in s.scalars(select(m.MacroFactor))}
    n = 0
    for code, sector, direction, magnitude, rationale in SEED_MAP:
        if code not in factors:
            continue
        s.add(m.SectorSensitivity(factor_id=factors[code], sector_id=SqlStore.sector_id(s, sector), direction=direction,
                                  magnitude=Decimal(str(magnitude)), rationale=rationale, version=1, status="approved",
                                  proposed_by="seed", approved_by="seed: review me"))
        n += 1
    return n
