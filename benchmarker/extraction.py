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
    item.priced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    item.source_url = normalize_url(item.source_url)

    # URL absente, visiblement générique (page d'accueil de billetterie) ou sur
    # un site sans prix (streaming, Shazam…) : recherche ciblée de la fiche.
    refined_once = False  # une recherche ciblée a déjà eu lieu (sauvetage inutile pour un concert)
    if _needs_refine(item.source_url, item):
        refined_once = True
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

    # Festival repéré sur un agrégateur (fiche descriptive, souvent tronquée ou
    # fermée à web_fetch) : le site officiel affiche la grille de pass. Si on
    # ne le trouve pas, on garde l'agrégateur.
    if isinstance(item, Festival) and _domain_in(item.source_url, config.FESTIVAL_AGGREGATOR_DOMAINS):
        refined_once = True
        official = _refine_source_url(client, item, settings, mode="official")
        if official:
            item.notes = _append_note(item.notes, f"site officiel privilégié à l'agrégateur : {official}")
            item.source_url = official

    # Domaine connu pour bloquer l'accès automatisé (cf. HIGH_FRICTION_DOMAINS) :
    # on cherche d'abord une source alternative (agrégateur/presse) plutôt que
    # de perdre du temps sur une URL qui échouera très probablement. Si rien
    # de mieux n'est trouvé, on continue quand même avec l'URL d'origine (le
    # blocage n'est pas garanti à 100 %, mieux vaut tenter que renoncer).
    # Vérifié après le bloc précédent (et pas en `elif`) pour couvrir aussi le
    # cas d'une URL manquante affinée vers un domaine lui-même à forte friction.
    if item.source_url and _is_high_friction(item.source_url):
        refined_once = True
        alt = _refine_source_url(client, item, settings, mode="alternate")
        if alt:
            item.notes = _append_note(
                item.notes,
                f"Source alternative privilégiée (domaine à forte friction anti-bot) : {alt}",
            )
            item.source_url = alt

    reading = _Reading(client, item, settings, renderer, refined=refined_once)
    reading.read(item.source_url)
    if reading.dead_domain:
        return item

    # 2 ter) Sauvetage : toujours rien d'exploitable. Une recherche ciblée d'une
    # AUTRE page (site officiel, page tarifs dédiée…) ; pour un festival dont les
    # tarifs de l'édition visée ne sont pas encore publiés, la grille de
    # l'édition précédente (constaté en octobre : éditions 2027 sans tarifs,
    # alors que la grille 2026 restait en ligne). Une seule fois.
    if not _is_sufficient(reading.result) and (isinstance(item, Festival) or not reading.refined):
        previous = _previous_edition(item, reading.texts)
        alt = _refine_source_url(client, item, settings, mode="rescue", tried=reading.tried, previous=previous)
        if alt:
            what = f"grille de l'édition {previous} recherchée (tarifs {item.edition} pas encore publiés)" if previous \
                else "autre page recherchée"
            item.notes = _append_note(item.notes, f"{what} : {alt}")
            reading.read(alt, rescue=True)
    result, used, last_text = reading.result, reading.used, reading.last_text
    if reading.url_used:
        item.source_url = reading.url_used

    # 3) Application du résultat
    # Échec sans explication du modèle : on note le début de la page lue (mur
    # anti-bot, page vide, billetterie fermée…) pour ne plus avoir d'échec muet.
    if last_text and (result is None or (result.price_confidence == PriceConfidence.NONE and not result.notes)):
        excerpt = " ".join(last_text.split())[:180].rstrip("…. ")
        item.notes = _append_note(item.notes, f"aucune grille lue — début de la page : « {excerpt}… »")
    if result is None:
        item.price_confidence = PriceConfidence.NONE
        return item

    item.prices = result.prices
    item.price_confidence = result.price_confidence
    item.price_phase = normalize_phase(result.phase) if result.prices else None
    _align_edition(item, result.edition)
    if used:
        item.notes = _append_note(item.notes, f"source prix : {used}")
    if result.notes:
        item.notes = _append_note(item.notes, result.notes)
    return item


