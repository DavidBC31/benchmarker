"""Score de comparabilité (0-100) d'un événement avec la cible du benchmark.

La découverte ramène parfois des comparables éloignés (constaté : festivals de
32 000 personnes/jour pour une référence à 10-15 000). Plutôt que de les mêler
aveuglément aux statistiques, chaque événement reçoit un score par critère,
pondéré, avec un détail lisible. Sous `COMPARABILITY_MIN`, il est « hors
profil » : affiché, mais écarté des statistiques et de la préconisation.

Cible (par ordre de priorité) : critères explicites de l'utilisateur, puis
profil chiffré de la référence (résolu avec les comparables), puis l'événement
de référence lui-même s'il figure dans les résultats. Un critère sans valeur
d'un côté ou de l'autre n'est ni compté ni pénalisé (signalé comme manquant).
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Iterable, List, Optional

from .models import Concert, Festival, FestivalCriteria, SearchCriteria, Setting, coerce_setting

_CAPACITY_TOLERANCE = 2.5  # score nul à 2,5× hors de la cible
_APN_TOLERANCE = 2.0
_POINT_MARGIN = 1.25  # une cible ponctuelle accepte ±25 % sans pénalité

# La jauge (≈ niveau de notoriété et de budget artistique) détermine largement
# le niveau de prix : elle n'est pas moyennée avec les autres critères mais
# agit en multiplicateur — un festival de 33 000/jour ne devient pas
# comparable à une cible de 12 000/jour parce que son cadre et sa
# programmation correspondent. Les autres critères sont moyennés (pondérés).
_GATE = "jauge"
_GATE_FLOOR = 0.25  # multiplicateur minimal quand la jauge est totalement hors cible
_FESTIVAL_WEIGHTS = {"jauge": 0.0, "cadre": 0.30, "programmation": 0.30, "soirs": 0.20, "artistes/soir": 0.20}
_CONCERT_WEIGHTS = {"jauge": 0.0, "style": 1.0}

_GENRE_SYNONYMS = {
    "hip": "rap", "hop": "rap", "hiphop": "rap", "trap": "rap", "drill": "rap",
    "electronique": "electro", "edm": "electro", "techno": "electro", "house": "electro",
    "variete": "chanson", "francaise": "chanson", "punk": "rock", "metal": "rock",
    "rnb": "rnb", "soul": "rnb", "afro": "afro", "afrobeat": "afro", "afrobeats": "afro",
}
_GENRE_STOPWORDS = {"musique", "musiques", "et", "de", "du", "la", "le", "les", "melodique", "francais", "fr"}


def _genre_tokens(values: Iterable[Optional[str]]) -> set[str]:
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        text = "".join(c for c in unicodedata.normalize("NFKD", v.lower()) if not unicodedata.combining(c))
        for tok in re.split(r"[^a-z]+", text):
            if len(tok) >= 2 and tok not in _GENRE_STOPWORDS:
                out.add(_GENRE_SYNONYMS.get(tok, tok))
    return out


def _range_score(value: Optional[float], lo: Optional[float], hi: Optional[float], tolerance: float) -> Optional[float]:
    """1 dans la fourchette, décroissance logarithmique au-dehors (0 à `tolerance`×)."""
    if not value or (lo is None and hi is None):
        return None
    if lo and value < lo:
        gap = math.log(lo / value)
    elif hi and value > hi:
        gap = math.log(value / hi)
    else:
        return 1.0
    return max(0.0, 1 - gap / math.log(tolerance))


def _point_range(ref: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    return (ref / _POINT_MARGIN, ref * _POINT_MARGIN) if ref else (None, None)


def _nights_score(value: Optional[int], lo: Optional[int], hi: Optional[int]) -> Optional[float]:
    if not value or (lo is None and hi is None):
        return None
    gap = (lo - value) if (lo and value < lo) else (value - hi) if (hi and value > hi) else 0
    return max(0.0, 1 - 0.4 * gap)


def _setting_score(value: Setting, targets: List[str]) -> Optional[float]:
    if value == Setting.INCONNU or not targets:
        return None
    if value.value in targets:
        return 1.0
    # Le périurbain est à mi-chemin de l'urbain et du rural.
    return 0.5 if (value == Setting.PERIURBAIN or "periurbain" in targets) else 0.0


def _genre_score(item_genres: Iterable[Optional[str]], target: Iterable[Optional[str]]) -> Optional[float]:
    t, g = _genre_tokens(target), _genre_tokens(item_genres)
    if not t or not g:
        return None
    # Coefficient de recouvrement : un événement mono-style n'est pas pénalisé
    # face à une cible multi-styles s'il en partage le style.
    return len(t & g) / min(len(t), len(g))


def _fmt(n: Optional[float]) -> str:
    return f"{int(n):,}".replace(",", " ") if n else "?"


def _combine(parts: dict, weights: dict, labels: dict) -> tuple[Optional[int], Optional[str]]:
    scored = {k: v for k, v in parts.items() if v is not None}
    missing = [k for k, v in parts.items() if v is None]
    if not scored:
        return None, None
    gate = scored.get(_GATE)
    others = {k: v for k, v in scored.items() if k != _GATE}
    if others:
        base = sum(weights[k] * v for k, v in others.items()) / sum(weights[k] for k in others)
        score = base * (_GATE_FLOOR + (1 - _GATE_FLOOR) * gate) if gate is not None else base
    else:
        score = gate
    score = round(100 * score)
    marks = []
    for k, v in scored.items():
        symbol = "✓" if v >= 0.8 else "~" if v >= 0.4 else "✗"
        marks.append(f"{k} {symbol}{labels.get(k, '')}")
    detail = " · ".join(marks)
    if missing:
        detail += f" · non évalué : {', '.join(missing)}"
    return score, detail


# --- Festivals --------------------------------------------------------------------
def score_festivals(festivals: List[Festival], criteria: FestivalCriteria) -> None:
    """Renseigne `comparability` / `comparability_detail` sur chaque festival."""
    ref = _find_reference(festivals, criteria.reference_festival, lambda f: f.name)

    cap_lo, cap_hi = criteria.capacity_min, criteria.capacity_max
    if cap_lo is None and cap_hi is None:
        cap_lo, cap_hi = _point_range(criteria.reference_capacity or (ref.capacity if ref else None))
    settings = list(criteria.settings)
    if not settings:
        ref_setting = coerce_setting(criteria.reference_setting) if criteria.reference_setting else (ref.setting if ref else Setting.INCONNU)
        settings = [ref_setting.value] if ref_setting != Setting.INCONNU else []
    n_lo, n_hi = criteria.nights_min, criteria.nights_max
    if n_lo is None and n_hi is None:
        n = criteria.reference_nights or (ref.nb_nights if ref else None)
        n_lo = n_hi = n
    a_lo, a_hi = criteria.artists_per_night_min, criteria.artists_per_night_max
    if a_lo is None and a_hi is None:
        a_lo, a_hi = _point_range(criteria.reference_artists_per_night or (ref.artists_per_night if ref else None))
    genres = list(criteria.genres) or ([ref.genres] if ref and ref.genres else [])

    for f in festivals:
        parts = {
            "jauge": _range_score(f.capacity, cap_lo, cap_hi, _CAPACITY_TOLERANCE),
            "cadre": _setting_score(f.setting, settings),
            "programmation": _genre_score([f.genres], genres),
            "soirs": _nights_score(f.nb_nights, n_lo, n_hi),
            "artistes/soir": _range_score(f.artists_per_night, a_lo, a_hi, _APN_TOLERANCE),
        }
        labels = {"jauge": f" ({_fmt(f.capacity)} vs {_fmt(cap_lo)}–{_fmt(cap_hi)})"} if parts["jauge"] is not None and parts["jauge"] < 0.8 else {}
        f.comparability, f.comparability_detail = _combine(parts, _FESTIVAL_WEIGHTS, labels)


# --- Concerts ---------------------------------------------------------------------
def score_concerts(concerts: List[Concert], criteria: SearchCriteria) -> None:
    """Renseigne `comparability` / `comparability_detail` sur chaque date."""
    cap_lo, cap_hi = criteria.capacity_min, criteria.capacity_max
    if cap_lo is None and cap_hi is None:
        ref_caps = sorted(c.capacity for c in concerts
                          if c.capacity and criteria.reference_artist
                          and c.artist.strip().lower() == criteria.reference_artist.strip().lower())
        ref_cap = criteria.reference_capacity or (ref_caps[len(ref_caps) // 2] if ref_caps else None)
        cap_lo, cap_hi = _point_range(ref_cap)
    genres = list(criteria.genres)

    for c in concerts:
        parts = {
            "jauge": _range_score(c.capacity, cap_lo, cap_hi, _CAPACITY_TOLERANCE),
            "style": _genre_score([c.genre], genres),
        }
        labels = {"jauge": f" ({_fmt(c.capacity)} vs {_fmt(cap_lo)}–{_fmt(cap_hi)})"} if parts["jauge"] is not None and parts["jauge"] < 0.8 else {}
        c.comparability, c.comparability_detail = _combine(parts, _CONCERT_WEIGHTS, labels)


def _find_reference(items, name: Optional[str], key):
    if not name:
        return None
    n = name.strip().lower()
    for it in items:
        k = key(it).strip().lower()
        if k == n or n in k or k in n:
            return it
    return None
