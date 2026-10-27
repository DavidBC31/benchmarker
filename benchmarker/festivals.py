"""Mode Festival — résolution de festivals comparables et découverte.

Pendant du mode Artiste (`similar_artists.py` + `discovery.py`), avec des
critères propres aux festivals : jauge journalière, cadre (rural / périurbain
/ urbain), programmation, nombre de soirs et d'artistes par soir. L'extraction
des prix réutilise la même mécanique que les concerts (`extraction.py`).
"""

from __future__ import annotations

from datetime import date as _date
from typing import Callable, List, Optional

import anthropic

from . import config
from .discovery import search_budget
from .llm import parse_structured, run_server_tool_loop
from .models import (
    DiscoveredFestivalList,
    Festival,
    FestivalCriteria,
    SimilarFestivalsResult,
    discovered_to_festival,
)

# --- Festivals comparables -----------------------------------------------------
_SIMILAR_SYSTEM = """Tu es un expert de l'économie des festivals de musique (programmation,
billetterie, production). On te donne un festival de référence. Ta mission : identifier des
festivals vraiment COMPARABLES pour un benchmark tarifaire, sur l'ensemble de ces critères :
- jauge JOURNALIÈRE du même ordre de grandeur (festivaliers par jour, pas la fréquentation cumulée) ;
- cadre d'implantation similaire : rural (site en pleine campagne), périurbain, ou urbain (centre-ville) ;
- programmation proche : styles dominants et niveau des têtes d'affiche ;
- format proche : nombre de soirs / jours, nombre d'artistes par soir.

Utilise la recherche web pour établir le profil du festival de référence sur ces critères,
puis trouve des festivals qui partagent ce profil (France en priorité).

La JAUGE JOURNALIÈRE est le critère prioritaire (elle détermine le niveau de
programmation et de prix) : ne retiens que des festivals entre environ 0,6× et 1,6×
la jauge journalière de la référence. Un festival 2 à 3 fois plus gros n'est PAS
comparable, même s'il est plus connu ou partage une tête d'affiche : préfère des
festivals moins médiatisés mais de taille équivalente. Un festival
électro urbain de 5 000 personnes/jour n'est pas comparable à un festival rural
généraliste de 50 000 personnes/jour, même s'ils partagent une tête d'affiche.
Reste factuel : ne propose que des festivals dont tu es raisonnablement sûr."""

_SIMILAR_INSTRUCTION = """À partir des recherches ci-dessous, résume en une ligne le profil du
festival de référence (cadre, jauge journalière, programmation, nombre de soirs), indique sa
programmation dominante, et liste les festivals comparables pour un benchmark tarifaire.
Donne aussi ce profil en chiffres : jauge journalière, cadre (urbain / periurbain / rural),
nombre de soirs, artistes par soir (0 ou chaîne vide si inconnu ; pour une fourchette,
prends le milieu).
N'inclus PAS le festival de référence lui-même dans la liste des comparables."""


def find_similar_festivals(
    client: anthropic.Anthropic,
    festival: str,
    *,
    count: Optional[int] = None,
    settings: "config.RunSettings | None" = None,
) -> SimilarFestivalsResult:
    """Identifie des festivals comparables à `festival` (un seul appel de recherche)."""
    settings = settings or config.resolve_settings()
    count = count or config.SIMILAR_FESTIVALS_COUNT
    web_search = dict(config.WEB_SEARCH_TOOL)
    web_search["max_uses"] = config.SIMILAR_FESTIVALS_MAX_USES

    findings = run_server_tool_loop(
        client,
        system=_SIMILAR_SYSTEM,
        user=(
            f"Festival de référence : {festival}\n\n"
            f"Trouve environ {count} festivals comparables (jauge journalière, cadre, "
            "programmation et format similaires) pour construire un benchmark tarifaire."
        ),
        tools=[web_search],
        model=settings.discovery_model,
        effort=settings.discovery_effort,
    )
    if not findings:
        return SimilarFestivalsResult(
            reference_festival=festival, reference_capacity=0, reference_setting="",
            reference_nights=0, reference_artists_per_night=0,
        )

    parsed = parse_structured(
        client,
        schema=SimilarFestivalsResult,
        instruction=_SIMILAR_INSTRUCTION,
        material=f"Festival de référence : {festival}\n\n{findings}",
        model=settings.structure_model,
    )
    if parsed is None:
        return SimilarFestivalsResult(
            reference_festival=festival, reference_capacity=0, reference_setting="",
            reference_nights=0, reference_artists_per_night=0,
        )

    similar = [
        f for f in parsed.similar_festivals
        if f.strip().lower() != festival.strip().lower()
    ]
    return SimilarFestivalsResult(
        reference_festival=festival,
        inferred_profile=parsed.inferred_profile,
        inferred_genre=parsed.inferred_genre,
        similar_festivals=similar,
        reference_capacity=parsed.reference_capacity,
        reference_setting=parsed.reference_setting,
        reference_nights=parsed.reference_nights,
        reference_artists_per_night=parsed.reference_artists_per_night,
    )


