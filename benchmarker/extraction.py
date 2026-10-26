"""Phase 2 — Extraction de la grille tarifaire d'un événement (concert ou festival).

Trois stratégies de récupération de la page billetterie :
  - "web_fetch"  : le LLM récupère la page via l'outil serveur web_fetch ;
  - "playwright" : on rend la page dans Chromium (JS exécuté) et on structure ;
  - "auto"       : web_fetch d'abord, fallback Playwright si la grille est faible.

Dans tous les cas, la structuration finale (texte → grille) passe par le même
appel LLM typé. C'est ici que la donnée « prix par catégorie » est la plus
fragile : on qualifie systématiquement ce qu'on trouve (niveau de confiance).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import anthropic

from . import config
from .llm import parse_structured, run_server_tool_loop
from .models import Festival, PriceConfidence, PriceExtraction, PricedItem, normalize_phase, normalize_url

_FETCH_SYSTEM = """Tu es un analyste billetterie. On te donne l'URL d'une page de vente
de billets pour un concert ou un festival. Récupère la page et restitue son contenu tarifaire.

Règles :
- Récupère le contenu de l'URL fournie avec l'outil web_fetch.
- Concert : liste chaque catégorie de place avec son prix (Carré Or, Fosse, Cat. 1/2/3, Placement libre…).
- Festival : liste chaque type de pass avec son prix (pass 1 jour / par jour nommé, 2 jours,
  3 jours, intégral, VIP…) ainsi que les options payantes (camping, parking…).
- Indique si les frais de location sont inclus quand l'information est disponible.
- N'invente aucun prix. Reste strictement factuel sur ce que la page affiche."""

_STRUCTURE_INSTRUCTION = """À partir du contenu de page ci-dessous, extrais la grille tarifaire
structurée : liste des catégories avec prix et devise, un niveau de confiance
(grille_complete / grille_partielle / prix_a_partir_de / aucun_prix), et une note
éventuelle (early bird, dynamic pricing, complet, etc.). N'invente aucun prix.

Pour un FESTIVAL : une catégorie par type de pass, en gardant explicitement la
durée dans le nom (ex. « Pass 1 jour », « Pass Samedi », « Pass 2 jours »,
« Pass 3 jours », « Pass intégral », « Pass VIP 3 jours »). Les options vendues
à part (camping, parking, navette…) sont des catégories séparées dont le nom
commence par « Option : ».

Tarifs à NE PAS mélanger au plein tarif : garde-les, mais nomme-les explicitement
(« Tarif réduit », « Enfant », « Tarif habitants », « PMR », « Groupe »…).

Édition et phase (essentiel pour comparer à périmètre égal) :
- edition = l'année de l'édition à laquelle s'applique la grille AFFICHÉE (une page
  peut déjà montrer l'édition suivante) ; chaîne vide si inconnue ;
- phase = phase de prix de la grille retenue : « early bird » (prévente, super early,
  quotas limités), « normal », « dernière minute » ou « sur place ». Si plusieurs
  phases sont affichées, retiens le tarif « normal » s'il existe, sinon celui en
  vente, et mentionne les autres en note."""

_REFINE_URL_SYSTEM = """Tu cherches l'URL DIRECTE de la fiche de vente de billets pour un
événement précis — concert, ou édition précise d'un festival (pas la page d'accueil d'une
billetterie, pas un portail d'événements, pas une page de recherche ou de liste). Pour un
festival, la page billetterie / tarifs de son site officiel convient parfaitement.
Jamais une page de streaming, de réseau social ou d'agenda musical (Shazam, Songkick,
Bandsintown, Spotify, YouTube…) : elles n'affichent aucun prix.

Réponds STRICTEMENT par :
- l'URL trouvée (et rien d'autre), si tu es raisonnablement sûr qu'elle correspond
  précisément à cet événement ;
- ou le mot AUCUNE si tu ne trouves pas de fiche dédiée à cet événement précis."""

