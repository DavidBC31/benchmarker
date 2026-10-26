"""Schéma de données du benchmark (Pydantic v2).

Ces modèles servent à la fois de contrat pour la sortie structurée du LLM
(`messages.parse`) et de format canonique manipulé par l'analyse pandas.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import List, Optional, Union

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Nature de la date, importante pour interpréter les tarifs."""

    UNIQUE = "date_unique"
    TOURNEE = "tournee"
    FESTIVAL = "festival"
    RESIDENCE = "residence"
    AUTRE = "autre"


class PriceConfidence(str, Enum):
    """Niveau de confiance sur la grille tarifaire récupérée.

    C'est le point dur du projet : on horodate et on qualifie chaque prix
    plutôt que de prétendre à une exhaustivité qu'on n'a pas.
    """

    COMPLETE = "grille_complete"   # toutes les catégories + prix trouvés
    PARTIAL = "grille_partielle"   # certaines catégories seulement
    FROM_PRICE = "prix_a_partir_de"  # uniquement un « à partir de X€ »
    NONE = "aucun_prix"            # rien de fiable trouvé


class Setting(str, Enum):
    """Cadre d'implantation d'un festival (critère clé de comparabilité)."""

    URBAIN = "urbain"
    PERIURBAIN = "periurbain"
    RURAL = "rural"
    INCONNU = "inconnu"


class PriceCategory(BaseModel):
    """Un tarif pour une catégorie de place (concert) ou un type de pass (festival)."""

    name: str = Field(
        description="Nom de la catégorie : Carré Or, Fosse, Cat. 1… ou type de pass : Pass 1 jour, Pass 3 jours, VIP…"
    )
    price: float = Field(description="Prix affiché pour cette catégorie.")
    currency: str = Field(default="EUR", description="Devise ISO (EUR, GBP, CHF…).")
    fees_included: Optional[bool] = Field(
        default=None,
        description="True si le prix inclut les frais de location, False sinon, None si inconnu.",
    )


def normalize_url(url: Optional[str]) -> Optional[str]:
    """URL exploitable ou None. Ajoute `https://` aux domaines nus
    (« garorock.com », constaté en conditions réelles : le navigateur les
    refusait et elles échappaient aux contrôles de domaine)."""
    if not url:
        return None
    u = url.strip().strip("<>\"'")
    if not u or any(c.isspace() for c in u):
        return None
    if u.startswith("//"):
        u = "https:" + u
    if not re.match(r"^https?://", u, re.I):
        if not re.match(r"^[\w.-]+\.[a-z]{2,}(?:[/?#]|$)", u, re.I):
            return None
        u = "https://" + u
    return u


def normalize_phase(value: Optional[str]) -> Optional[str]:
    """Ramène une phase de prix libre à : early bird, normal, dernière minute, sur place."""
    if not value or not value.strip():
        return None
    v = value.strip().lower()
    if any(k in v for k in ("early", "prévente", "prevente", "pré-vente", "blind", "lancement")):
        return "early bird"
    if any(k in v for k in ("dernière", "derniere", "last")):
        return "dernière minute"
    if any(k in v for k in ("sur place", "guichet", "door")):
        return "sur place"
    if any(k in v for k in ("normal", "plein", "standard", "régulier", "regulier")):
        return "normal"
    return None


def prices_to_eur(prices: List[PriceCategory], fx: dict[str, float]) -> List[float]:
    """Convertit une grille en EUR (les devises sans taux connu sont ignorées)."""
    out: List[float] = []
    for pc in prices:
        rate = fx.get(pc.currency.upper())
        if rate is not None:
            out.append(round(pc.price * rate, 2))
    return out


