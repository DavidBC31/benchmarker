"""Orchestration bout-en-bout : critères → dates → prix → benchmark."""

from __future__ import annotations

import queue
import threading
from contextlib import nullcontext
from datetime import date as _date
from typing import Callable, List, Optional

import anthropic

from . import config, knowledge, price_cache
from .analysis import check_grid
from .comparability import score_concerts, score_festivals
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
    coerce_setting,
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
    puis les plus comparables à la cible, puis par proximité avec aujourd'hui
    (passé récent ou futur proche avant les dates éloignées) — sans rien supprimer."""
    rank = _CONFIDENCE_RANK.get(item.price_confidence, 0)
    comparability = item.comparability if item.comparability is not None else 50
    try:
        d = _date.fromisoformat((item.sort_date() or "")[:10])
        recency = abs((d - _date.today()).days)
    except ValueError:
        recency = 10**6  # date absente/non parsable : reléguée en fin de groupe
    return (-rank, -comparability, recency)


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
            "reference_capacity": criteria.reference_capacity or similar.reference_capacity or None,
        }
    )
    genre_note = f" (style détecté : {similar.inferred_genre})" if similar.inferred_genre else ""
    cap_note = f", salles de ~{similar.reference_capacity} places" if similar.reference_capacity else ""
    log(f"Artistes retenus : {', '.join(artists)}{genre_note}{cap_note}")
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
    score_concerts(concerts, criteria)
    known = _from_base(criteria, concerts, log)
    if concerts:
        log(f"{len(concerts)} date(s) trouvée(s).{_profile_summary(concerts)}")
    elif not known:
        log(
            "0 date trouvée. Voir les lignes ci-dessus pour la cause (recherche "
            "vide vs structuration vide) ; sinon, essaie des critères moins "
            "restrictifs ou le profil 'equilibre'."
        )
        return concerts

    if with_prices and concerts:
        _extract_all(client, concerts, settings, log)
        _record(concerts, log)
    return _combine(concerts, known)


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
            "reference_capacity": criteria.reference_capacity or similar.reference_capacity or None,
            "reference_setting": criteria.reference_setting or _setting_or_none(similar.reference_setting),
            "reference_nights": criteria.reference_nights or similar.reference_nights or None,
            "reference_artists_per_night": (
                criteria.reference_artists_per_night or similar.reference_artists_per_night or None
            ),
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
    score_festivals(festivals, criteria)
    known = _from_base(criteria, festivals, log)
    if festivals:
        log(f"{len(festivals)} festival(s) trouvé(s).{_profile_summary(festivals)}")
    elif not known:
        log(
            "0 festival trouvé. Voir les lignes ci-dessus pour la cause ; sinon, "
            "essaie des critères moins restrictifs ou le profil 'equilibre'."
        )
        return festivals

    if with_prices and festivals:
        _extract_all(client, festivals, settings, log)
        _record(festivals, log)
    return _combine(festivals, known)


def _extract_all(
    client: anthropic.Anthropic,
    items: List[PricedItem],
    settings: config.RunSettings,
    log: Callable[[str], None],
) -> None:
    """Extrait les prix de chaque événement (in place), puis les réordonne.

    - `EXTRACTION_WORKERS` événements en parallèle, chacun avec son propre
      navigateur (l'API Playwright n'est pas partageable entre fils) ;
    - grilles déjà extraites récemment reprises de la mémoire (`price_cache`) ;
    - incohérences de chaque grille signalées (`check_grid`) ;
    - une erreur fatale (crédits, auth) arrête tout en conservant l'acquis ; une
      erreur ponctuelle est notée sur l'événement concerné.
    """
    pending: "queue.Queue[PricedItem]" = queue.Queue()
    for item in items:
        pending.put(item)
    stop = threading.Event()
    lock = threading.Lock()
    progress = {"n": 0, "done": 0, "cached": 0}
    total = len(items)

    def next_index() -> int:
        with lock:
            progress["n"] += 1
            return progress["n"]

    def worker() -> None:
        try:
            with _renderer_for(settings.fetch_strategy, log) as renderer:
                while not stop.is_set():
                    try:
                        item = pending.get_nowait()
                    except queue.Empty:
                        return
                    _extract_one(item, renderer, next_index())
        except Exception as exc:  # noqa: BLE001 — ex. navigateur impossible à lancer
            log(f"  ! fil d'extraction interrompu ({exc})")

    def _extract_one(item: PricedItem, renderer, i: int) -> None:
        key = price_cache.cache_key(item)
        cached = price_cache.load(key)
        if cached:
            price_cache.apply(item, cached)
            log(f"[{i}/{total}] Grille en mémoire : {item.short_label()}")
            with lock:
                progress["cached"] += 1
        else:
            log(f"[{i}/{total}] Extraction prix ({settings.fetch_strategy}) : {item.short_label()}")
            try:
                extract_prices(client, item, settings=settings, renderer=renderer)
                price_cache.store(key, item)
            except Exception as exc:  # noqa: BLE001
                if _is_fatal(exc):
                    stop.set()
                    log(f"Arrêt : erreur fatale ({exc}). Résultats déjà collectés conservés.")
                    return
                log(f"  ! événement ignoré ({exc})")
                item.notes = _append_note(item.notes, f"extraction échouée : {exc}")
        item.anomalies = check_grid(item)
        with lock:
            progress["done"] += 1

    n_workers = max(1, min(config.EXTRACTION_WORKERS, total))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if progress["cached"]:
        log(f"{progress['cached']} grille(s) reprise(s) de la mémoire (aucun coût).")
    if stop.is_set():
        log(f"{progress['done']}/{total} événement(s) traité(s) avant l'arrêt.")

    # Fait ressortir en priorité les événements avec un vrai prix exploitable
    # (rien n'est supprimé, juste réordonné).
    items.sort(key=_price_priority_key)


def _from_base(criteria, discovered: List[PricedItem], log: Callable[[str], None]) -> List[PricedItem]:
    """Événements déjà connus de la base de référence à ajouter au run.
    Une base indisponible ne doit jamais faire échouer un run."""
    try:
        return knowledge.matching_events(
            criteria, exclude_ids={price_cache.cache_key(i) for i in discovered}, log=log
        )
    except Exception as exc:  # noqa: BLE001
        log(f"  ! base de référence indisponible ({exc}) — run sans la mémoire.")
        return []


def _record(items: List[PricedItem], log: Callable[[str], None]) -> None:
    """Enregistre les événements tarifés du run dans la base de référence."""
    try:
        c = knowledge.record(items, source=f"run du {_date.today().isoformat()}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! enregistrement dans la base de référence impossible ({exc}).")
        return
    if c["ajouts"] or c["mises_a_jour"]:
        log(f"Base de référence : {c['ajouts']} ajout(s), {c['mises_a_jour']} mise(s) à jour.")


def _combine(run_items: List[PricedItem], known: List[PricedItem]) -> List[PricedItem]:
    """Événements du run + événements de la base qui ne font pas doublon
    (l'identité d'un festival peut changer à l'extraction : édition réalignée)."""
    seen = {price_cache.cache_key(i) for i in run_items}
    combined = run_items + [k for k in known if price_cache.cache_key(k) not in seen]
    combined.sort(key=_price_priority_key)
    return combined


def _profile_summary(items: List[PricedItem]) -> str:
    scored = [i.comparability for i in items if i.comparability is not None]
    if not scored:
        return ""
    out = sum(1 for s in scored if s < config.COMPARABILITY_MIN)
    return f" Comparabilité médiane : {sorted(scored)[len(scored) // 2]}/100" + (
        f", {out} hors profil (affichés, exclus des statistiques)." if out else "."
    )


def _setting_or_none(value: Optional[str]) -> Optional[str]:
    setting = coerce_setting(value)
    return None if setting.value == "inconnu" else setting.value


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
