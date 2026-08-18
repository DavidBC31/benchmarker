"""Configuration centrale du benchmarker."""

from __future__ import annotations

import os
from dataclasses import dataclass

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
# sert uniquement à couvrir large — 6 suffit à un léger matelas de sécurité.
MAX_SEARCH_USES = int(os.environ.get("BENCHMARKER_MAX_SEARCH_USES", "6"))
MAX_FETCH_USES = int(os.environ.get("BENCHMARKER_MAX_FETCH_USES", "2"))

# Plafond de tokens de contenu ramené par web_fetch dans le contexte (coût).
FETCH_MAX_CONTENT_TOKENS = int(
    os.environ.get("BENCHMARKER_FETCH_MAX_CONTENT_TOKENS", "6000")
)

# Recherche ciblée max pour retrouver une URL directe de billetterie quand la
# découverte n'a renvoyé qu'une page générique (page d'accueil/portail) ou rien.
# Peu coûteux : un seul appel, uniquement pour les dates concernées.
URL_REFINE_MAX_USES = int(os.environ.get("BENCHMARKER_URL_REFINE_MAX_USES", "2"))

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

# --- Rendu navigateur (Playwright) -----------------------------------------
# Stratégie d'extraction des prix :
#   "playwright" : rendu navigateur local (GRATUIT) + 1 structuration bon marché.
#   "web_fetch"  : le LLM récupère la page (coûteux : la page entre dans le contexte).
#   "auto"       : Playwright d'abord (gratuit) ; bascule sur web_fetch UNIQUEMENT
#                  si la grille rendue est insuffisante. -> défaut, le moins cher.
FETCH_STRATEGY = os.environ.get("BENCHMARKER_FETCH_STRATEGY", "auto")

RENDER_TIMEOUT_MS = int(os.environ.get("BENCHMARKER_RENDER_TIMEOUT_MS", "30000"))
RENDER_IDLE_MS = int(os.environ.get("BENCHMARKER_RENDER_IDLE_MS", "4000"))
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
