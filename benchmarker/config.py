"""Configuration centrale du benchmarker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Modèles LLM (par rôle, pour maîtriser le coût) -------------------------
# Chaque étape utilise le modèle le moins cher qui fait bien le travail :
#   - découverte / extraction : boucles avec outils web (recherche/fetch) →
#     nécessitent un modèle compatible outils web récents → Sonnet 5 (bon marché
#     et suffisant) plutôt qu'Opus.
#   - structuration (texte → JSON) : tâche mécanique → Haiku (le moins cher).
# Tous surchargeables par variable d'environnement.
MODEL = os.environ.get("BENCHMARKER_MODEL", "claude-sonnet-5")
DISCOVERY_MODEL = os.environ.get("BENCHMARKER_DISCOVERY_MODEL", "claude-sonnet-5")
EXTRACTION_MODEL = os.environ.get("BENCHMARKER_EXTRACTION_MODEL", "claude-sonnet-5")
STRUCTURE_MODEL = os.environ.get("BENCHMARKER_STRUCTURE_MODEL", "claude-haiku-4-5")

# Effort de raisonnement (low | medium | high | xhigh | max).
# Volontairement bas : la découverte n'a pas besoin d'un raisonnement profond,
# et la structuration n'utilise ni thinking ni effort.
DISCOVERY_EFFORT = os.environ.get("BENCHMARKER_DISCOVERY_EFFORT", "medium")
EXTRACTION_EFFORT = os.environ.get("BENCHMARKER_EXTRACTION_EFFORT", "low")

# --- Outils serveur ---------------------------------------------------------
# Versions à jour avec filtrage dynamique (nécessitent Opus 4.8/4.7/4.6, Sonnet).
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

# Nombre max d'appels d'outils par étape (garde-fou coût/latence).
# 6 recherches en découverte : la vérification d'URL ne s'y fait plus (elle
# est déléguée à URL_REFINE_MAX_USES ci-dessous), donc le budget de découverte
# sert uniquement à couvrir large — 6 suffit à un léger matelas de sécurité
# pour une recherche par style générique.
MAX_SEARCH_USES = int(os.environ.get("BENCHMARKER_MAX_SEARCH_USES", "6"))
MAX_FETCH_USES = int(os.environ.get("BENCHMARKER_MAX_FETCH_USES", "2"))

# Avec un artiste (ou festival) de référence, la découverte cible une LISTE
# précise (répartition explicite du budget de recherche entre les cibles, cf.
# discovery.py) : un budget fixe de 6 devient vite insuffisant (ex. 7
# artistes = moins d'une recherche chacun), ce qui a provoqué en conditions
# réelles un épuisement précoce du budget suivi d'un abandon prématuré du
# modèle. Le budget de découverte croît donc avec le nombre de cibles,
# jusqu'à ce plafond (coût plus élevé, accepté explicitement).
DISCOVERY_SEARCH_USES_PER_TARGET = int(
    os.environ.get("BENCHMARKER_DISCOVERY_SEARCH_USES_PER_TARGET", "2")
)
MAX_SEARCH_USES_CAP = int(os.environ.get("BENCHMARKER_MAX_SEARCH_USES_CAP", "20"))

# Timeout explicite par appel LLM (secondes). Sans ça, le SDK utilise son
# défaut (10 min) : un appel qui traîne (latence réseau, modèle qui rame)
# bloque silencieusement le job pendant tout ce temps sans le moindre signal
# dans l'UI — observé en conditions réelles sur l'étape de résolution
# d'artistes comparables. Un échec net et rapide (job en erreur, message
# visible) vaut mieux qu'un blocage muet.
LLM_REQUEST_TIMEOUT_S = float(os.environ.get("BENCHMARKER_LLM_REQUEST_TIMEOUT_S", "180"))

# Plafond de tokens de contenu ramené par web_fetch dans le contexte (coût).
FETCH_MAX_CONTENT_TOKENS = int(
    os.environ.get("BENCHMARKER_FETCH_MAX_CONTENT_TOKENS", "6000")
)

# Recherche ciblée max pour retrouver une URL directe de billetterie quand la
# découverte n'a renvoyé qu'une page générique (page d'accueil/portail) ou rien
# (et, séparément, pour chercher une source alternative sur un domaine à forte
# friction — cf. HIGH_FRICTION_DOMAINS). Relevé de 2 à 4 : sur un run réel,
# c'était la première cause d'échec (aucune URL trouvée, ni en découverte ni
# en affinage) — un budget de recherche plus large par date concernée
# améliore le taux de succès au prix d'un coût légèrement plus élevé, un
# compromis accepté explicitement (option "plus cher" plutôt que qualité).
URL_REFINE_MAX_USES = int(os.environ.get("BENCHMARKER_URL_REFINE_MAX_USES", "4"))

# Résolution d'artistes comparables à partir d'un artiste de référence (un seul
# appel par run, uniquement si l'utilisateur fournit un artiste de référence).
SIMILAR_ARTISTS_MAX_USES = int(os.environ.get("BENCHMARKER_SIMILAR_ARTISTS_MAX_USES", "4"))
SIMILAR_ARTISTS_COUNT = int(os.environ.get("BENCHMARKER_SIMILAR_ARTISTS_COUNT", "6"))

# Idem pour le mode Festival (festival de référence -> festivals comparables).
SIMILAR_FESTIVALS_MAX_USES = int(os.environ.get("BENCHMARKER_SIMILAR_FESTIVALS_MAX_USES", "4"))
SIMILAR_FESTIVALS_COUNT = int(os.environ.get("BENCHMARKER_SIMILAR_FESTIVALS_COUNT", "6"))

# Nombre max de continuations pause_turn dans une boucle d'outils serveur.
MAX_CONTINUATIONS = 6

# --- Sources billetterie prioritaires (France d'abord) ----------------------
# Sert d'indice au modèle : ce ne sont pas des restrictions strictes.
PREFERRED_SOURCES_FR = [
    "ticketmaster.fr",
    "fnacspectacles.com",
    "seetickets.com",
    "dice.fm",
    "shotgun.live",
    "billetreduc.com",
    "digitick.com",
    "weezevent.com",
]

# Sites qui n'affichent jamais de prix de billets (streaming, réseaux sociaux,
# agendas de type Shazam/Songkick…) : une URL sur ces domaines est traitée
# comme une URL absente (nouvelle recherche ciblée) plutôt que d'être rendue —
# constaté en conditions réelles : une page Shazam retenue comme "billetterie".
NON_TICKETING_DOMAINS = [
    "shazam.com", "songkick.com", "bandsintown.com", "spotify.com", "deezer.com",
    "music.apple.com", "youtube.com", "youtu.be", "soundcloud.com", "genius.com",
    "wikipedia.org", "instagram.com", "facebook.com", "x.com", "twitter.com",
    "tiktok.com", "linkedin.com",
]

# Billetteries couramment utilisées par les festivals (indice pour la
# découverte ; le site officiel du festival reste la source n°1 des grilles
# de pass, souvent publiées en clair sur une page « billetterie / tarifs »).
FESTIVAL_TICKETING_SOURCES = [
    "site officiel du festival",
    "weezevent.com",
    "shotgun.live",
    "billetweb.fr",
    "seetickets.com",
    "ticketmaster.fr",
    "dice.fm",
    "festicket.com",
]

# Sources secondaires (agrégateurs / presse) qui affichent souvent un prix
# indicatif en texte simple, sans grille détaillée par catégorie mais sans
# les protections anti-bot des grosses billetteries — utiles en repli quand
# la billetterie officielle bloque l'accès automatisé (cf. HIGH_FRICTION_DOMAINS).
SECONDARY_PRICE_SOURCES_FR = [
    "jds.fr",
    "infoconcert.com",
    "sortiraparis.com",
    "wegow.com",
]

# Agrégateurs / agendas de festivals : fiche descriptive, rarement la grille
# de pass complète, souvent tronquée ou fermée à l'outil web_fetch (constaté :
# touslesfestivals.com « url_not_allowed », jds.fr coupé avant les tarifs).
# Pour un festival, le site officiel est cherché en priorité.
FESTIVAL_AGGREGATOR_DOMAINS = [
    "touslesfestivals.com",
    "jds.fr",
    "infoconcert.com",
    "sortiraparis.com",
    "festivalfinder.eu",
    "lesfestivals.fr",
    "francefestivals.com",
    "evous.fr",
    "wegow.com",
]

# Domaines de billetterie connus pour bloquer systématiquement (ou très
# souvent) l'accès automatisé — aussi bien notre rendu Playwright que l'outil
# serveur web_fetch (403, pages de détection de trafic, refus explicite).
# Sur ces domaines, on tente d'abord une source alternative (cf.
# SECONDARY_PRICE_SOURCES_FR) avant de perdre du temps sur l'URL d'origine.
HIGH_FRICTION_DOMAINS = [
    "ticketmaster.fr",
    "ticketmaster.com",
    "seetickets.com",
    "fnacspectacles.com",
    "leclercbilletterie.com",
    "gimstour.com",
]

# Plateformes de billetterie / agrégateurs : leur page d'accueil n'est jamais
# une fiche événement. À l'inverse, la page d'accueil du site officiel d'un
# festival est une bonne porte d'entrée (lien « Billetterie » à suivre) — elle
# ne doit pas être rejetée comme « générique » (constaté : le festival de
# référence lui-même finissait sans URL ni prix).
TICKETING_PORTAL_DOMAINS = sorted(
    {d for d in PREFERRED_SOURCES_FR + FESTIVAL_TICKETING_SOURCES + SECONDARY_PRICE_SOURCES_FR
     + HIGH_FRICTION_DOMAINS if "." in d}
    | {"eventim.fr", "festik.net", "helloasso.com", "francebillet.com", "placeminute.com", "sortiraparis.com"}
)

# --- Rendu navigateur (Playwright) -----------------------------------------
# Stratégie d'extraction des prix :
#   "playwright" : rendu navigateur local (GRATUIT) + 1 structuration bon marché.
#   "web_fetch"  : le LLM récupère la page (coûteux : la page entre dans le contexte).
#   "auto"       : Playwright d'abord (gratuit) ; bascule sur web_fetch UNIQUEMENT
#                  si la grille rendue est insuffisante. -> défaut, le moins cher.
FETCH_STRATEGY = os.environ.get("BENCHMARKER_FETCH_STRATEGY", "auto")

RENDER_TIMEOUT_MS = int(os.environ.get("BENCHMARKER_RENDER_TIMEOUT_MS", "30000"))
RENDER_IDLE_MS = int(os.environ.get("BENCHMARKER_RENDER_IDLE_MS", "6000"))
RENDER_TEXT_CAP = int(os.environ.get("BENCHMARKER_RENDER_TEXT_CAP", "12000"))
# Tentatives sur erreur réseau/protocole (ex. ERR_HTTP2_PROTOCOL_ERROR) avant
# d'abandonner — certains sites coupent la connexion de façon intermittente
# face à un navigateur headless ; un contexte frais suffit parfois.
RENDER_MAX_ATTEMPTS = int(os.environ.get("BENCHMARKER_RENDER_MAX_ATTEMPTS", "2"))
RENDER_USER_AGENT = os.environ.get(
    "BENCHMARKER_RENDER_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
)

# Sélecteurs courants de boutons « accepter les cookies » (billetteries FR).
COOKIE_ACCEPT_SELECTORS = [
    "#didomi-notice-agree-button",       # Didomi (très répandu en France)
    "button#onetrust-accept-btn-handler",  # OneTrust
    "button[aria-label*='accepter' i]",
    "button:has-text('Tout accepter')",
    "button:has-text('Accepter')",
    "button:has-text('J\\'accepte')",
    "#accept",
]

# Sélecteurs courants de boutons qui révèlent la grille tarifaire (souvent
# masquée derrière un widget de sélection tant qu'on n'a pas cliqué) : simple
# navigation d'utilisateur normal, best-effort, sans jamais aller jusqu'au
# paiement. Inclut les variantes anglaises (Shotgun.live et d'autres
# plateformes servent leurs pages en anglais par défaut) et le texte observé
# sur Ticketmaster (« Sélectionnez une séance »).
PRICE_REVEAL_SELECTORS = [
    "button:has-text('Voir les tarifs')",
    "button:has-text('Voir les prix')",
    "button:has-text('Choisir mes places')",
    "button:has-text('Choisir ma place')",
    "button:has-text('Voir les places')",
    "a:has-text('Voir les tarifs')",
    "a:has-text('Acheter')",
    "button:has-text('Acheter')",
    "button:has-text(\"J'achète\")",
    "button:has-text('Réserver')",
    "a:has-text('Billets')",
    "button:has-text('Sélectionner une séance')",
    "a:has-text('Sélectionner une séance')",
    "button:has-text('Choisir une séance')",
    "button:has-text('Get tickets')",
    "a:has-text('Get tickets')",
    "button:has-text('Buy tickets')",
    "a:has-text('Buy tickets')",
    "button:has-text('Select tickets')",
    "a:has-text('See tickets')",
]

# --- Profils qualité / coût -------------------------------------------------
# Un profil regroupe modèles + effort + budget d'outils pour un run donné.
# "eco" = valeurs par défaut ci-dessus (le moins cher). Les profils supérieurs
# améliorent surtout la COUVERTURE des dates (découverte) et la structuration.
DEFAULT_PROFILE = os.environ.get("BENCHMARKER_PROFILE", "eco")
PROFILES = ("eco", "equilibre", "max")


@dataclass
class RunSettings:
    """Réglages effectifs d'un run (résolus depuis un profil)."""

    discovery_model: str
    extraction_model: str
    structure_model: str
    discovery_effort: str
    extraction_effort: str
    max_search_uses: int
    max_fetch_uses: int
    fetch_max_content_tokens: int
    fetch_strategy: str


