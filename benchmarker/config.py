"""Configuration centrale du benchmarker."""

from __future__ import annotations

import os

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
MAX_SEARCH_USES = int(os.environ.get("BENCHMARKER_MAX_SEARCH_USES", "5"))
MAX_FETCH_USES = int(os.environ.get("BENCHMARKER_MAX_FETCH_USES", "2"))

# Plafond de tokens de contenu ramené par web_fetch dans le contexte (coût).
FETCH_MAX_CONTENT_TOKENS = int(
    os.environ.get("BENCHMARKER_FETCH_MAX_CONTENT_TOKENS", "6000")
)

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