class Concert(BaseModel):
    """Une date de concert trouvée et (idéalement) tarifée."""

    artist: str = Field(description="Nom de l'artiste ou du groupe principal.")
    date: Optional[str] = Field(
        default=None,
        description="Date de l'événement au format ISO (YYYY-MM-DD) si connue.",
    )
    venue: Optional[str] = Field(default=None, description="Nom de la salle / du lieu.")
    city: Optional[str] = Field(default=None, description="Ville.")
    country: Optional[str] = Field(default=None, description="Pays.")
    capacity: Optional[int] = Field(
        default=None, description="Jauge de la salle si connue (nombre de places)."
    )
    genre: Optional[str] = Field(default=None, description="Style musical dominant.")
    event_type: EventType = Field(
        default=EventType.AUTRE, description="Nature de la date."
    )
    source_url: Optional[str] = Field(
        default=None, description="URL de la page billetterie de référence."
    )
    prices: List[PriceCategory] = Field(
        default_factory=list, description="Grille tarifaire par catégorie."
    )
    price_confidence: PriceConfidence = Field(
        default=PriceConfidence.NONE,
        description="Qualité de la grille tarifaire récupérée.",
    )
    priced_at: Optional[str] = Field(
        default=None,
        description="Horodatage ISO (UTC) de la récupération des prix.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Infos utiles : early bird, dynamic pricing, guest, complet, etc.",
    )
    price_phase: Optional[str] = Field(
        default=None, description="Phase de prix de la grille : early bird, normal, dernière minute, sur place."
    )
    comparability: Optional[int] = Field(
        default=None, description="Score de comparabilité avec la cible (0-100)."
    )
    comparability_detail: Optional[str] = Field(default=None, description="Détail du score.")
    anomalies: List[str] = Field(
        default_factory=list, description="Incohérences détectées dans la grille (signalées, pas corrigées)."
    )
    estimated_fields: List[str] = Field(
        default_factory=list, description="Champs estimés plutôt que trouvés tels quels (capacity…)."
    )
    from_base: bool = Field(
        default=False, description="Événement repris de la base de référence (non réextrait dans ce run)."
    )

    # --- Helpers --------------------------------------------------------------
    def prices_eur(self, fx: dict[str, float]) -> List[float]:
        """Prix de toutes les catégories convertis en EUR."""
        return prices_to_eur(self.prices, fx)

    def search_label(self) -> str:
        """Description de l'événement pour les recherches ciblées (URL, prix)."""
        return (
            f"Concert — Artiste : {self.artist} | Salle : {self.venue or '?'} | "
            f"Ville : {self.city or '?'} | Date : {self.date or '?'}"
        )

    def short_label(self) -> str:
        return f"{self.artist} — {self.venue or '?'}"

    def sort_date(self) -> Optional[str]:
        return self.date


class ConcertList(BaseModel):
    """Enveloppe pour un ensemble de `Concert` (usage interne / round-trip)."""

    concerts: List[Concert] = Field(default_factory=list)


class DiscoveredConcert(BaseModel):
    """Schéma minimal renvoyé par la découverte (sortie structurée légère).

    Volontairement plat et sans prix : la sortie structurée du LLM a une limite
    de complexité, donc on garde ce schéma le plus simple possible, puis on le
    convertit en `Concert` complet côté code.
    """

    artist: str = Field(description="Nom de l'artiste ou du groupe principal.")
    date: Optional[str] = Field(default=None, description="Date ISO YYYY-MM-DD.")
    venue: Optional[str] = Field(default=None, description="Nom de la salle / du lieu.")
    city: Optional[str] = Field(default=None, description="Ville.")
    country: Optional[str] = Field(default=None, description="Pays.")
    genre: Optional[str] = Field(default=None, description="Style musical dominant.")
    event_type: Optional[str] = Field(
        default=None, description="Nature : date unique, tournée, festival…"
    )
    capacity: Optional[int] = Field(default=None, description="Jauge de la salle.")
    capacity_estimated: bool = Field(
        description="True si la jauge est la capacité habituelle de la salle (non lue dans les résultats)."
    )
    source_url: Optional[str] = Field(
        default=None, description="URL de la page billetterie."
    )


class DiscoveredList(BaseModel):
    """Enveloppe pour la sortie structurée de la découverte."""

    concerts: List[DiscoveredConcert] = Field(default_factory=list)


_EVENT_TYPE_KEYWORDS = {
    EventType.FESTIVAL: ["festival"],
    EventType.TOURNEE: ["tourn", "tour"],
    EventType.UNIQUE: ["unique", "exceptionnel"],
    EventType.RESIDENCE: ["residence", "résidence"],
}


