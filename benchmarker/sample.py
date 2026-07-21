"""Jeu de données d'exemple (sans appel LLM).

Sert au test d'intégration / smoke test `python -m benchmarker.cli selftest` :
il exerce toute la chaîne non-LLM (analyse, stats, reco, export) et permet de
vérifier l'application sans consommer de crédits API. Les dates sont inspirées
de résultats réels, avec des niveaux de confiance variés (complet, partiel,
à partir de, aucun) pour illustrer le comportement.
"""

from __future__ import annotations

from typing import List

from .models import Concert, EventType, PriceCategory, PriceConfidence


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
