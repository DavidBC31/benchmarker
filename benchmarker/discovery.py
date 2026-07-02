"""Phase 1 — Découverte des dates via recherche web.

Le LLM ratisse le web pour trouver les concerts correspondant aux critères,
puis on structure les résultats en objets `Concert` (sans prix à ce stade).
"""

from __future__ import annotations

from typing import List

import anthropic

from . import config
from .llm import parse_structured, run_server_tool_loop
from .models import Concert, ConcertList, SearchCriteria

_DISCOVERY_SYSTEM = """Tu es un analyste billetterie spécialisé dans le live musical.
Ta mission : trouver des dates de concerts réelles correspondant à des critères.

Méthode :
- Utilise la recherche web pour identifier des dates concrètes (artiste, date, salle, ville).
- Privilégie les sources billetterie fiables et les sites officiels de salles/festivals.
- Reste factuel : n'invente jamais une date, un lieu ou un prix. Si tu n'es pas sûr, ne l'inclus pas.
- Pour chaque date, note l'URL de la page billetterie de référence si tu la trouves.
- Identifie la nature de la date (date unique, tournée, festival) et le style musical.

Tu produiras une liste de dates candidates ; l'extraction des prix se fait dans une étape ultérieure."""

_DISCOVERY_INSTRUCTION = """À partir des résultats de recherche ci-dessous, produis la liste
structurée des dates de concerts trouvées. Pour chaque date, renseigne au maximum :
artiste, date (ISO), salle, ville, pays, jauge si connue, style, nature de l'événement,
et l'URL de la page billetterie. NE mets PAS de prix à cette étape (prices vide).
Ignore les doublons et les dates hors critères."""


def discover(client: anthropic.Anthropic, criteria: SearchCriteria) -> List[Concert]:
    """Retourne une liste de `Concert` candidats (sans grille tarifaire)."""
    sources_hint = ", ".join(config.PREFERRED_SOURCES_FR)
    user = (
        "Trouve des dates de concerts correspondant aux critères suivants :\n\n"
        f"{criteria.to_prompt()}\n\n"
        f"Sources billetterie à privilégier (France) : {sources_hint}.\n"
        f"Vise environ {criteria.max_results} dates distinctes et pertinentes. "
        "Présente-les de façon claire (une date par bloc) avec les URLs trouvées."
    )

    web_search = dict(config.WEB_SEARCH_TOOL)
    web_search["max_uses"] = config.MAX_SEARCH_USES

    findings = run_server_tool_loop(
        client,
        system=_DISCOVERY_SYSTEM,
        user=user,
        tools=[web_search],
        effort=config.DISCOVERY_EFFORT,
    )

    if not findings:
        return []

    parsed = parse_structured(
        client,
        schema=ConcertList,
        instruction=_DISCOVERY_INSTRUCTION,
        material=findings,
        effort="medium",
    )
    return parsed.concerts if parsed else []
