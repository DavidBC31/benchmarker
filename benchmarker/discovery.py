"""Phase 1 — Découverte des dates via recherche web.

Le LLM ratisse le web pour trouver les concerts correspondant aux critères,
puis on structure les résultats en objets `Concert` (sans prix à ce stade).
"""

from __future__ import annotations

from datetime import date as _date
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
- Si des « artistes ciblés » sont fournis dans les critères : concentre tes
  recherches sur CES artistes précis (une ou plusieurs dates par artiste,
  chacun a déjà été sélectionné en amont pour sa pertinence). Ne cherche pas
  d'autres artistes dans ce cas — répartis ton budget de recherche entre eux.

Sur les URLs (secondaire — ne consomme PAS de recherche dédiée pour ça, une
étape ultérieure s'en charge séparément) :
- Note l'URL de billetterie uniquement si elle apparaît naturellement dans tes
  résultats de recherche. Ne fais pas de recherche supplémentaire juste pour
  vérifier ou préciser une URL — laisse le champ vide si tu n'as qu'une page
  d'accueil générique (ex. jds.fr, infoconcert.com) ou rien du tout.
- L'absence d'URL ne doit JAMAIS te faire exclure une date par ailleurs
  confirmée (artiste + date + salle trouvés). Mieux vaut une date sans URL
  qu'une date manquante.

Sur le choix des dates quand tu as plusieurs candidats pour un même artiste
(la date exacte est secondaire pour ce benchmark, l'important est le prix) :
- Une date déjà passée reste tout à fait valable si sa page billetterie est
  probablement encore en ligne avec sa grille tarifaire (courant pour les
  billetteries qui gardent l'historique) — privilégie les dates passées
  RÉCENTES (quelques mois) à ce titre plutôt que de les écarter d'office.
- Évite en priorité les dates très anciennes (plus d'un an) : leur page est
  souvent dépubliée. Évite aussi les dates trop lointaines dans le futur si
  la billetterie n'a probablement pas encore ouvert (prix non disponibles).
- En clair : si tu dois choisir entre plusieurs dates plausibles pour un même
  artiste, privilégie celles dont le prix a de bonnes chances d'être encore
  accessible (en vente actuellement, ou terminées depuis peu) plutôt que des
  dates très anciennes ou une mise en vente pas encore ouverte.

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
    secondary_hint = ", ".join(config.SECONDARY_PRICE_SOURCES_FR)
    user = (
        f"Date d'aujourd'hui : {_date.today().isoformat()} (pour juger si une date "
        "passée est récente ou trop ancienne, et si une date future a probablement "
        "déjà sa billetterie ouverte).\n\n"
        "Trouve des dates de concerts correspondant aux critères suivants :\n\n"
        f"{criteria.to_prompt()}\n\n"
        f"Sources billetterie à privilégier (France) : {sources_hint}.\n"
        f"Si un de ces résultats de recherche affiche déjà un prix indicatif "
        f"(agrégateurs/presse comme {secondary_hint}), note-le même si tu as "
        "aussi une URL de billetterie officielle — les grosses plateformes "
        "bloquent souvent l'extraction automatisée du détail tarifaire, un "
        "prix approximatif trouvé directement vaut mieux qu'aucun prix.\n"
        f"Vise environ {criteria.max_results} dates distinctes et pertinentes. "
        "Présente-les de façon claire (une date par bloc) avec les URLs trouvées."
    )

    web_search = dict(config.WEB_SEARCH_TOOL)
    n_targets = len(criteria.target_artists)
    if n_targets:
        # Budget proportionnel au nombre d'artistes ciblés (répartition
        # explicite demandée dans le prompt) plutôt qu'un plafond fixe pensé
        # pour une recherche par style générique — sinon le budget s'épuise
        # avant d'avoir couvert chaque artiste, et le modèle abandonne trop tôt.
        web_search["max_uses"] = min(
            config.MAX_SEARCH_USES_CAP,
            max(settings.max_search_uses, n_targets * config.DISCOVERY_SEARCH_USES_PER_ARTIST),
        )
        log(f"  budget de recherche découverte : {web_search['max_uses']} "
            f"(pour {n_targets} artiste(s) ciblé(s))")
    else:
        web_search["max_uses"] = settings.max_search_uses

    # Le timeout par défaut (config.LLM_REQUEST_TIMEOUT_S) suppose un budget de
    # recherche standard ; avec un budget élargi (artistes ciblés, jusqu'à
    # MAX_SEARCH_USES_CAP), on l'allonge proportionnellement pour ne pas couper
    # un appel qui a légitimement besoin de plus de temps.
    timeout = config.LLM_REQUEST_TIMEOUT_S * max(1, web_search["max_uses"] / config.MAX_SEARCH_USES)

    findings = run_server_tool_loop(
        client,
        system=_DISCOVERY_SYSTEM,
        user=user,
        tools=[web_search],
        model=settings.discovery_model,
        effort=settings.discovery_effort,
        timeout=timeout,
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