_REFINE_ALTERNATE_SYSTEM = """Tu cherches une SOURCE ALTERNATIVE pour connaître le prix des
billets d'un événement précis (concert ou festival), car la billetterie bloque l'accès
automatisé.

Privilégie, dans cet ordre : pour un festival, la page billetterie / tarifs de son
site officiel (les grilles de pass y sont souvent en clair) ; un agrégateur de
billetterie (ex. jds.fr, infoconcert.com, wegow.com) ; un article de presse locale
annonçant les tarifs ; le site de l'organisateur/de la salle — PAS un doublon du
domaine à éviter.

Réponds STRICTEMENT par :
- l'URL trouvée (et rien d'autre), si elle affiche un prix ou une fourchette de
  prix pour cet événement précis ;
- ou le mot AUCUNE si tu ne trouves rien de mieux."""


def extract_prices(
    client: anthropic.Anthropic,
    item: PricedItem,
    *,
    settings: "config.RunSettings | None" = None,
    renderer=None,
) -> PricedItem:
    """Enrichit un `Concert` ou un `Festival` avec sa grille tarifaire (in place et retourné).

    `settings` : réglages du run (modèles, stratégie, budgets). Défaut : profil courant.
    `renderer` : instance `PageRenderer` réutilisable (optionnel, pour Playwright).
    """
    settings = settings or config.resolve_settings()
    strategy = settings.fetch_strategy
    item.priced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    item.source_url = normalize_url(item.source_url)

    # URL absente, visiblement générique (page d'accueil de billetterie) ou sur
    # un site sans prix (streaming, Shazam…) : recherche ciblée de la fiche.
    if _needs_refine(item.source_url, item):
        refined = _refine_source_url(client, item, settings)
        if refined:
            item.notes = _append_note(item.notes, f"URL affinée : {refined}")
            item.source_url = refined
        else:
            item.price_confidence = PriceConfidence.NONE
            item.notes = _append_note(
                item.notes,
                "Pas de fiche événement directe trouvée (URL générique ou absente).",
            )
            return item

    # Domaine connu pour bloquer l'accès automatisé (cf. HIGH_FRICTION_DOMAINS) :
    # on cherche d'abord une source alternative (agrégateur/presse) plutôt que
    # de perdre du temps sur une URL qui échouera très probablement. Si rien
    # de mieux n'est trouvé, on continue quand même avec l'URL d'origine (le
    # blocage n'est pas garanti à 100 %, mieux vaut tenter que renoncer).
    # Vérifié après le bloc précédent (et pas en `elif`) pour couvrir aussi le
    # cas d'une URL manquante affinée vers un domaine lui-même à forte friction.
    if item.source_url and _is_high_friction(item.source_url):
        alt = _refine_source_url(client, item, settings, mode="alternate")
        if alt:
            item.notes = _append_note(
                item.notes,
                f"Source alternative privilégiée (domaine à forte friction anti-bot) : {alt}",
            )
            item.source_url = alt

    result: Optional[PriceExtraction] = None
    used: Optional[str] = None

    # 1) Playwright d'abord (rendu local GRATUIT) pour "playwright" et "auto".
    #    Seule la structuration coûte, et une seule fois.
    if strategy in ("playwright", "auto"):
        rendered, err, links = _gather_via_playwright(item.source_url, renderer)
        if rendered:
            result = _structure(client, rendered, settings.structure_model)
            used = "playwright"
        else:
            # Notée même en mode "auto" (avant : silencieuse) pour pouvoir
            # diagnostiquer un souci de rendu sans devoir forcer la stratégie.
            item.notes = _append_note(item.notes, f"playwright : {err}")

        # Page vitrine (salle, organisateur) sans grille : les prix sont derrière
        # le lien « Réserver ». On suit le premier lien de réservation exploitable
        # (un seul, coût maîtrisé : rendu gratuit + une structuration).
        if not _is_sufficient(result):
            link = next(
                (l for l in links if not _is_high_friction(l) and not _is_non_ticketing(l)),
                None,
            )
            if link:
                text2, err2, _ = _gather_via_playwright(link, renderer)
                if text2:
                    candidate = _structure(client, text2, settings.structure_model)
                    if _is_better(candidate, result):
                        result, used = candidate, "playwright (lien de réservation)"
                        item.notes = _append_note(item.notes, f"lien de réservation suivi : {link}")
                        item.source_url = link
                else:
                    item.notes = _append_note(item.notes, f"lien de réservation {link} : {err2}")

    # 2) web_fetch (LLM, coûteux) : stratégie dédiée, ou fallback "auto"
    #    UNIQUEMENT si le rendu gratuit n'a pas donné de grille suffisante.
    need_web_fetch = strategy == "web_fetch" or (
        strategy == "auto" and not _is_sufficient(result)
    )
    if need_web_fetch:
        page_text, err = _gather_via_web_fetch(client, item, settings)
        if page_text:
            candidate = _structure(client, page_text, settings.structure_model)
            if _is_better(candidate, result):
                result, used = candidate, "web_fetch"
        else:
            # Note systématiquement, même si Playwright avait déjà produit un
            # résultat (insuffisant) — sinon un double échec (playwright +
            # web_fetch) reste invisible dans les notes de l'événement.
            item.notes = _append_note(item.notes, f"web_fetch : {err}")

    # 3) Application du résultat
    if result is None:
        item.price_confidence = PriceConfidence.NONE
        return item

    item.prices = result.prices
    item.price_confidence = result.price_confidence
    item.price_phase = normalize_phase(result.phase)
    _align_edition(item, result.edition)
    if used:
        item.notes = _append_note(item.notes, f"source prix : {used}")
    if result.notes:
        item.notes = _append_note(item.notes, result.notes)
    return item


