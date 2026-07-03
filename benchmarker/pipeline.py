"""Orchestration bout-en-bout : critères → dates → prix → benchmark."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Callable, List, Optional

import anthropic

from . import config
from .discovery import discover
from .extraction import extract_prices
from .llm import build_client
from .models import Concert, SearchCriteria


def build_benchmark(
    criteria: SearchCriteria,
    *,
    client: Optional[anthropic.Anthropic] = None,
    with_prices: bool = True,
    strategy: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Concert]:
    """Construit le benchmark complet pour un jeu de critères.

    1. Découverte des dates (recherche web).
    2. Extraction des prix par date, si `with_prices`, selon `strategy`
       ("web_fetch" | "playwright" | "auto").

    Pour les stratégies utilisant le navigateur, un unique `PageRenderer` est
    ouvert puis réutilisé sur toutes les dates (bien plus efficace).
    """
    client = client or build_client()
    strategy = strategy or config.FETCH_STRATEGY
    log = progress or (lambda _msg: None)

    log("Découverte des dates…")
    concerts = discover(client, criteria)
    log(f"{len(concerts)} date(s) trouvée(s).")

    if not with_prices:
        return concerts

    with _renderer_for(strategy, log) as renderer:
        for i, concert in enumerate(concerts, start=1):
            label = f"{concert.artist} — {concert.venue or '?'}"
            log(f"[{i}/{len(concerts)}] Extraction prix ({strategy}) : {label}")
            try:
                extract_prices(client, concert, strategy=strategy, renderer=renderer)
            except Exception as exc:  # noqa: BLE001
                if _is_fatal(exc):
                    # Crédits épuisés / auth invalide : inutile de continuer, mais
                    # on conserve tout ce qui a déjà été collecté.
                    log(f"Arrêt : erreur fatale ({exc}). "
                        f"{i - 1}/{len(concerts)} date(s) traitée(s), résultats conservés.")
                    break
                # Erreur ponctuelle sur cette date : on la note et on continue.
                log(f"  ! date ignorée ({exc})")
                concert.notes = _append_note(concert.notes, f"extraction échouée : {exc}")

    return concerts


def _is_fatal(exc: Exception) -> bool:
    """Distingue les erreurs qui condamnent tout le run (crédits, auth)."""
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("credit balance", "billing", "quota", "insufficient"))


def _append_note(existing, addition: str) -> str:
    if existing:
        return f"{existing} | {addition}"
    return addition


def _renderer_for(strategy: str, log: Callable[[str], None]):
    """Ouvre un PageRenderer partagé si la stratégie en a besoin, sinon rien.

    En cas d'indisponibilité de Playwright, on dégrade proprement (renderer
    None) : `extract_prices` retombera sur un navigateur à la demande ou une
    note d'échec explicite.
    """
    if strategy not in ("playwright", "auto"):
        return nullcontext(None)
    try:
        from .browser import PageRenderer

        return PageRenderer()
    except Exception as exc:  # playwright non installé, etc.
        log(f"Playwright indisponible ({exc}) — extraction sans rendu navigateur.")
        return nullcontext(None)
