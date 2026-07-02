"""Schéma de données du benchmark (Pydantic v2).

Ces modèles servent à la fois de contrat pour la sortie structurée du LLM
(`messages.parse`) et de format canonique manipulé par l'analyse pandas.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

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


class PriceCategory(BaseModel):
    """Un tarif pour une catégorie de place donnée."""

    name: str = Field(description="Nom de la catégorie : Carré Or, Fosse, Cat. 1, etc.")
    price: float = Field(description="Prix affiché pour cette catégorie.")
    currency: str = Field(default="EUR", description="Devise ISO (EUR, GBP, CHF…).")
    fees_included: Optional[bool] = Field(
        default=None,
        description="True si le prix inclut les frais de location, False sinon, None si inconnu.",
    )


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

    # --- Helpers d'analyse --------------------------------------------------
    def prices_eur(self, fx: dict[str, float]) -> List[float]:
        """Prix de toutes les catégories convertis en EUR."""
        out: List[float] = []
        for pc in self.prices:
            rate = fx.get(pc.currency.upper())
            if rate is not None:
                out.append(round(pc.price * rate, 2))
        return out


class ConcertList(BaseModel):
    """Enveloppe pour la sortie structurée de la phase de découverte."""

    concerts: List[Concert] = Field(default_factory=list)


class PriceExtraction(BaseModel):
    """Sortie structurée de la phase d'extraction tarifaire d'une date."""

    prices: List[PriceCategory] = Field(default_factory=list)
    price_confidence: PriceConfidence = Field(default=PriceConfidence.NONE)
    notes: Optional[str] = Field(default=None)


class SearchCriteria(BaseModel):
    """Critères de recherche fournis par l'utilisateur."""

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