def _align_edition(item: PricedItem, priced_edition: str) -> None:
    """Rattache un festival à l'édition dont la grille a réellement été lue.

    Constaté : une édition « 2026 » tarifée avec la grille 2027 déjà en ligne.
    Les prix font foi : on aligne l'édition, et on écarte les dates si elles
    appartiennent à l'autre édition (en les gardant en note).
    """
    if not isinstance(item, Festival):
        return
    m = re.search(r"(19|20)\d{2}", priced_edition or "")
    if not m or (item.edition and m.group(0) in item.edition):
        return
    new = m.group(0)
    item.notes = _append_note(item.notes, f"grille de l'édition {new} (édition trouvée à la découverte : {item.edition or '?'})")
    if item.date_start and not item.date_start.startswith(new):
        item.notes = _append_note(item.notes, f"dates de l'autre édition : {item.date_start} → {item.date_end or '?'}")
        item.date_start = item.date_end = None
    item.edition = new


# --- qualité de l'URL --------------------------------------------------------
def _looks_like_generic_url(url: Optional[str]) -> bool:
    """Détecte une URL de type page d'accueil/portail plutôt qu'une fiche événement.

    Heuristique volontairement simple mais indépendante du domaine : une page
    d'accueil n'a pas de chemin significatif (vide ou juste `/`). Une fiche
    événement a presque toujours un chemin (slug, identifiant, etc.). Ça évite
    de dépendre d'une liste de domaines à maintenir.
    """
    if not url:
        return True
    return urlsplit(url).path in ("", "/")


def _is_high_friction(url: Optional[str]) -> bool:
    """Détecte un domaine connu pour bloquer souvent l'accès automatisé."""
    if not url:
        return False
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in config.HIGH_FRICTION_DOMAINS)


def _domain_in(url: Optional[str], domains) -> bool:
    if not url:
        return False
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _needs_refine(url: Optional[str], item: PricedItem) -> bool:
    """URL inexploitable telle quelle : absente, sur un site sans prix, ou page
    d'accueil générique — sauf le site officiel d'un festival, porte d'entrée
    valable (son lien « Billetterie » est suivi à l'extraction)."""
    if not url or _is_non_ticketing(url):
        return True
    if _looks_like_generic_url(url):
        official_site = isinstance(item, Festival) and not _domain_in(url, config.TICKETING_PORTAL_DOMAINS)
        return not official_site
    return False


def _is_non_ticketing(url: Optional[str]) -> bool:
    """Domaine qui n'affiche jamais de prix de billets (streaming, Shazam…)."""
    if not url:
        return False
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in config.NON_TICKETING_DOMAINS)