# --- Découverte ----------------------------------------------------------------
_DISCOVERY_SYSTEM = """Tu es un analyste billetterie spécialisé dans les festivals de musique.
Ta mission : trouver des festivals réels (et leur édition la plus pertinente) correspondant à
des critères, avec un nombre LIMITÉ de recherches web.

Pour chaque festival retenu, établis ces caractéristiques (ce sont les critères de
comparaison du benchmark) :
- jauge JOURNALIÈRE (festivaliers par jour). Si seule la fréquentation cumulée est connue,
  estime la jauge journalière en la divisant par le nombre de jours ;
- cadre : rural (site en plein air hors agglomération), périurbain, ou urbain (centre-ville) ;
- programmation : styles dominants et principales têtes d'affiche de l'édition ;
- format : nombre de soirs / jours, nombre d'artistes par soir, nombre total d'artistes.

Méthode :
- Priorise la LARGEUR : couvre autant de festivals pertinents que possible avec ton budget.
- Reste factuel : n'invente jamais un festival, une jauge, une date ou un prix.
- Si des « festivals ciblés » sont fournis : concentre tes recherches sur CES festivals
  (sélectionnés en amont pour leur pertinence) et répartis ton budget entre eux.
- Édition : privilégie l'édition en cours de vente, ou la plus récente dont la grille de pass
  est probablement encore publiée (une édition passée récente reste valable). Évite les
  éditions anciennes (plus d'un an, tarifs souvent dépubliés) et une édition future dont la
  billetterie n'a pas encore ouvert.

Sur les URLs (secondaire — une étape ultérieure s'en charge) : la page billetterie / tarifs du
site officiel du festival est la source idéale (les grilles de pass y sont souvent en clair) ;
note-la si elle apparaît dans tes résultats, sans recherche dédiée. L'absence d'URL ne doit
JAMAIS te faire exclure un festival par ailleurs confirmé.

RÈGLE CRITIQUE si ton budget de recherche s'épuise ou qu'un appel est refusé :
- Ne t'excuse JAMAIS et ne demande JAMAIS à l'utilisateur de renvoyer sa demande.
- Réponds TOUJOURS avec la liste des festivals déjà identifiés, même incomplète."""

_DISCOVERY_INSTRUCTION = """À partir des résultats de recherche ci-dessous, produis la liste
structurée des festivals trouvés. Pour chacun, renseigne au maximum : nom, édition (année),
dates de début et de fin (ISO), commune, région, pays, cadre (urbain / periurbain / rural),
jauge journalière, programmation dominante, têtes d'affiche (séparées par des virgules),
nombre de soirs, artistes par soir, et l'URL de billetterie ou de la page tarifs si elle
apparaît (laisse vide sinon — ne pas la deviner). Champ inconnu : chaîne vide ou 0.
Indique honnêtement si la jauge journalière et le nombre d'artistes par soir sont
estimés (calculés, approximés) plutôt que lus tels quels dans les résultats.
IMPORTANT : inclus TOUS les festivals confirmés, même sans URL. Ignore les doublons et les
festivals hors critères."""


def edition_hint(today: _date) -> str:
    """Édition à viser, en années explicites. Sans ça, le modèle se calait sur
    des éditions déjà fermées (constaté : éditions 2025 visées en octobre 2026,
    billetteries fermées, aucun prix)."""
    y = today.year
    if today.month >= 9:  # saison estivale terminée
        return (f"Les éditions de l'été {y} sont passées ; la billetterie de l'édition {y + 1} est souvent déjà "
                f"ouverte (prévente / early bird). Vise l'édition {y + 1} si ses tarifs sont publiés, sinon "
                f"l'édition {y} (tarifs souvent encore visibles). N'utilise pas l'édition {y - 1} ni les précédentes.")
    if today.month <= 4:
        return (f"La saison {y} approche : la billetterie de l'édition {y} est généralement ouverte. Vise "
                f"l'édition {y}, sinon {y - 1} si ses tarifs sont encore visibles. N'utilise pas {y - 2} ni avant.")
    return (f"La saison {y} est en cours : vise l'édition {y} (billetterie ouverte ou tarifs encore visibles), "
            f"sinon {y - 1}. N'utilise pas {y - 2} ni avant.")


def discover_festivals(
    client: anthropic.Anthropic,
    criteria: FestivalCriteria,
    settings: "config.RunSettings | None" = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Festival]:
    """Retourne une liste de `Festival` candidats (sans grille tarifaire)."""
    settings = settings or config.resolve_settings()
    log = progress or (lambda _msg: None)
    user = (
        f"Date d'aujourd'hui : {_date.today().isoformat()}. {edition_hint(_date.today())}\n\n"
        "Trouve des festivals correspondant aux critères suivants :\n\n"
        f"{criteria.to_prompt()}\n\n"
        f"Sources billetterie courantes des festivals : {', '.join(config.FESTIVAL_TICKETING_SOURCES)}.\n"
        "Si un résultat affiche déjà les prix des pass (site officiel, agrégateur comme "
        f"{', '.join(config.SECONDARY_PRICE_SOURCES_FR)}, presse), note-les.\n"
        f"Vise environ {criteria.max_results} festivals distincts et pertinents. "
        "Présente-les clairement (un festival par bloc) avec les URLs trouvées."
    )

    web_search, timeout = search_budget(settings, len(criteria.target_festivals), "festival(s)", log)

    findings = run_server_tool_loop(
        client,
        system=_DISCOVERY_SYSTEM,
        user=user,
        tools=[web_search],
        model=settings.discovery_model,
        effort=settings.discovery_effort,
        timeout=timeout,
    )
    if not findings:
        log("  ! recherche web : aucun résultat retourné par le modèle.")
        return []
    log(f"  recherche web : {len(findings)} caractère(s) de résultats bruts.")

    parsed = parse_structured(
        client,
        schema=DiscoveredFestivalList,
        instruction=_DISCOVERY_INSTRUCTION,
        material=findings,
        model=settings.structure_model,
    )
    if not parsed or not parsed.festivals:
        preview = findings[:400].replace("\n", " ")
        log(f"  ! structuration : 0 festival extrait. Aperçu des résultats de recherche : {preview}…")
        return []
    return [discovered_to_festival(d) for d in parsed.festivals]