class _Reading:
    """Lecture d'une ou plusieurs pages pour un même événement.

    Pour chaque URL : rendu Playwright (gratuit) puis, si la grille manque, le
    premier lien « Réserver / Billetterie » ; web_fetch (payant) en repli ;
    seconde chance de structuration avec le modèle plus solide quand une page
    lue contient visiblement des prix. Garde le meilleur résultat, la page
    dont il vient et tout ce qui a été lu (pour expliquer un échec).
    """

    def __init__(self, client, item: PricedItem, settings: "config.RunSettings", renderer, *, refined: bool = False):
        self.client, self.item, self.settings, self.renderer = client, item, settings, renderer
        self.result: Optional[PriceExtraction] = None
        self.used: Optional[str] = None
        self.url_used: Optional[str] = None
        self.last_text: Optional[str] = None
        self.texts: list[tuple[str, str]] = []  # (source, texte)
        self.tried: list[str] = []
        self.refined = refined
        self.dead_domain = False
        self._reinforced: set[int] = set()

    def _offer(self, text: str, source: str, url: str) -> bool:
        self.last_text = text
        self.texts.append((source, text))
        return self._consider(_structure(self.client, text, self.settings.structure_model), source, url)

    def _consider(self, candidate: Optional[PriceExtraction], source: str, url: str) -> bool:
        if _is_better(candidate, self.result):
            self.result, self.used, self.url_used = candidate, source, url
            return True
        return False

    def read(self, url: str, *, rescue: bool = False) -> None:
        item, settings = self.item, self.settings
        strategy = settings.fetch_strategy
        tag = " (sauvetage)" if rescue else ""
        self.tried.append(url)
        n_texts = len(self.texts)

        # 1) Playwright d'abord (rendu local GRATUIT) pour "playwright" et "auto".
        if strategy in ("playwright", "auto"):
            rendered, err, links = _gather_via_playwright(url, self.renderer)
            # Domaine inexistant (URL inventée à la découverte, ex. « cabaret-vert.com ») :
            # une recherche ciblée plutôt qu'un web_fetch payant sur une adresse morte.
            if not rendered and _is_dead_domain(err) and not rescue:
                dead = urlsplit(url).netloc
                self.refined = True
                refined = _refine_source_url(self.client, item, settings)
                if not refined or urlsplit(refined).netloc == dead:
                    item.notes = _append_note(item.notes, f"domaine inexistant ({dead}) et aucune autre URL trouvée")
                    item.price_confidence = PriceConfidence.NONE
                    self.dead_domain = True
                    return
                item.notes = _append_note(item.notes, f"domaine inexistant ({dead}), URL retrouvée : {refined}")
                url = item.source_url = refined
                self.tried.append(url)
                rendered, err, links = _gather_via_playwright(url, self.renderer)
            if rendered:
                self._offer(rendered, "playwright" + tag, url)
            else:
                item.notes = _append_note(item.notes, f"playwright{tag} : {err}")

            # Page vitrine (salle, organisateur) sans grille : les prix sont derrière
            # le lien « Réserver ». On suit le premier lien de réservation exploitable
            # (un seul, coût maîtrisé : rendu gratuit + une structuration).
            if not _is_sufficient(self.result):
                link = next(
                    (l for l in links if not _same_url(l, self.tried)
                     and not _is_high_friction(l) and not _is_non_ticketing(l)),
                    None,
                )
                if link:
                    self.tried.append(link)
                    text2, err2, _ = _gather_via_playwright(link, self.renderer)
                    if text2:
                        if self._offer(text2, "playwright (lien de réservation)" + tag, link):
                            item.notes = _append_note(item.notes, f"lien de réservation suivi : {link}")
                    else:
                        item.notes = _append_note(item.notes, f"lien de réservation {link} : {err2}")

        # 2) web_fetch (LLM, coûteux) : stratégie dédiée, ou fallback "auto"
        #    UNIQUEMENT si le rendu gratuit n'a pas donné de grille suffisante.
        if strategy == "web_fetch" or (strategy == "auto" and not _is_sufficient(self.result)):
            page_text, err = _gather_via_web_fetch(self.client, item, settings, url)
            if page_text:
                self._offer(page_text, "web_fetch" + tag, url)
            else:
                item.notes = _append_note(item.notes, f"web_fetch{tag} : {err}")

        # 2 bis) Seconde chance : une page lue contient visiblement des prix, mais
        # la structuration (modèle économique) n'en a rien tiré — constaté : grille
        # complète du Cabaret Vert 2027 lue par le web_fetch puis perdue. On
        # restructure une fois avec le modèle plus solide (coût limité à ces cas).
        if not _is_sufficient(self.result) and settings.extraction_model != settings.structure_model:
            fresh = [(i, st) for i, st in enumerate(self.texts) if i >= n_texts and i not in self._reinforced]
            best = max(fresh, key=lambda ist: _price_evidence(ist[1][1]), default=None)
            if best and _price_evidence(best[1][1]) >= 2:
                self._reinforced.add(best[0])
                source, text = best[1]
                self._consider(_structure(self.client, text, settings.extraction_model),
                               f"{source} (structuration renforcée)", self.url_used or url)


