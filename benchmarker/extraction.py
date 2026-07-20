"""Phase 2 — Extraction de la grille tarifaire par date.

Trois stratégies de récupération de la page billetterie :
  - "web_fetch"  : le LLM récupère la page via l'outil serveur web_fetch ;
  - "playwright" : on rend la page dans Chromium (JS exécuté) et on structure ;
  - "auto"       : web_fetch d'abord, fallback Playwright si la grille est faible.

Dans tous les cas, la structuration finale (texte → grille) passe par le même
appel LLM typé. C'est ici que la donnée « prix par catégorie » est la plus
fragile : on qualifie systématiquement ce qu'on trouve (niveau de confiance).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import anthropic

from . import config
from .llm import parse_structured, run_server_tool_loop
from .models import Concert, PriceConfidence, PriceExtraction

_FETCH_SYSTEM = """Tu es un analyste billetterie. On te donne l'URL d'une page de vente
de billets pour un concert. Récupère la page et restitue son contenu tarifaire.

Règles :
- Récupère le contenu de l'URL fournie avec l'outil web_fetch.
- Liste chaque catégorie de place avec son prix (Carré Or, Fosse, Cat. 1/2/3, Placement libre…).
- Indique si les frais de location sont inclus quand l'information est disponible.
- N'invente aucun prix. Reste strictement factuel sur ce que la page affiche."""

_STRUCTURE_INSTRUCTION = """À partir du contenu de page ci-dessous, extrais la grille tarifaire
structurée : liste des catégories avec prix et devise, un niveau de confiance
(grille_complete / grille_partielle / prix_a_partir_de / aucun_prix), et une note
éventuelle (early bird, dynamic pricing, complet, etc.). N'invente aucun prix."""


def extract_prices(
    client: anthropic.Anthropic,
    concert: Concert,
    *,
    strategy: str = None,
    renderer=None,
) -> Concert:
    """Enrichit un `Concert` avec sa grille tarifaire (in place et retourné).

    `strategy` : "web_fetch" | "playwright" | "auto" (défaut : config).
    `renderer` : instance `PageRenderer` réutilisable (optionnel, pour Playwright).
    """
    strategy = strategy or config.FETCH_STRATEGY
    concert.priced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not concert.source_url:
        concert.price_confidence = PriceConfidence.NONE
        concert.notes = _append_note(concert.notes, "Pas d'URL billetterie disponible.")
        return concert

    result: Optional[PriceExtraction] = None
    used: Optional[str] = None

    # 1) Playwright d'abord (rendu local GRATUIT) pour "playwright" et "auto".
    #    Seule la structuration (Haiku) coûte, et une seule fois.
    if strategy in ("playwright", "auto"):
        rendered, err = _gather_via_playwright(concert.source_url, renderer)
        if rendered:
            result = _structure(client, rendered)
            used = "playwright"
        elif strategy == "playwright":
            concert.notes = _append_note(concert.notes, f"playwright : {err}")

    # 2) web_fetch (LLM, coûteux) : stratégie dédiée, ou fallback "auto"
    #    UNIQUEMENT si le rendu gratuit n'a pas donné de grille suffisante.
    need_web_fetch = strategy == "web_fetch" or (
        strategy == "auto" and not _is_sufficient(result)
    )
    if need_web_fetch:
        page_text, err = _gather_via_web_fetch(client, concert)
        if page_text:
            candidate = _structure(client, page_text)
            if _is_better(candidate, result):
                result, used = candidate, "web_fetch"
        elif not result:
            concert.notes = _append_note(concert.notes, f"web_fetch : {err}")

    # 3) Application du résultat
    if result is None:
        concert.price_confidence = PriceConfidence.NONE
        return concert

    concert.prices = result.prices
    concert.price_confidence = result.price_confidence
    if used:
        concert.notes = _append_note(concert.notes, f"source prix : {used}")
    if result.notes:
        concert.notes = _append_note(concert.notes, result.notes)
    return concert


# --- collecte de la page ----------------------------------------------------
def _gather_via_web_fetch(
    client: anthropic.Anthropic, concert: Concert
) -> tuple[Optional[str], Optional[str]]:
    """Récupère la page via l'outil serveur web_fetch. Renvoie (texte, erreur)."""
    user = (
        f"Voici la page billetterie pour {concert.artist}"
        f"{f' à {concert.venue}' if concert.venue else ''}"
        f"{f' le {concert.date}' if concert.date else ''}.\n"
        f"URL : {concert.source_url}\n\n"
        "Récupère la page et donne-moi la grille tarifaire complète par catégorie."
    )
    web_fetch = dict(config.WEB_FETCH_TOOL)
    web_fetch["max_uses"] = config.MAX_FETCH_USES
    web_fetch["max_content_tokens"] = config.FETCH_MAX_CONTENT_TOKENS
    try:
        text = run_server_tool_loop(
            client,
            system=_FETCH_SYSTEM,
            user=user,
            tools=[web_fetch],
            model=config.EXTRACTION_MODEL,
            effort=config.EXTRACTION_EFFORT,
        )
        return (text or None), (None if text else "page vide ou inaccessible")
    except Exception as exc:  # réseau / refus / autre
        return None, str(exc)


def _gather_via_playwright(
    url: str, renderer
) -> tuple[Optional[str], Optional[str]]:
    """Rend la page avec Chromium. Renvoie (texte, erreur).

    Si un `renderer` (PageRenderer) est fourni, il est réutilisé ; sinon on
    ouvre un navigateur dédié le temps de la page.
    """
    try:
        if renderer is not None:
            text = renderer.render(url)
        else:
            from .browser import render_page

            text = render_page(url)
        return (text or None), (None if text else "rendu vide")
    except Exception as exc:
        return None, str(exc)


# --- structuration ----------------------------------------------------------
def _structure(client: anthropic.Anthropic, page_text: str) -> Optional[PriceExtraction]:
    """Structure le texte de page en grille tarifaire typée."""
    return parse_structured(
        client,
        schema=PriceExtraction,
        instruction=_STRUCTURE_INSTRUCTION,
        material=page_text,
    )


def _is_sufficient(result: Optional[PriceExtraction]) -> bool:
    """Une grille est « suffisante » si complète, ou partielle avec ≥1 prix."""
    if result is None:
        return False
    if result.price_confidence == PriceConfidence.COMPLETE:
        return True
    if result.price_confidence == PriceConfidence.PARTIAL and result.prices:
        return True
    return False


def _is_better(candidate: Optional[PriceExtraction], current: Optional[PriceExtraction]) -> bool:
    """Ordonne les résultats par qualité pour choisir le meilleur (fallback auto)."""
    if candidate is None:
        return False
    if current is None:
        return True
    rank = {
        PriceConfidence.NONE: 0,
        PriceConfidence.FROM_PRICE: 1,
        PriceConfidence.PARTIAL: 2,
        PriceConfidence.COMPLETE: 3,
    }
    if rank[candidate.price_confidence] != rank[current.price_confidence]:
        return rank[candidate.price_confidence] > rank[current.price_confidence]
    # À confiance égale, on préfère la grille avec le plus de catégories.
    return len(candidate.prices) > len(current.prices)


def _append_note(existing: str | None, addition: str) -> str:
    """Concatène proprement deux notes."""
    if existing:
        return f"{existing} | {addition}"
    return addition
