"""Phase 1 — Découverte des dates via recherche web.

Le LLM ratisse le web pour trouver les concerts correspondant aux critères,
puis on structure les résultats en objets `Concert` (sans prix à ce stade).
"""

from __future__ import annotations

from typing import Callable, List, Optional

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
Ta mission : trouver des dates de concerts réelles correspondant à des critères,
avec un nombre LIMITÉ de recherches web.

Méthode :
- Priorise la LARGEUR : couvre autant de dates pertinentes que possible avec ton
  budget de recherche, plutôt que d'approfondir une seule date en détail.
- Reste factuel : n'invente jamais une date, un lieu ou un prix. Si tu n'es pas sûr, ne l'inclus pas.
- Identifie la nature de la date (date unique, tournée, festival) et le style musical.

Sur les URLs (secondaire — ne consomme PAS de recherche dédiée pour ça, une
étape ultérieure s'en charge séparément) :
- Note l'URL de billetterie uniquement si elle apparaît naturellement dans tes
  résultats de recherche. Ne fais pas de recherche supplémentaire juste pour
  vérifier ou préciser une URL — laisse le champ vide si tu n'as qu'une page
  d'accueil générique (ex. jds.fr, infoconcert.com) ou rien du tout.
- L'absence d'URL ne doit JAMAIS te faire exclure une date par ailleurs
  confirmée (artiste + date + salle trouvés). Mieux vaut une date sans URL
  qu'une date manquante.

RÈGLE CRITIQUE si ton budget de recherche s'épuise ou qu'un appel est refusé :
- Ne t'excuse JAMAIS et ne demande JAMAIS à l'utilisateur de renvoyer sa demande.
- Réponds TOUJOURS avec la liste des dates que tu as déjà identifiées à ce
  stade, même incomplète. Une réponse partielle vaut toujours mieux qu'aucune
  réponse ou qu'un message d'excuse.

Tu produiras une liste de dates candidates ; l'extraction des prix se fait dans une étape ultérieure."""

_DISCOVERY_INSTRUCTION = """À partir des résultats de recherche ci-dessous, produis la liste
structurée des dates de concerts trouvées. Pour chaque date, renseigne au maximum :
artiste, date (ISO), salle, ville, pays, jauge si connue, style, nature de l'événement,
et l'URL de billetterie si elle apparaît dans les résultats (laisse vide sinon —
ne pas essayer de la deviner ou de la reconstruire).
IMPORTANT : inclus TOUTES les dates confirmées dans les résultats de recherche
ci-dessous, même celles sans URL. Ne rejette une date que si elle est hors
critères ou si elle est un doublon.
Ignore les doublons et les dates hors critères."""


def discover(
    client: anthropic.Anthropic,
    criteria: SearchCriteria,
    settings: config.RunSettings | None = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Concert]:
    """Retourne une liste de `Concert` candidats (sans grille tarifaire)."""
    settings = settings or config.resolve_settings()
    log = progress or (lambda _msg: None)
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

    # Diagnostic explicite : distingue "la recherche web n'a rien remonté" de
    # "la structuration n'a rien extrait" — sinon un 0 résultat est muet sur
    # sa cause et oblige à deviner.
    if not findings:
        log("  ! recherche web : aucun résultat retourné par le modèle.")
        return []
    log(f"  recherche web : {len(findings)} caractère(s) de résultats bruts.")

    parsed = parse_structured(
        client,
        schema=DiscoveredList,
        instruction=_DISCOVERY_INSTRUCTION,
        material=findings,
        model=settings.structure_model,
    )
    if not parsed or not parsed.concerts:
        preview = findings[:400].replace("\n", " ")
        log(f"  ! structuration : 0 date extraite. Aperçu des résultats de recherche : {preview}…")
        return []
    return [discovered_to_concert(d) for d in parsed.concerts]