def coerce_event_type(value: Optional[str]) -> EventType:
    """Mappe une chaîne libre vers un `EventType` (best effort)."""
    if not value:
        return EventType.AUTRE
    v = value.strip().lower()
    for et in EventType:
        if v == et.value:
            return et
    for et, kws in _EVENT_TYPE_KEYWORDS.items():
        if any(k in v for k in kws):
            return et
    return EventType.AUTRE


def discovered_to_concert(d: DiscoveredConcert) -> Concert:
    """Convertit un `DiscoveredConcert` (léger) en `Concert` complet."""
    return Concert(
        artist=d.artist,
        date=d.date,
        venue=d.venue,
        city=d.city,
        country=d.country,
        capacity=d.capacity,
        genre=d.genre,
        event_type=coerce_event_type(d.event_type),
        source_url=normalize_url(d.source_url),
        estimated_fields=["capacity"] if (d.capacity and d.capacity_estimated) else [],
    )


class PriceExtraction(BaseModel):
    """Sortie structurée de la phase d'extraction tarifaire d'une date."""

    prices: List[PriceCategory] = Field(default_factory=list)
    price_confidence: PriceConfidence = Field(default=PriceConfidence.NONE)
    notes: Optional[str] = Field(default=None)
    edition: str = Field(
        description="Année / édition à laquelle s'applique la grille affichée (ex. 2027), chaîne vide si inconnue."
    )
    phase: str = Field(
        description="Phase de prix de la grille retenue : early bird, normal, dernière minute, sur place, ou chaîne vide."
    )


class SearchCriteria(BaseModel):
    """Critères de recherche fournis par l'utilisateur."""

    reference_artist: Optional[str] = Field(
        default=None,
        description=(
            "Artiste de référence : l'outil recherche des artistes comparables "
            "(même style, notoriété et taille de salle) pour orienter la découverte, "
            "plutôt que de se fier uniquement à 'genres'."
        ),
    )
    target_artists: List[str] = Field(
        default_factory=list,
        description=(
            "Liste résolue d'artistes à rechercher (renseignée automatiquement à "
            "partir de reference_artist ; peut aussi être fournie directement)."
        ),
    )
    reference_capacity: Optional[int] = Field(
        default=None,
        description="Jauge habituelle des salles de l'artiste de référence (résolue automatiquement).",
    )
    genres: List[str] = Field(
        default_factory=list, description="Styles musicaux ciblés."
    )
    location: Optional[str] = Field(
        default=None,
        description="Zone géographique : ville, région, pays (ex. 'France', 'Paris').",
    )
    countries: List[str] = Field(
        default_factory=lambda: ["France"],
        description="Pays à couvrir, France en priorité.",
    )
    capacity_min: Optional[int] = Field(default=None, description="Jauge minimale.")
    capacity_max: Optional[int] = Field(default=None, description="Jauge maximale.")
    date_from: Optional[str] = Field(
        default=None, description="Début de période (ISO YYYY-MM-DD)."
    )
    date_to: Optional[str] = Field(
        default=None, description="Fin de période (ISO YYYY-MM-DD)."
    )
    max_results: int = Field(default=15, description="Nombre de dates visées.")
    extra: Optional[str] = Field(
        default=None, description="Contraintes libres additionnelles."
    )

    def to_prompt(self) -> str:
        """Rend les critères sous forme de texte pour le prompt de découverte."""
        parts: List[str] = []
        if self.target_artists:
            parts.append(f"- Artistes ciblés (rechercher CES artistes précisément) : {', '.join(self.target_artists)}")
        if self.genres:
            parts.append(f"- Styles musicaux : {', '.join(self.genres)}")
        if self.location:
            parts.append(f"- Zone géographique : {self.location}")
        if self.countries:
            parts.append(f"- Pays (France prioritaire) : {', '.join(self.countries)}")
        if self.capacity_min or self.capacity_max:
            lo = self.capacity_min if self.capacity_min is not None else "?"
            hi = self.capacity_max if self.capacity_max is not None else "?"
            parts.append(f"- Jauge de salle : entre {lo} et {hi} places")
        if self.date_from or self.date_to:
            parts.append(
                f"- Période : du {self.date_from or '?'} au {self.date_to or '?'}"
            )
        parts.append(f"- Nombre de dates visées : {self.max_results}")
        if self.extra:
            parts.append(f"- Contraintes additionnelles : {self.extra}")
        return "\n".join(parts)


