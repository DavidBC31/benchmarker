"""Configuration centrale du benchmarker."""

from __future__ import annotations

import os

# --- Modèle LLM -------------------------------------------------------------
# On utilise par défaut le modèle le plus capable pour la découverte et
# l'extraction. Surchargeable via la variable d'environnement.
MODEL = os.environ.get("BENCHMARKER_MODEL", "claude-opus-4-8")

# Effort de raisonnement (low | medium | high | xhigh | max).
DISCOVERY_EFFORT = os.environ.get("BENCHMARKER_DISCOVERY_EFFORT", "high")
EXTRACTION_EFFORT = os.environ.get("BENCHMARKER_EXTRACTION_EFFORT", "medium")

# --- Outils serveur ---------------------------------------------------------
# Versions à jour avec filtrage dynamique (nécessitent Opus 4.8/4.7/4.6, Sonnet).
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

# Nombre max d'appels d'outils par étape (garde-fou coût/latence).
MAX_SEARCH_USES = int(os.environ.get("BENCHMARKER_MAX_SEARCH_USES", "8"))
MAX_FETCH_USES = int(os.environ.get("BENCHMARKER_MAX_FETCH_USES", "4"))

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
# Stratégie d'extraction des prix : "web_fetch" (LLM), "playwright" (navigateur),
# ou "auto" (web_fetch puis fallback navigateur si la grille est insuffisante).
FETCH_STRATEGY = os.environ.get("BENCHMARKER_FETCH_STRATEGY", "web_fetch")

RENDER_TIMEOUT_MS = int(os.environ.get("BENCHMARKER_RENDER_TIMEOUT_MS", "30000"))
RENDER_IDLE_MS = int(os.environ.get("BENCHMARKER_RENDER_IDLE_MS", "5000"))
RENDER_TEXT_CAP = int(os.environ.get("BENCHMARKER_RENDER_TEXT_CAP", "20000"))
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