def _base_settings() -> RunSettings:
    """Profil 'eco' : reprend les défauts (surchargés par les variables d'env)."""
    return RunSettings(
        discovery_model=DISCOVERY_MODEL,
        extraction_model=EXTRACTION_MODEL,
        structure_model=STRUCTURE_MODEL,
        discovery_effort=DISCOVERY_EFFORT,
        extraction_effort=EXTRACTION_EFFORT,
        max_search_uses=MAX_SEARCH_USES,
        max_fetch_uses=MAX_FETCH_USES,
        fetch_max_content_tokens=FETCH_MAX_CONTENT_TOKENS,
        fetch_strategy=FETCH_STRATEGY,
    )


def resolve_settings(profile: str | None = None, strategy: str | None = None) -> RunSettings:
    """Construit les réglages effectifs pour un profil (et une stratégie optionnelle).

    - eco       : le moins cher (rendu gratuit + Haiku, Sonnet, effort bas).
    - equilibre : meilleure couverture des dates (effort high, 8 recherches) et
                  structuration en Sonnet ; prix toujours via rendu gratuit.
    - max       : Opus en découverte/fetch, Sonnet en structuration, effort élevé.
    """
    profile = (profile or DEFAULT_PROFILE or "eco").lower()
    if profile not in PROFILES:
        profile = "eco"
    s = _base_settings()

    if profile == "equilibre":
        s.discovery_effort = "high"
        s.max_search_uses = 8
        s.structure_model = "claude-sonnet-5"
    elif profile == "max":
        s.discovery_model = "claude-opus-4-8"
        s.extraction_model = "claude-opus-4-8"
        s.structure_model = "claude-sonnet-5"
        s.discovery_effort = "high"
        s.extraction_effort = "medium"
        s.max_search_uses = 8

    if strategy:
        s.fetch_strategy = strategy
    return s


