"""Mémoire des grilles tarifaires extraites, par événement.

Relancer un benchmark sur les mêmes artistes / festivals ne repaie pas les
mêmes recherches et pages, et rend le même résultat (cohérence d'un run à
l'autre). Clé = identité de l'événement telle que trouvée à la découverte ;
un fichier JSON par événement (sûr avec l'extraction parallèle). Les grilles
trouvées sont gardées `PRICE_CACHE_TTL_DAYS` jours, les échecs
`PRICE_CACHE_FAIL_TTL_HOURS` heures (un blocage peut n'être que passager) —
et seulement s'ils viennent de la version courante de l'extraction
(`EXTRACTION_VERSION`) : un correctif doit pouvoir retenter l'événement.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config
from .models import Festival, PriceConfidence, PricedItem

_FIELDS = ("source_url", "prices", "price_confidence", "price_phase", "priced_at", "notes")
_FESTIVAL_FIELDS = ("edition", "date_start", "date_end")


def _norm(text: Optional[str]) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


def cache_key(item: PricedItem) -> str:
    """Identité stable de l'événement (à calculer AVANT l'extraction, qui peut
    modifier URL et édition)."""
    if isinstance(item, Festival):
        raw = f"festival|{_norm(item.name)}|{_norm(item.edition)}"
    else:
        raw = f"concert|{_norm(item.artist)}|{_norm(item.date)}|{_norm(item.venue or item.city)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _path(key: str) -> Path:
    return config.CACHE_DIR / f"{key}.json"


def load(key: str) -> Optional[dict]:
    """Payload en cache encore valide, sinon None."""
    try:
        payload = json.loads(_path(key).read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(payload["saved_at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    found = payload["fields"].get("price_confidence") != PriceConfidence.NONE.value
    if not found and payload.get("version") != config.EXTRACTION_VERSION:
        return None
    ttl = timedelta(days=config.PRICE_CACHE_TTL_DAYS) if found else timedelta(hours=config.PRICE_CACHE_FAIL_TTL_HOURS)
    return payload if datetime.now(timezone.utc) - saved <= ttl else None


def store(key: str, item: PricedItem) -> None:
    fields = _FIELDS + (_FESTIVAL_FIELDS if isinstance(item, Festival) else ())
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": config.EXTRACTION_VERSION,
        "fields": item.model_dump(mode="json", include=set(fields)),
    }
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _path(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # le cache est une optimisation : son échec ne doit jamais casser un run


def apply(item: PricedItem, payload: dict) -> None:
    """Applique une grille mémorisée à l'événement (et le signale en note)."""
    # Validation via le modèle pour retrouver les types (PriceCategory, enums…).
    restored = type(item).model_validate({**_base(item), **payload["fields"]})
    for name in payload["fields"]:
        setattr(item, name, getattr(restored, name))
    saved = payload["saved_at"][:10]
    item.notes = f"grille en mémoire (extraite le {saved})" + (f" | {item.notes}" if item.notes else "")


def _base(item: PricedItem) -> dict:
    return {"name": item.name} if isinstance(item, Festival) else {"artist": item.artist}