def _same_url(url: str, seen: list[str]) -> bool:
    key = lambda u: (u or "").split("#")[0].rstrip("/").lower()
    return key(url) in {key(u) for u in seen}


_UNPUBLISHED_RE = re.compile(
    r"pas encore (?:été )?(?:publi|annonc|communiqu|disponib|en vente|ouverte?)"
    r"|bient[ôo]t (?:disponible|en vente)|(?:tarifs?|billetterie|prix) (?:à venir|prochainement)"
    r"|not (?:yet )?(?:on sale|available|announced)|coming soon",
    re.I,
)


def _previous_edition(item: PricedItem, texts: list[tuple[str, str]]) -> Optional[str]:
    """Édition précédente à viser quand les pages lues disent que les tarifs de
    l'édition visée ne sont pas encore publiés (et qu'elle n'est pas trop
    ancienne : on ne descend jamais sous l'année dernière)."""
    if not isinstance(item, Festival):
        return None
    m = re.search(r"(?:19|20)\d{2}", item.edition or "")
    if not m or not any(_UNPUBLISHED_RE.search(t) for _, t in texts):
        return None
    previous = int(m.group(0)) - 1
    return str(previous) if previous >= datetime.now(timezone.utc).year - 1 else None


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


def _is_dead_domain(err: Optional[str]) -> bool:
    return bool(err) and any(k in err for k in ("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED"))


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
    tried: Optional[list[str]] = None,
    previous: Optional[str] = None,
) -> Optional[str]:
    """Recherche ciblée d'une URL de billetterie (un seul appel, peu coûteux).

    `mode="direct"` : cas URL manquante/générique, cherche la fiche directe.
    `mode="alternate"` : cas domaine à forte friction anti-bot, cherche une
    source de repli (agrégateur/presse) plutôt qu'un doublon du même domaine.
    `mode="official"` : festival trouvé sur un agrégateur, cherche son site officiel.
    `mode="rescue"` : les pages `tried` n'ont rien donné, cherche une autre page —
    celle de l'édition `previous` si les tarifs de l'édition visée ne sont pas publiés.
    """
    is_fest = isinstance(item, Festival)
    tried = tried or []
    if mode == "alternate":
        query = ("Trouve une source alternative pour le prix des billets de cet événement précis "
                 f"(la billetterie {urlsplit(item.source_url).netloc if item.source_url else '?'} "
                 "bloque l'accès automatisé) :\n")
    elif mode == "official":
        query = ("Trouve la page billetterie / tarifs du SITE OFFICIEL de ce festival (pas un agrégateur ni "
                 f"un agenda comme {', '.join(config.FESTIVAL_AGGREGATOR_DOMAINS[:4])}) :\n")
    elif mode == "rescue":
        query = ("Les pages suivantes n'ont pas donné de grille tarifaire exploitable (illisibles, tronquées "
                 f"ou sans prix) : {', '.join(tried) or '?'}.\n"
                 "Trouve une AUTRE page affichant les prix de cet événement"
                 + (" (page billetterie / tarifs du site officiel de préférence, sinon presse ou agrégateur "
                    "annonçant les prix des pass)" if is_fest else "") + " :\n")
    else:
        query = "Trouve l'URL directe de vente de billets pour cet événement précis :\n"
    query += item.search_label()
    if is_fest and previous:
        query += (f"\nLes tarifs de l'édition {item.edition} ne sont pas encore publiés : cherche la page qui "
                  f"affiche les prix des pass de l'édition {previous} (billetterie officielle encore en ligne, "
                  f"page tarifs, article de presse ou agrégateur annonçant les tarifs {previous}).")
    elif is_fest:
        query += ("\nSi cette édition est passée, la page billetterie / tarifs de l'édition en cours ou à venir "
                  "convient ; si ses tarifs ne sont pas encore publiés, une page affichant ceux de l'édition "
                  "précédente convient (les prix seront rattachés à la bonne édition).")
    web_search = dict(config.WEB_SEARCH_TOOL)
    web_search["max_uses"] = config.URL_REFINE_MAX_USES
    try:
        text = run_server_tool_loop(
            client,
            system=_REFINE_ALTERNATE_SYSTEM if mode in ("alternate", "rescue") else _REFINE_URL_SYSTEM,
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
    if _needs_refine(url, item) or _same_url(url, tried):
        return None
    if mode == "official" and _domain_in(url, config.FESTIVAL_AGGREGATOR_DOMAINS):
        return None
    return url


# --- collecte de la page ----------------------------------------------------
def _gather_via_web_fetch(
    client: anthropic.Anthropic, item: PricedItem, settings: "config.RunSettings", url: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """Récupère la page via l'outil serveur web_fetch. Renvoie (texte, erreur)."""
    user = (
        f"Voici la page billetterie pour : {item.search_label()}\n"
        f"URL : {url or item.source_url}\n\n"
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
        if not text:
            return None, "page vide ou inaccessible"
        # Le modèle répond parfois par un message d'échec (« url_not_accessible »,
        # « je n'ai pas pu… ») : c'est un échec, pas un contenu de page.
        if _FETCH_FAILURE_RE.search(text) and _price_evidence(text) < 2:
            return None, f"page inaccessible ({' '.join(text.split())[:90]}…)"
        return text, None
    except Exception as exc:  # réseau / refus / autre
        return None, str(exc)


# Réponses du modèle qui décrivent un échec ou un travail inachevé plutôt
# qu'un contenu de page (constaté : « Il y a un lien billetterie séparé. Je
# vais le récupérer… » pris pour la page de Rio Loco ; page jds.fr « tronquée
# avant la section tarifaire »).
_FETCH_FAILURE_RE = re.compile(
    r"url_not_accessible|url_not_allowed|n'ai (?:donc )?pas pu|impossible d'acc[ée]der|pas pu (?:r[ée]cup[ée]rer|charger)"
    r"|n'est pas autoris[ée]|ne contient (?:pas|aucun) (?:de )?(?:contenu|grille|information|prix)"
    r"|s'arr[êe]te avant|tronqu[ée]|je vais (?:le |la |les |maintenant |donc )?(?:r[ée]cup[ée]rer|consulter|chercher|essayer|v[ée]rifier)"
    r"|v[ée]rifions|unable to (?:fetch|access)|could not (?:fetch|access)",
    re.I,
)
_PRICE_RE = re.compile(r"\d+(?:[.,]\d{1,2})?\s?(?:€|eur\b|euros?\b)", re.I)


def _price_evidence(text: str) -> int:
    """Nombre de montants en euros dans un texte (indice qu'une grille y figure)."""
    return len(_PRICE_RE.findall(text or ""))


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