# --- Efficience : extraction parallèle et mémoire des grilles ----------------
# Nombre d'événements extraits en parallèle (un navigateur headless par fil,
# quelques centaines de Mo chacun). Même coût API, durée divisée d'autant.
EXTRACTION_WORKERS = int(os.environ.get("BENCHMARKER_EXTRACTION_WORKERS", "3"))
# Grilles mémorisées par événement : un run relancé ne repaie pas les mêmes
# pages et rend le même résultat. Échecs gardés moins longtemps (un blocage
# peut n'être que passager).
CACHE_DIR = Path(os.environ.get("BENCHMARKER_CACHE_DIR") or (PROJECT_ROOT / "cache" / "prices"))
PRICE_CACHE_TTL_DAYS = float(os.environ.get("BENCHMARKER_PRICE_CACHE_TTL_DAYS", "7"))
PRICE_CACHE_FAIL_TTL_HOURS = float(os.environ.get("BENCHMARKER_PRICE_CACHE_FAIL_TTL_HOURS", "24"))
# Version de la chaîne d'extraction : un ÉCHEC mémorisé par une version
# antérieure n'est pas rejoué (constaté : des échecs de l'ancien code ressortis
# « en mémoire » au lieu d'être retentés avec les correctifs). Les grilles
# trouvées restent valables quelle que soit la version. À incrémenter quand
# l'extraction s'améliore.
EXTRACTION_VERSION = 5