class SimilarArtistsResult(BaseModel):
    """Sortie structurée de la résolution d'artistes comparables."""

    reference_artist: str = Field(description="Artiste de référence fourni par l'utilisateur.")
    inferred_genre: Optional[str] = Field(
        default=None, description="Style musical dominant de l'artiste de référence."
    )
    similar_artists: List[str] = Field(
        default_factory=list,
        description="Artistes comparables (même style, notoriété, taille de salle). Ne pas inclure l'artiste de référence.",
    )
    reference_capacity: int = Field(
        description="Jauge typique des salles que remplit actuellement l'artiste de référence ; 0 si inconnue."
    )


# =============================================================================
# Mode Festival
# =============================================================================
class Festival(BaseModel):
    """Une édition de festival trouvée et (idéalement) tarifée."""

    name: str = Field(description="Nom du festival.")
    edition: Optional[str] = Field(default=None, description="Édition / année (ex. 2026).")
    date_start: Optional[str] = Field(default=None, description="Premier jour (ISO YYYY-MM-DD).")
    date_end: Optional[str] = Field(default=None, description="Dernier jour (ISO YYYY-MM-DD).")
    city: Optional[str] = Field(default=None, description="Commune d'implantation.")
    region: Optional[str] = Field(default=None, description="Région / département.")
    country: Optional[str] = Field(default=None, description="Pays.")
    setting: Setting = Field(default=Setting.INCONNU, description="Cadre : urbain, périurbain, rural.")
    capacity: Optional[int] = Field(
        default=None, description="Jauge JOURNALIÈRE (festivaliers par jour), pas la fréquentation cumulée."
    )
    genres: Optional[str] = Field(default=None, description="Programmation dominante (styles).")
    headliners: List[str] = Field(default_factory=list, description="Têtes d'affiche de l'édition.")
    nb_nights: Optional[int] = Field(default=None, description="Nombre de soirs / jours de festival.")
    artists_per_night: Optional[int] = Field(default=None, description="Nombre moyen d'artistes par soir.")
    nb_artists_total: Optional[int] = Field(default=None, description="Nombre total d'artistes programmés.")
    source_url: Optional[str] = Field(
        default=None, description="URL de la page billetterie / tarifs de référence."
    )
    prices: List[PriceCategory] = Field(
        default_factory=list, description="Grille tarifaire par type de pass."
    )
    price_confidence: PriceConfidence = Field(
        default=PriceConfidence.NONE, description="Qualité de la grille tarifaire récupérée."
    )
    priced_at: Optional[str] = Field(
        default=None, description="Horodatage ISO (UTC) de la récupération des prix."
    )
    notes: Optional[str] = Field(
        default=None, description="Infos utiles : early bird, sold out, phases de prix, etc."
    )
    price_phase: Optional[str] = Field(
        default=None, description="Phase de prix de la grille : early bird, normal, dernière minute, sur place."
    )
    comparability: Optional[int] = Field(
        default=None, description="Score de comparabilité avec la cible (0-100)."
    )
    comparability_detail: Optional[str] = Field(default=None, description="Détail du score.")
    anomalies: List[str] = Field(
        default_factory=list, description="Incohérences détectées dans la grille (signalées, pas corrigées)."
    )
    estimated_fields: List[str] = Field(
        default_factory=list, description="Champs estimés plutôt que trouvés tels quels (capacity…)."
    )
    from_base: bool = Field(
        default=False, description="Événement repris de la base de référence (non réextrait dans ce run)."
    )

    def prices_eur(self, fx: dict[str, float]) -> List[float]:
        return prices_to_eur(self.prices, fx)

    def search_label(self) -> str:
        dates = f"{self.date_start or '?'} → {self.date_end or '?'}"
        return (
            f"Festival — Nom : {self.name} | Édition : {self.edition or '?'} | "
            f"Ville : {self.city or '?'} | Dates : {dates}"
        )

    def short_label(self) -> str:
        return f"{self.name} {self.edition or ''}".strip()

    def sort_date(self) -> Optional[str]:
        return self.date_start


PricedItem = Union[Concert, Festival]


