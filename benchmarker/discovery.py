"""Phase 1 — Découverte des dates via recherche web.

Le LLM ratisse le web pour trouver les concerts correspondant aux critères,
puis on structure les résultats en objets `Concert` (sans prix à ce stade).
"""

from __future__ import annotations

from typing import List

import anthropic

from . import config
from .llm import parse_structured, run_server_tool_loop
from .models import (
    Concert,
    DiscoveredList,
    SearchCriteria,
    discovered_to_concert,
)

_DISCOVERY_SYSTEM = """Tu es un analyste billetterie spécialisé dans le live musical.
Ta mission : trouver des dates de concerts réelles correspondant à des critères.

Méthode :
- Utilise la recherche web pour identifier des dates concrètes (artiste, date, salle, ville).
- Privilégie les sources billetterie fiables et les sites officiels de salles/festivals.
- Reste factuel : n'invente jamais une date, un lieu ou un prix. Si tu n'es pas sûr, ne l'inclus pas.
- Identifie la nature de la date (date unique, tournée, festival) et le style musical.

Règle STRICTE sur les URLs (le point le plus important) :
- L'URL doit pointer DIRECTEMENT sur la fiche de vente de CET événement précis
  (ex : contient un identifiant d'événement, un slug artiste+date, un chemin du
  type /evenement/…, /event/…, /place-spectacle/…, /billets/…).
- N'indique JAMAIS l'URL d'une page d'accueil, d'un portail agrégateur
  d'événements (ex. jds.fr, infoconcert.com), d'une page de recherche ou
  d'une liste d'événements. ARRIVER LE PLUS PRÉCIS POSSIBLE.
- Si tu ne trouves pas d'URL directe vers la fiche de CET événement, laisse
  l'URL vide plutôt que de mettre une page d'accueil ou un portail générique.
  Une URL vide est préférable à une URL générique.

Tu produiras une liste de dates candidates ; l'extraction des prix se fait dans une étape ultérieure."""

_DISCOVERY_INSTRUCTION = """À partir des résultats de recherche ci-dessous, produis la liste
structurée des dates de concerts trouvées. Pour chaque date, renseigne au maximum :
artiste, date (ISO), salle, ville, pays, jauge si connue, style, nature de l'événement,
et l'URL de la page billetterie DIRECTE de cet événement (jamais une page d'accueil
ou un portail générique — laisse le champ vide si tu n'as trouvé qu'une page générique).
Ignore les doublons et les dates hors critères."""


def discover(
    client: anthropic.Anthropic,
    criteria: SearchCriteria,
    settings: config.RunSettings | None = None,
) -> List[Concert]:
    """Retourne une liste de `Concert` candidats (sans grille tarifaire)."""
    settings = settings or config.resolve_settings()
    sources_hint = ", ".join(config.PREFERRED_SOURCES_FR)
    user = (
        "Trouve des dates de concerts correspondant aux critères suivants :\n\n"
        f"{criteria.to_prompt()}\n\n"
        f"Sources billetterie à privilégier (France) : {sources_hint}.\n"
        f"Vise environ {criteria.max_results} dates distinctes et pertinentes. "
        "Présente-les de façon claire (une date par bloc) avec les URLs trouvées."
    )

    web_search = dict(config.WEB_SEARCH_TOOL)
    web_search["max_uses"] = settings.max_search_uses

    findings = run_server_tool_loop(
        client,
        system=_DISCOVERY_SYSTEM,
        user=user,
        tools=[web_search],
        model=settings.discovery_model,
        effort=settings.discovery_effort,
    )

    if not findings:
        return []

    parsed = parse_structured(
        client,
        schema=DiscoveredList,
        instruction=_DISCOVERY_INSTRUCTION,
        material=findings,
        model=settings.structure_model,
    )
    if not parsed:
        return []
    return [discovered_to_concert(d) for d in parsed.concerts]