# --- Base de référence (mémoire cumulative) ---------------------------------
# Chaque run y enregistre ses événements tarifés ; les suivants y puisent les
# événements déjà connus dans le profil (sans repayer leur extraction), et la
# préconisation peut s'appuyer sur toute la base.
BASE_PATH = Path(os.environ.get("BENCHMARKER_BASE_PATH") or (PROJECT_ROOT / "data" / "reference.sqlite"))
# Âge maximal d'une grille en base pour être reprise dans un nouveau run.
BASE_MAX_AGE_DAYS = int(os.environ.get("BENCHMARKER_BASE_MAX_AGE_DAYS", "365"))

# --- Justesse des statistiques ---------------------------------------------
# Prix plein tarif en dessous de ce seuil : erreur ou tarif enfant non nommé
# (constaté : un « Pass enfant » à 3 € tirait le minimum global) -> écarté des
# stats et signalé.
MIN_PLAUSIBLE_PRICE_EUR = float(os.environ.get("BENCHMARKER_MIN_PLAUSIBLE_PRICE_EUR", "5"))
# Score de comparabilité (0-100) sous lequel un événement est « hors profil » :
# affiché, mais écarté des statistiques et de la préconisation.
COMPARABILITY_MIN = int(os.environ.get("BENCHMARKER_COMPARABILITY_MIN", "40"))
# Seuil plus exigeant pour reprendre de la base un événement qui n'est pas
# parmi les comparables choisis pour CE run (constaté : un festival de 32 000/
# jour repris à 40/100 pour une cible à ~17 500/jour, seul tarifé du run).
BASE_COMPARABILITY_MIN = int(os.environ.get("BENCHMARKER_BASE_COMPARABILITY_MIN", "55"))

# Devise de référence pour la normalisation.
BASE_CURRENCY = "EUR"

# Taux de change indicatifs vers l'EUR (fallback si aucune conversion trouvée).
# À affiner / remplacer par une source live si le périmètre s'étend hors zone €.
FX_TO_EUR = {
    "EUR": 1.0,
    "GBP": 1.17,
    "CHF": 1.05,
    "USD": 0.92,
}