class DiscoveredFestival(BaseModel):
    """Schéma minimal renvoyé par la découverte de festivals.

    Tous les champs sont OBLIGATOIRES et de type simple (chaîne vide / 0 si
    inconnu) : chaque champ optionnel compte comme une union « valeur ou
    null » dans la limite de complexité de la sortie structurée, et 14 champs
    optionnels la dépassaient (« Schema is too complex », constaté en
    conditions réelles). Converti en `Festival` côté code (vide/0 -> None).
    """

    name: str = Field(description="Nom du festival.")
    edition: str = Field(description="Édition / année, ou chaîne vide si inconnue.")
    date_start: str = Field(description="Premier jour ISO YYYY-MM-DD, ou chaîne vide.")
    date_end: str = Field(description="Dernier jour ISO YYYY-MM-DD, ou chaîne vide.")
    city: str = Field(description="Commune, ou chaîne vide.")
    region: str = Field(description="Région / département, ou chaîne vide.")
    country: str = Field(description="Pays, ou chaîne vide.")
    setting: str = Field(description="urbain, periurbain ou rural ; chaîne vide si inconnu.")
    capacity: int = Field(description="Jauge journalière ; 0 si inconnue.")
    capacity_estimated: bool = Field(
        description="True si la jauge est estimée (ex. fréquentation cumulée divisée par le nombre de jours)."
    )
    genres: str = Field(description="Programmation dominante, ou chaîne vide.")
    headliners: str = Field(description="Têtes d'affiche séparées par des virgules, ou chaîne vide.")
    nb_nights: int = Field(description="Nombre de soirs / jours ; 0 si inconnu.")
    artists_per_night: int = Field(description="Artistes par soir ; 0 si inconnu.")
    artists_per_night_estimated: bool = Field(
        description="True si le nombre d'artistes par soir est estimé plutôt que compté sur la programmation."
    )
    source_url: str = Field(description="URL billetterie / tarifs, ou chaîne vide.")


class DiscoveredFestivalList(BaseModel):
    festivals: List[DiscoveredFestival] = Field(default_factory=list)


def coerce_setting(value: Optional[str]) -> Setting:
    """Mappe une chaîne libre vers un `Setting` (best effort)."""
    if not value:
        return Setting.INCONNU
    v = value.strip().lower().replace("é", "e").replace("-", "")
    # "periurbain" contient "urbain" : à tester en premier.
    if any(k in v for k in ("periurbain", "banlieue", "peri urbain")):
        return Setting.PERIURBAIN
    if any(k in v for k in ("rural", "campagne")):
        return Setting.RURAL
    if any(k in v for k in ("urbain", "ville", "centre")):
        return Setting.URBAIN
    return Setting.INCONNU


def discovered_to_festival(d: DiscoveredFestival) -> Festival:
    def text(v: str) -> Optional[str]:
        return v.strip() or None

    def num(v: int) -> Optional[int]:
        return v if v and v > 0 else None

    headliners = [h.strip() for h in re.split(r"[,;/]", d.headliners) if h.strip()]
    return Festival(
        name=d.name,
        edition=text(d.edition),
        date_start=text(d.date_start),
        date_end=text(d.date_end),
        city=text(d.city),
        region=text(d.region),
        country=text(d.country),
        setting=coerce_setting(d.setting),
        capacity=num(d.capacity),
        genres=text(d.genres),
        headliners=headliners[:10],
        nb_nights=num(d.nb_nights),
        artists_per_night=num(d.artists_per_night),
        source_url=normalize_url(d.source_url),
        estimated_fields=[
            f for f, est in (("capacity", d.capacity_estimated), ("artists_per_night", d.artists_per_night_estimated))
            if est and getattr(d, f)
        ],
    )


SETTING_LABELS = {"urbain": "urbain", "periurbain": "périurbain", "rural": "rural"}


