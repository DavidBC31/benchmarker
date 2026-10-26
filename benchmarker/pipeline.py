"""Orchestration bout-en-bout : critères → dates → prix → benchmark."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date as _date
from typing import Callable, List, Optional

import anthropic

from . import config
from .discovery import discover
from .extraction import extract_prices
from .festivals import discover_festivals, find_similar_festivals
from .llm import build_client
from .models import (
    Concert,
    Festival,
    FestivalCriteria,
    PriceConfidence,
    PricedItem,
    SearchCriteria,
)
from .similar_artists import find_similar_artists

_CONFIDENCE_RANK = {
    PriceConfidence.COMPLETE: 3,
    PriceConfidence.PARTIAL: 2,
    PriceConfidence.FROM_PRICE: 1,
    PriceConfidence.NONE: 0,
}


def _price_priority_key(item: PricedItem) -> tuple:
    """Trie les événements avec prix en premier (meilleure confiance d'abord),
    puis par proximité avec aujourd'hui (passé récent ou futur proche avant
    les dates plus éloignées dans le temps) — sans jamais rien supprimer."""
    rank = _CONFIDENCE_RANK.get(item.price_confidence, 0)
    try:
        d = _date.fromisoformat((item.sort_date() or "")[:10])
        recency = abs((d - _date.today()).days)
    except ValueError:
        recency = 10**6  # date absente/non parsable : reléguée en fin de groupe
    return (-rank, recency)


def resolve_target_artists(
    client: anthropic.Anthropic,
    criteria: SearchCriteria,
    settings: config.RunSettings,
    log: Callable[[str], None],
) -> SearchCriteria:
    """Résout `reference_artist` en `target_artists` (+ style déduit si absent).

    Idempotente et sans coût si `target_artists` est déjà renseigné ou si
    `reference_artist` est absent : sûr à appeler explicitement en amont (pour
    exposer les critères résolus, ex. dans l'historique) puis à nouveau depuis
    `build_benchmark` sans déclencher une seconde recherche.
    """
    if not criteria.reference_artist or criteria.target_artists:
        return criteria
    log(f"Recherche d'artistes comparables à « {criteria.reference_artist} »…")
    similar = find_similar_artists(client, criteria.reference_artist, settings=settings)
    artists = [criteria.reference_artist] + similar.similar_artists
    resolved = criteria.model_copy(
        update={
            "target_artists": artists,
            "genres": criteria.genres or ([similar.inferred_genre] if similar.inferred_genre else []),
        }
    )
    genre_note = f" (style détecté : {similar.inferred_genre})" if similar.inferred_genre else ""
    log(f"Artistes retenus : {', '.join(artists)}{genre_note}")
    return resolved


def build_benchmark(
    criteria: SearchCriteria,
    *,
    client: Optional[anthropic.Anthropic] = None,
    with_prices: bool = True,
    profile: Optional[str] = None,
    strategy: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Concert]:
    """Construit le benchmark complet pour un jeu de critères.

    0. Si `criteria.reference_artist` est fourni (et `target_artists` vide) :
       résolution d'artistes comparables (un seul appel de recherche), qui
       oriente la découverte sur cette liste précise plutôt que sur un style
       générique. Le style est aussi déduit automatiquement si absent.
    1. Découverte des dates (recherche web).
    2. Extraction des prix par date, si `with_prices`.

    `profile` : "eco" | "equilibre" | "max" (compromis coût/qualité).
    `strategy` : force la stratégie d'extraction (sinon celle du profil).

    Pour les stratégies utilisant le navigateur, un unique `PageRenderer` est
    ouvert puis réutilisé sur toutes les dates (bien plus efficace).
    """
    client = client or build_client()
    settings = config.resolve_settings(profile, strategy)
    log = progress or (lambda _msg: None)

    criteria = resolve_target_artists(client, criteria, settings, log)

    log(f"Découverte des dates… (profil : {profile or config.DEFAULT_PROFILE})")
    concerts = discover(client, criteria, settings, progress=log)
    # Avec des artistes ciblés, tous partagent par construction le style de
    # l'artiste de référence : on le reporte là où la découverte l'a laissé
    # vide (sinon le filtre par style de la préconisation les ignore).
    if criteria.target_artists and criteria.genres:
        for c in concerts:
            c.genre = c.genre or criteria.genres[0]
    if concerts:
        log(f"{len(concerts)} date(s) trouvée(s).")
    else:
        log(
            "0 date trouvée. Voir les lignes ci-dessus pour la cause (recherche "
            "vide vs structuration vide) ; sinon, essaie des critères moins "
            "restrictifs ou le profil 'equilibre'."
        )
        return concerts

    if not with_prices:
        return concerts

    _extract_all(client, concerts, settings, log)
    return concerts


def resolve_target_festivals(
    client: anthropic.Anthropic,
    criteria: FestivalCriteria,
    settings: config.RunSettings,
    log: Callable[[str], None],
) -> FestivalCriteria:
    """Résout `reference_festival` en `target_festivals` (+ programmation
    déduite si absente). Idempotente, comme `resolve_target_artists`."""
    if not criteria.reference_festival or criteria.target_festivals:
        return criteria
    log(f"Recherche de festivals comparables à « {criteria.reference_festival} »…")
    similar = find_similar_festivals(client, criteria.reference_festival, settings=settings)
    festivals = [criteria.reference_festival] + similar.similar_festivals
    resolved = criteria.model_copy(
        update={
            "target_festivals": festivals,
            "genres": criteria.genres or ([similar.inferred_genre] if similar.inferred_genre else []),
        }
    )
    if similar.inferred_profile:
        log(f"Profil de référence : {similar.inferred_profile}")
    log(f"Festivals retenus : {', '.join(festivals)}")
    return resolved


def build_festival_benchmark(
    criteria: FestivalCriteria,
    *,
    client: Optional[anthropic.Anthropic] = None,
    with_prices: bool = True,
    profile: Optional[str] = None,
    strategy: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Festival]:
    """Pendant festival de `build_benchmark` : festivals comparables (si
    festival de référence), découverte, puis extraction des grilles de pass."""
    client = client or build_client()
    settings = config.resolve_settings(profile, strategy)
    log = progress or (lambda _msg: None)

    criteria = resolve_target_festivals(client, criteria, settings, log)

    log(f"Découverte des festivals… (profil : {profile or config.DEFAULT_PROFILE})")
    festivals = discover_festivals(client, criteria, settings, progress=log)
    if festivals:
        log(f"{len(festivals)} festival(s) trouvé(s).")
    else:
        log(
            "0 festival trouvé. Voir les lignes ci-dessus pour la cause ; sinon, "
            "essaie des critères moins restrictifs ou le profil 'equilibre'."
        )
        return festivals

    if with_prices:
        _extract_all(client, festivals, settings, log)
    return festivals


def _extract_all(
    client: anthropic.Anthropic,
    items: List[PricedItem],
    settings: config.RunSettings,
    log: Callable[[str], None],
) -> None:
    """Extrait les prix de chaque événement (in place), puis les réordonne.

    Un unique `PageRenderer` est partagé sur tous les événements. Une erreur
    fatale (crédits, auth) stoppe la boucle en conservant l'acquis ; une
    erreur ponctuelle est notée sur l'événement concerné.
    """
    with _renderer_for(settings.fetch_strategy, log) as renderer:
        for i, item in enumerate(items, start=1):
            log(f"[{i}/{len(items)}] Extraction prix ({settings.fetch_strategy}) : {item.short_label()}")
            try:
                extract_prices(client, item, settings=settings, renderer=renderer)
            except Exception as exc:  # noqa: BLE001
                if _is_fatal(exc):
                    log(f"Arrêt : erreur fatale ({exc}). "
                        f"{i - 1}/{len(items)} événement(s) traité(s), résultats conservés.")
                    break
                log(f"  ! événement ignoré ({exc})")
                item.notes = _append_note(item.notes, f"extraction échouée : {exc}")

    # Fait ressortir en priorité les événements avec un vrai prix exploitable
    # (rien n'est supprimé, juste réordonné) : à confiance égale, les plus
    # proches d'aujourd'hui passent avant les plus éloignés dans le temps.
    items.sort(key=_price_priority_key)


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