def _refine_source_url(
    client: anthropic.Anthropic,
    item: PricedItem,
    settings: "config.RunSettings",
    *,
    mode: str = "direct",
) -> Optional[str]:
    """Recherche ciblée d'une URL de billetterie (un seul appel, peu coûteux).

    `mode="direct"` : cas URL manquante/générique, cherche la fiche directe.
    `mode="alternate"` : cas domaine à forte friction anti-bot, cherche une
    source de repli (agrégateur/presse) plutôt qu'un doublon du même domaine.
    """
    is_alternate = mode == "alternate"
    query = (
        ("Trouve une source alternative pour le prix des billets de cet événement précis "
         f"(la billetterie {urlsplit(item.source_url).netloc if item.source_url else '?'} "
         "bloque l'accès automatisé) :\n"
         if is_alternate else
         "Trouve l'URL directe de vente de billets pour cet événement précis :\n")
        + item.search_label()
    )
    web_search = dict(config.WEB_SEARCH_TOOL)
    web_search["max_uses"] = config.URL_REFINE_MAX_USES
    try:
        text = run_server_tool_loop(
            client,
            system=_REFINE_ALTERNATE_SYSTEM if is_alternate else _REFINE_URL_SYSTEM,
            user=query,
            tools=[web_search],
            model=settings.discovery_model,
            # "medium" (et non "low") : avec un budget de recherche élargi
            # (URL_REFINE_MAX_USES), autant que le raisonnement en tire parti
            # plutôt que de s'arrêter sur la première page trouvée.
            effort="medium",
            max_tokens=300,
        )
    except Exception:
        return None

    text = (text or "").strip()
    if not text or text.upper().startswith("AUCUNE"):
        return None
    match = re.search(r"https?://\S+", text)
    if not match:
        return None
    url = normalize_url(match.group(0).rstrip(".,)\"'"))
    return None if _needs_refine(url, item) else url


# --- collecte de la page ----------------------------------------------------
def _gather_via_web_fetch(
    client: anthropic.Anthropic, item: PricedItem, settings: "config.RunSettings"
) -> tuple[Optional[str], Optional[str]]:
    """Récupère la page via l'outil serveur web_fetch. Renvoie (texte, erreur)."""
    user = (
        f"Voici la page billetterie pour : {item.search_label()}\n"
        f"URL : {item.source_url}\n\n"
        "Récupère la page et donne-moi la grille tarifaire complète par catégorie "
        "(ou par type de pass pour un festival)."
    )
    web_fetch = dict(config.WEB_FETCH_TOOL)
    web_fetch["max_uses"] = settings.max_fetch_uses
    web_fetch["max_content_tokens"] = settings.fetch_max_content_tokens
    try:
        text = run_server_tool_loop(
            client,
            system=_FETCH_SYSTEM,
            user=user,
            tools=[web_fetch],
            model=settings.extraction_model,
            effort=settings.extraction_effort,
        )
        return (text or None), (None if text else "page vide ou inaccessible")
    except Exception as exc:  # réseau / refus / autre
        return None, str(exc)


def _gather_via_playwright(
    url: str, renderer
) -> tuple[Optional[str], Optional[str], list[str]]:
    """Rend la page avec Chromium. Renvoie (texte, erreur, liens de réservation).

    Si un `renderer` (PageRenderer) est fourni, il est réutilisé ; sinon on
    ouvre un navigateur dédié le temps de la page (sans collecte de liens).
    """
    try:
        if renderer is not None:
            text, links = renderer.render_with_links(url)
        else:
            from .browser import render_page

            text, links = render_page(url), []
        return (text or None), (None if text else "rendu vide"), links
    except Exception as exc:
        return None, str(exc), []


# --- structuration ----------------------------------------------------------
def _structure(
    client: anthropic.Anthropic, page_text: str, model: str | None = None
) -> Optional[PriceExtraction]:
    """Structure le texte de page en grille tarifaire typée."""
    return parse_structured(
        client,
        schema=PriceExtraction,
        instruction=_STRUCTURE_INSTRUCTION,
        material=page_text,
        model=model,
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