class FestivalCriteria(BaseModel):
    """Critères de recherche propres aux festivals."""

    reference_festival: Optional[str] = Field(
        default=None,
        description=(
            "Festival de référence : l'outil recherche des festivals comparables "
            "(jauge, cadre, programmation, format) pour orienter la découverte."
        ),
    )
    target_festivals: List[str] = Field(
        default_factory=list,
        description="Liste résolue de festivals à rechercher (depuis reference_festival).",
    )
    # Profil chiffré du festival de référence (résolu automatiquement) : cible
    # du score de comparabilité quand aucun critère explicite n'est donné.
    reference_capacity: Optional[int] = Field(default=None)
    reference_setting: Optional[str] = Field(default=None)
    reference_nights: Optional[int] = Field(default=None)
    reference_artists_per_night: Optional[int] = Field(default=None)
    genres: List[str] = Field(default_factory=list, description="Programmation ciblée (styles).")
    location: Optional[str] = Field(default=None, description="Zone géographique.")
    countries: List[str] = Field(default_factory=lambda: ["France"])
    settings: List[str] = Field(
        default_factory=list, description="Cadres acceptés : urbain, periurbain, rural (vide = tous)."
    )
    capacity_min: Optional[int] = Field(default=None, description="Jauge journalière minimale.")
    capacity_max: Optional[int] = Field(default=None, description="Jauge journalière maximale.")
    nights_min: Optional[int] = Field(default=None, description="Nombre de soirs minimal.")
    nights_max: Optional[int] = Field(default=None, description="Nombre de soirs maximal.")
    artists_per_night_min: Optional[int] = Field(default=None)
    artists_per_night_max: Optional[int] = Field(default=None)
    date_from: Optional[str] = Field(default=None, description="Début de période (ISO).")
    date_to: Optional[str] = Field(default=None, description="Fin de période (ISO).")
    max_results: int = Field(default=10, description="Nombre de festivals visés.")
    extra: Optional[str] = Field(default=None, description="Contraintes libres additionnelles.")

    def to_prompt(self) -> str:
        parts: List[str] = []
        if self.target_festivals:
            parts.append(
                f"- Festivals ciblés (rechercher CES festivals précisément) : {', '.join(self.target_festivals)}"
            )
        if self.genres:
            parts.append(f"- Programmation : {', '.join(self.genres)}")
        if self.location:
            parts.append(f"- Zone géographique : {self.location}")
        if self.countries:
            parts.append(f"- Pays (France prioritaire) : {', '.join(self.countries)}")
        if self.settings:
            labels = [SETTING_LABELS.get(s, s) for s in self.settings]
            parts.append(f"- Cadre d'implantation : {' ou '.join(labels)}")
        for label, lo, hi, unit in (
            ("Jauge journalière", self.capacity_min, self.capacity_max, "festivaliers/jour"),
            ("Nombre de soirs", self.nights_min, self.nights_max, "soirs"),
            ("Artistes par soir", self.artists_per_night_min, self.artists_per_night_max, "artistes"),
        ):
            if lo is not None or hi is not None:
                parts.append(f"- {label} : entre {lo if lo is not None else '?'} et {hi if hi is not None else '?'} {unit}")
        if self.date_from or self.date_to:
            parts.append(f"- Période : du {self.date_from or '?'} au {self.date_to or '?'}")
        parts.append(f"- Nombre de festivals visés : {self.max_results}")
        if self.extra:
            parts.append(f"- Contraintes additionnelles : {self.extra}")
        return "\n".join(parts)


class SimilarFestivalsResult(BaseModel):
    """Sortie structurée de la résolution de festivals comparables."""

    reference_festival: str = Field(description="Festival de référence fourni par l'utilisateur.")
    inferred_profile: Optional[str] = Field(
        default=None,
        description="Profil du festival de référence en une ligne : cadre, jauge/jour, programmation, nb de soirs.",
    )
    inferred_genre: Optional[str] = Field(default=None, description="Programmation dominante.")
    similar_festivals: List[str] = Field(
        default_factory=list,
        description="Festivals comparables (jauge, cadre, programmation, format). Ne pas inclure la référence.",
    )
    reference_capacity: int = Field(description="Jauge journalière du festival de référence ; 0 si inconnue.")
    reference_setting: str = Field(description="Cadre du festival de référence : urbain, periurbain, rural, ou chaîne vide.")
    reference_nights: int = Field(description="Nombre de soirs du festival de référence ; 0 si inconnu.")
    reference_artists_per_night: int = Field(description="Artistes par soir du festival de référence ; 0 si inconnu.")
