"""Phase 2 — Extraction de la grille tarifaire par date.

Pour chaque date disposant d'une URL billetterie, on demande au LLM de
récupérer la page (web_fetch) et d'en extraire la grille par catégorie, avec
un niveau de confiance et un horodatage. C'est ici que la donnée « prix par
catégorie » est la plus fragile : on qualifie systématiquement ce qu'on trouve.
"""

from __future__ import annotations

from datetime import datetime, timezone

import anthropic

from . import config
from .llm import parse_structured, run_server_tool_loop
from .models import Concert, PriceConfidence, PriceExtraction

_EXTRACTION_SYSTEM = """Tu es un analyste billetterie. On te donne l'URL d'une page de vente
de billets pour un concert. Récupère la page et extrais la grille tarifaire complète.

Règles :
- Récupère le contenu de l'URL fournie avec l'outil web_fetch.
- Liste chaque catégorie de place avec son prix (Carré Or, Fosse, Cat. 1/2/3, Placement libre…).
- Indique si les frais de location sont inclus quand l'information est disponible.
- Sois honnête sur la complétude : si tu ne vois qu'un « à partir de X€ », dis-le.
- N'invente aucun prix. Reste strictement factuel sur ce que la page affiche."""

_EXTRACTION_INSTRUCTION = """À partir du contenu de page ci-dessous, extrais la grille tarifaire
structurée : liste des catégories avec prix et devise, un niveau de confiance
(grille_complete / grille_partielle / prix_a_partir_de / aucun_prix), et une note
éventuelle (early bird, dynamic pricing, complet, etc.)."""


def extract_prices(client: anthropic.Anthropic, concert: Concert) -> Concert:
    """Enrichit un `Concert` avec sa grille tarifaire (in place et retourné)."""
    concert.priced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not concert.source_url:
        concert.price_confidence = PriceConfidence.NONE
        concert.notes = _append_note(concert.notes, "Pas d'URL billetterie disponible.")
        return concert

    user = (
        f"Voici la page billetterie pour {concert.artist}"
        f"{f' à {concert.venue}' if concert.venue else ''}"
        f"{f' le {concert.date}' if concert.date else ''}.\n"
        f"URL : {concert.source_url}\n\n"
        "Récupère la page et donne-moi la grille tarifaire complète par catégorie."
    )

    web_fetch = dict(config.WEB_FETCH_TOOL)
    web_fetch["max_uses"] = config.MAX_FETCH_USES

    try:
        page_text = run_server_tool_loop(
            client,
            system=_EXTRACTION_SYSTEM,
            user=user,
            tools=[web_fetch],
            effort=config.EXTRACTION_EFFORT,
        )
    except Exception as exc:  # récupération réseau / refus / autre
        concert.price_confidence = PriceConfidence.NONE
        concert.notes = _append_note(concert.notes, f"Échec récupération : {exc}")
        return concert

    if not page_text:
        concert.price_confidence = PriceConfidence.NONE
        concert.notes = _append_note(concert.notes, "Page vide ou inaccessible.")
        return concert

    parsed = parse_structured(
        client,
        schema=PriceExtraction,
        instruction=_EXTRACTION_INSTRUCTION,
        material=page_text,
        effort="medium",
    )

    if parsed is None:
        concert.price_confidence = PriceConfidence.NONE
        return concert

    concert.prices = parsed.prices
    concert.price_confidence = parsed.price_confidence
    if parsed.notes:
        concert.notes = _append_note(concert.notes, parsed.notes)
    return concert


def _append_note(existing: str | None, addition: str) -> str:
    """Concatène proprement deux notes."""
    if existing:
        return f"{existing} | {addition}"
    return addition
