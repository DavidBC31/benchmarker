"""Résolution d'artistes comparables à partir d'un artiste de référence.

Permet de compléter (ou remplacer) le filtre par style musical : l'utilisateur
donne un artiste connu, l'outil identifie via recherche web des artistes du
même style, de notoriété et de taille de salle comparables, pour orienter la
découverte sur une liste précise plutôt que sur un style générique — plus
pertinent pour un benchmark tarifaire par comparables.
"""

from __future__ import annotations

import anthropic

from . import config
from .llm import parse_structured, run_server_tool_loop
from .models import SimilarArtistsResult

_SIMILAR_SYSTEM = """Tu es un expert en programmation musicale et en booking de concerts.
On te donne un artiste de référence. Ta mission : identifier des artistes vraiment
COMPARABLES pour un benchmark tarifaire — même style musical, notoriété et taille de
salle/jauge du même ordre de grandeur (pas seulement le style musical au sens large).

Utilise la recherche web pour t'informer sur : le style musical de l'artiste de
référence, sa notoriété actuelle, la taille des salles qu'il joue habituellement
(Zénith, Arena, club…).

Exemple : pour un artiste de rap qui remplit des Zéniths, ne propose pas un artiste
de rap qui ne joue que des clubs de 500 places, ni une pop star qui remplit des stades.
Reste factuel : ne propose que des artistes dont tu es raisonnablement sûr qu'ils
correspondent à ces critères."""

_SIMILAR_INSTRUCTION = """À partir des recherches ci-dessous, indique le style musical
dominant de l'artiste de référence et propose une liste d'artistes comparables (même
style, notoriété et taille de salle similaires) pour un benchmark tarifaire.
N'inclus PAS l'artiste de référence lui-même dans la liste des comparables."""


def find_similar_artists(
    client: anthropic.Anthropic,
    artist: str,
    *,
    count: int | None = None,
    settings: "config.RunSettings | None" = None,
) -> SimilarArtistsResult:
    """Identifie des artistes comparables à `artist` (un seul appel de recherche)."""
    settings = settings or config.resolve_settings()
    count = count or config.SIMILAR_ARTISTS_COUNT

    user = (
        f"Artiste de référence : {artist}\n\n"
        f"Trouve environ {count} artistes comparables (même style musical, notoriété "
        "et taille de salle similaires) pour construire un benchmark tarifaire pertinent."
    )
    web_search = dict(config.WEB_SEARCH_TOOL)
    web_search["max_uses"] = config.SIMILAR_ARTISTS_MAX_USES

    findings = run_server_tool_loop(
        client,
        system=_SIMILAR_SYSTEM,
        user=user,
        tools=[web_search],
        model=settings.discovery_model,
        effort=settings.discovery_effort,
    )
    if not findings:
        return SimilarArtistsResult(reference_artist=artist)

    parsed = parse_structured(
        client,
        schema=SimilarArtistsResult,
        instruction=_SIMILAR_INSTRUCTION,
        material=f"Artiste de référence : {artist}\n\n{findings}",
        model=settings.structure_model,
    )
    if parsed is None:
        return SimilarArtistsResult(reference_artist=artist)

    # Le modèle reformule parfois le nom ; on garde la saisie de l'utilisateur
    # comme référence canonique, et on filtre un éventuel doublon.
    similar = [
        a for a in parsed.similar_artists
        if a.strip().lower() != artist.strip().lower()
    ]
    return SimilarArtistsResult(
        reference_artist=artist,
        inferred_genre=parsed.inferred_genre,
        similar_artists=similar,
    )
