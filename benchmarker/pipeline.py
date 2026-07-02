"""Orchestration bout-en-bout : critères → dates → prix → benchmark."""

from __future__ import annotations

from typing import Callable, List, Optional

import anthropic

from .discovery import discover
from .extraction import extract_prices
from .llm import build_client
from .models import Concert, SearchCriteria


def build_benchmark(
    criteria: SearchCriteria,
    *,
    client: Optional[anthropic.Anthropic] = None,
    with_prices: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Concert]:
    """Construit le benchmark complet pour un jeu de critères.

    1. Découverte des dates (recherche web).
    2. Extraction des prix par date (web fetch), si `with_prices`.
    """
    client = client or build_client()
    log = progress or (lambda _msg: None)

    log("Découverte des dates…")
    concerts = discover(client, criteria)
    log(f"{len(concerts)} date(s) trouvée(s).")

    if with_prices:
        for i, concert in enumerate(concerts, start=1):
            label = f"{concert.artist} — {concert.venue or '?'}"
            log(f"[{i}/{len(concerts)}] Extraction prix : {label}")
            extract_prices(client, concert)

    return concerts
