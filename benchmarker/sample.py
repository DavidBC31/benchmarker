"""Jeu de données d'exemple (sans appel LLM).

Sert au test d'intégration / smoke test `python -m benchmarker.cli selftest` :
il exerce toute la chaîne non-LLM (analyse, stats, reco, export) et permet de
vérifier l'application sans consommer de crédits API. Les dates sont inspirées
de résultats réels, avec des niveaux de confiance variés (complet, partiel,
à partir de, aucun) pour illustrer le comportement.
"""

from __future__ import annotations

from typing import List

from .models import Concert, EventType, Festival, PriceCategory, PriceConfidence, Setting


def sample_concerts() -> List[Concert]:
    return [
        Concert(
            artist="A$AP Rocky", date="2026-10-15", venue="Accor Arena",
            city="Paris", country="France", capacity=20000, genre="rap",
            event_type=EventType.UNIQUE, source_url="https://example/asap",
            price_confidence=PriceConfidence.COMPLETE, priced_at="2026-07-21T10:00:00+00:00",
            prices=[
                PriceCategory(name="Fosse", price=69.0, fees_included=True),
                PriceCategory(name="Catégorie 1", price=79.0),
                PriceCategory(name="Catégorie 2", price=59.0),
                PriceCategory(name="Carré Or", price=120.0),
            ],
        ),
        Concert(
            artist="Bigflo & Oli", date="2026-11-20", venue="Accor Arena",
            city="Paris", country="France", capacity=20000, genre="rap",
            event_type=EventType.TOURNEE, source_url="https://example/bo1",
            price_confidence=PriceConfidence.COMPLETE, priced_at="2026-07-21T10:00:00+00:00",
            prices=[
                PriceCategory(name="Fosse", price=55.0),
                PriceCategory(name="Gradins", price=45.0),
                PriceCategory(name="Carré Or", price=89.0),
            ],
        ),
        Concert(
            artist="Bigflo & Oli", date="2026-11-21", venue="Accor Arena",
            city="Paris", country="France", capacity=20000, genre="rap",
            event_type=EventType.TOURNEE, source_url="https://example/bo2",
            price_confidence=PriceConfidence.PARTIAL, priced_at="2026-07-21T10:00:00+00:00",
            prices=[PriceCategory(name="Fosse", price=55.0)],
            notes="grille incomplète sur la page (2e date)",
        ),
        Concert(
            artist="Kery James", date="2026-12-05", venue="Accor Arena",
            city="Paris", country="France", capacity=20000, genre="rap",
            event_type=EventType.UNIQUE, source_url="https://example/kery",
            price_confidence=PriceConfidence.COMPLETE, priced_at="2026-07-21T10:00:00+00:00",
            prices=[
                PriceCategory(name="Fosse", price=49.0),
                PriceCategory(name="Gradins", price=39.0),
                PriceCategory(name="Carré Or", price=75.0),
            ],
        ),
        Concert(
            artist="Meryl", date="2026-10-30", venue="Accor Arena",
            city="Paris", country="France", capacity=20000, genre="rap",
            event_type=EventType.UNIQUE, source_url="https://example/meryl",
            price_confidence=PriceConfidence.FROM_PRICE, priced_at="2026-07-21T10:00:00+00:00",
            prices=[PriceCategory(name="À partir de", price=39.0)],
            notes="uniquement un « à partir de » affiché",
        ),
        Concert(
            artist="Kneecap", date="2026-09-30", venue="Zénith Paris – La Villette",
            city="Paris", country="France", capacity=6800, genre="rap",
            event_type=EventType.UNIQUE, source_url="https://example/kneecap",
            price_confidence=PriceConfidence.COMPLETE, priced_at="2026-07-21T10:00:00+00:00",
            prices=[
                PriceCategory(name="Placement debout", price=39.0),
                PriceCategory(name="Gradins", price=45.0),
            ],
        ),
        Concert(
            artist="So la Lune", date="2026-11-08", venue="Accor Arena",
            city="Paris", country="France", capacity=20000, genre="rap",
            event_type=EventType.UNIQUE, source_url=None,
            price_confidence=PriceConfidence.NONE, priced_at="2026-07-21T10:00:00+00:00",
            notes="pas d'URL billetterie trouvée",
        ),
    ]


def sample_festivals() -> List[Festival]:
    """Festivals FICTIFS (noms et prix inventés) pour tester la chaîne festival."""
    pc = PriceCategory
    return [
        Festival(
            name="Festival des Prairies (exemple)", edition="2026",
            date_start="2026-07-10", date_end="2026-07-12", city="Commune A",
            region="Bretagne", country="France", setting=Setting.RURAL, capacity=45000,
            genres="pop, rock, rap", headliners=["Tête d'affiche 1", "Tête d'affiche 2"],
            nb_nights=3, artists_per_night=12, nb_artists_total=36,
            source_url="https://example/prairies", price_confidence=PriceConfidence.COMPLETE,
            prices=[pc(name="Pass 1 jour", price=69.0), pc(name="Pass 2 jours", price=125.0),
                    pc(name="Pass 3 jours", price=169.0), pc(name="Pass VIP 3 jours", price=390.0),
                    pc(name="Option : Camping", price=25.0), pc(name="Pass 1 jour -26 ans", price=55.0)],
        ),
        Festival(
            name="Les Nuits du Bocage (exemple)", edition="2026",
            date_start="2026-08-21", date_end="2026-08-23", city="Commune B",
            region="Normandie", country="France", setting=Setting.RURAL, capacity=25000,
            genres="rap, électro", headliners=["Tête d'affiche 3"],
            nb_nights=3, artists_per_night=10, nb_artists_total=30,
            source_url="https://example/bocage", price_confidence=PriceConfidence.COMPLETE,
            prices=[pc(name="Pass Vendredi", price=59.0), pc(name="Pass Samedi", price=62.0),
                    pc(name="Pass 3 jours", price=139.0), pc(name="Camping", price=20.0)],
        ),
        Festival(
            name="Quais Électriques (exemple)", edition="2026",
            date_start="2026-06-26", date_end="2026-06-27", city="Ville C",
            region="Auvergne-Rhône-Alpes", country="France", setting=Setting.URBAIN, capacity=12000,
            genres="électro", headliners=["Tête d'affiche 4"],
            nb_nights=2, artists_per_night=8, nb_artists_total=16,
            source_url="https://example/quais", price_confidence=PriceConfidence.PARTIAL,
            prices=[pc(name="Pass 1 jour", price=49.0), pc(name="Pass week-end", price=89.0)],
        ),
        Festival(
            name="Rives Sonores (exemple)", edition="2025",
            date_start="2025-09-05", date_end="2025-09-07", city="Commune D",
            region="Occitanie", country="France", setting=Setting.PERIURBAIN, capacity=18000,
            genres="rap, pop", headliners=[], nb_nights=3, artists_per_night=9,
            source_url="https://example/rives", price_confidence=PriceConfidence.FROM_PRICE,
            prices=[pc(name="Pass 1 jour", price=45.0)], notes="uniquement un « à partir de » affiché",
        ),
        Festival(
            name="Festival Sans Grille (exemple)", edition="2026", city="Commune E",
            country="France", setting=Setting.RURAL, capacity=30000, genres="rock", nb_nights=4,
            price_confidence=PriceConfidence.NONE, notes="billetterie pas encore ouverte",
        ),
    ]
