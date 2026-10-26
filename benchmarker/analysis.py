"""Analyse statistique du benchmark et export.

Transforme une liste de `Concert` (mode Artiste) ou de `Festival` (mode
Festival) en tableaux exploitables (une ligne par tarif) et calcule des
statistiques : min, max, moyenne, médiane, par catégorie / type de pass, etc.
Tout est normalisé en EUR.
"""

from __future__ import annotations

import re
from typing import List, Optional

import pandas as pd

from . import config
from .models import Concert, Festival

# Schéma explicite du DataFrame « long ». Garantit que toutes les colonnes
# existent même quand `concerts` est vide (0 date trouvée) : sans ça,
# `pd.DataFrame([])` n'a aucune colonne et tout accès (`df["price_eur"]`)
# lève un KeyError en aval (summarize, recommend…).
_FRAME_COLUMNS = [
    "artist", "date", "venue", "city", "country", "capacity", "genre",
    "event_type", "source_url", "price_confidence", "priced_at", "notes",
    "category", "price", "currency", "price_eur", "fees_included",
]


def concerts_to_frame(concerts: List[Concert]) -> pd.DataFrame:
    """Aplati les concerts en un DataFrame « long » : une ligne par tarif.

    Les dates sans prix apparaissent tout de même (price NaN) pour garder une
    trace de la couverture. Renvoie toujours les mêmes colonnes, y compris
    pour une liste vide.
    """
    rows: list[dict] = []
    for c in concerts:
        base = {
            "artist": c.artist,
            "date": c.date,
            "venue": c.venue,
            "city": c.city,
            "country": c.country,
            "capacity": c.capacity,
            "genre": c.genre,
            "event_type": c.event_type.value,
            "source_url": c.source_url,
            "price_confidence": c.price_confidence.value,
            "priced_at": c.priced_at,
            "notes": c.notes,
        }
        if not c.prices:
            rows.append({**base, "category": None, "price": None,
                         "currency": None, "price_eur": None, "fees_included": None})
            continue
        for pc in c.prices:
            rate = config.FX_TO_EUR.get(pc.currency.upper())
            price_eur = round(pc.price * rate, 2) if rate is not None else None
            rows.append({
                **base,
                "category": pc.name,
                "price": pc.price,
                "currency": pc.currency,
                "price_eur": price_eur,
                "fees_included": pc.fees_included,
            })
    return pd.DataFrame(rows, columns=_FRAME_COLUMNS)


def _describe(series: pd.Series) -> dict:
    """Statistiques de base sur une série de prix (en EUR)."""
    s = series.dropna()
    if s.empty:
        return {"count": 0}
    return {
        "count": int(s.count()),
        "min": round(float(s.min()), 2),
        "max": round(float(s.max()), 2),
        "mean": round(float(s.mean()), 2),
        "median": round(float(s.median()), 2),
        "std": round(float(s.std()), 2) if s.count() > 1 else 0.0,
    }


def summarize(df: pd.DataFrame) -> dict:
    """Statistiques globales, par catégorie et par style (prix EUR)."""
    priced = df[df["price_eur"].notna()]
    # Nombre d'événements distincts (une date sans URL compte quand même).
    n_dates = df.groupby(["artist", "date", "venue"], dropna=False).ngroups

    by_category = {
        str(cat): _describe(grp["price_eur"])
        for cat, grp in priced.groupby("category", dropna=True)
    }
    by_genre = {
        str(genre): _describe(grp["price_eur"])
        for genre, grp in priced.groupby("genre", dropna=True)
    }

    # Une date par événement (et non une par ligne de tarif, sinon une grille
    # de 5 catégories compterait 5 fois dans la couverture).
    coverage = (
        df.drop_duplicates(["artist", "date", "venue"])
        .groupby("price_confidence")["price_confidence"].count().to_dict()
    )

    return {
        "dates_trouvees": int(n_dates),
        "tarifs_collectes": int(priced.shape[0]),
        "global": _describe(priced["price_eur"]),
        "par_categorie": by_category,
        "par_style": by_genre,
        "couverture_prix": {str(k): int(v) for k, v in coverage.items()},
    }


def summary_to_markdown(summary: dict) -> str:
    """Rend le résumé statistique en Markdown lisible."""
    lines: list[str] = ["# Benchmark billetterie — synthèse\n"]
    lines.append(f"- Dates trouvées : **{summary['dates_trouvees']}**")
    lines.append(f"- Tarifs collectés : **{summary['tarifs_collectes']}**\n")

    g = summary["global"]
    if g.get("count"):
        lines.append("## Global (EUR)\n")
        lines.append(f"- Min : {g['min']} | Max : {g['max']} | "
                     f"Moyenne : {g['mean']} | Médiane : {g['median']} | Écart-type : {g['std']}\n")

    if summary["par_categorie"]:
        lines.append("## Par catégorie de place (EUR)\n")
        lines.append("| Catégorie | N | Min | Max | Moyenne | Médiane |")
        lines.append("|---|---|---|---|---|---|")
        for cat, s in summary["par_categorie"].items():
            if s.get("count"):
                lines.append(
                    f"| {cat} | {s['count']} | {s['min']} | {s['max']} | {s['mean']} | {s['median']} |"
                )
        lines.append("")

    if summary["par_style"]:
        lines.append("## Par style musical (EUR)\n")
        lines.append("| Style | N | Min | Max | Moyenne | Médiane |")
        lines.append("|---|---|---|---|---|---|")
        for genre, s in summary["par_style"].items():
            if s.get("count"):
                lines.append(
                    f"| {genre} | {s['count']} | {s['min']} | {s['max']} | {s['mean']} | {s['median']} |"
                )
        lines.append("")

    lines.append("## Couverture des prix\n")
    for conf, n in summary["couverture_prix"].items():
        lines.append(f"- {conf} : {n}")

    return "\n".join(lines)


# =============================================================================
# Mode Festival
# =============================================================================
# Les noms de pass sont très hétérogènes d'un festival à l'autre (« Pass
# Samedi », « Forfait 3J », « Pass intégral », « Camping »…) : on les ramène à
# un type normalisé (Pass N jours / VIP / Option) pour pouvoir les comparer, et
# on dérive un prix par jour et un prix par artiste vu.
_WEEKDAYS = (
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_DAYS_RE = re.compile(r"(\d+)\s*(?:jours?|j\b|soirs?|soir[ée]es?|days?)", re.I)
_FULL_RE = re.compile(r"int[ée]gral|complet|\bfull\b|tous les jours|all days|pass festival", re.I)
_WEEKEND_RE = re.compile(r"week-?end", re.I)
_ONE_DAY_RE = re.compile(r"journ[ée]e|pass jour|day pass|\bsoir[ée]e\b|\bjour\b|\bday\b", re.I)
_VIP_RE = re.compile(r"\bvip\b|premium|golden|backstage|lounge|confort|carr[ée] or", re.I)
_OPTION_RE = re.compile(r"camping|parking|navette|consigne|glamping|caravane|tente", re.I)
_REDUCED_RE = re.compile(
    r"r[ée]duit|[ée]tudiant|enfant|jeune|junior|solidaire|ch[ôo]meur|pmr|-\s?\d+\s?ans|moins de \d+ ans", re.I
)

PASS_OPTION = "Option (camping, parking…)"
PASS_VIP = "VIP / Premium"
PASS_REDUCED = "Tarif réduit"
PASS_OTHER = "Autre"


def is_option(name: str) -> bool:
    """Option vendue à part (camping, parking…), exclue des stats de pass."""
    n = name.strip().lower()
    if n.startswith("option"):
        return True
    return bool(_OPTION_RE.search(n)) and "pass" not in n and "billet" not in n


def pass_days(name: str, nb_nights: Optional[int]) -> Optional[int]:
    """Nombre de jours couverts par un pass, déduit de son nom (best effort)."""
    n = name.lower()
    m = _DAYS_RE.search(n)
    if m:
        return int(m.group(1))
    named_days = sum(1 for d in _WEEKDAYS if d in n)
    if named_days:
        return named_days
    if _FULL_RE.search(n):
        return nb_nights
    if _WEEKEND_RE.search(n):
        return min(2, nb_nights) if nb_nights else 2
    if _ONE_DAY_RE.search(n):
        return 1
    return None


def pass_type(name: str, nb_nights: Optional[int]) -> str:
    if is_option(name):
        return PASS_OPTION
    if _VIP_RE.search(name):
        return PASS_VIP
    if _REDUCED_RE.search(name):
        return PASS_REDUCED
    d = pass_days(name, nb_nights)
    if d == 1:
        return "Pass 1 jour"
    if d:
        return f"Pass {d} jours"
    return PASS_OTHER


def _pass_type_order(label: str) -> tuple:
    m = re.match(r"Pass (\d+) jour", label)
    if m:
        return (0, int(m.group(1)))
    return ({PASS_VIP: 1, PASS_REDUCED: 2, PASS_OPTION: 3}.get(label, 4), 0)


_FEST_COLUMNS = [
    "festival", "edition", "date_start", "date_end", "city", "region", "country",
    "setting", "capacity", "genres", "headliners", "nb_nights", "artists_per_night",
    "nb_artists_total", "source_url", "price_confidence", "priced_at", "notes",
    "category", "pass_type", "pass_days", "is_option", "price", "currency",
    "price_eur", "fees_included", "price_per_day_eur", "price_per_artist_eur",
]


def festivals_to_frame(festivals: List[Festival]) -> pd.DataFrame:
    """Aplati les festivals en DataFrame « long » : une ligne par tarif.

    `price_per_day_eur` = prix / nombre de jours du pass ; `price_per_artist_eur`
    = prix par jour / artistes par soir (coût d'un artiste vu, indicateur clé
    pour comparer des formats différents).
    """
    rows: list[dict] = []
    for f in festivals:
        base = {
            "festival": f.name,
            "edition": f.edition,
            "date_start": f.date_start,
            "date_end": f.date_end,
            "city": f.city,
            "region": f.region,
            "country": f.country,
            "setting": f.setting.value,
            "capacity": f.capacity,
            "genres": f.genres,
            "headliners": ", ".join(f.headliners),
            "nb_nights": f.nb_nights,
            "artists_per_night": f.artists_per_night,
            "nb_artists_total": f.nb_artists_total,
            "source_url": f.source_url,
            "price_confidence": f.price_confidence.value,
            "priced_at": f.priced_at,
            "notes": f.notes,
        }
        if not f.prices:
            rows.append({**base, **{k: None for k in _FEST_COLUMNS if k not in base}})
            continue
        for pc in f.prices:
            rate = config.FX_TO_EUR.get(pc.currency.upper())
            price_eur = round(pc.price * rate, 2) if rate is not None else None
            option = is_option(pc.name)
            days = None if option else pass_days(pc.name, f.nb_nights)
            per_day = round(price_eur / days, 2) if (price_eur is not None and days) else None
            per_artist = (
                round(per_day / f.artists_per_night, 2)
                if (per_day is not None and f.artists_per_night) else None
            )
            rows.append({
                **base,
                "category": pc.name,
                "pass_type": pass_type(pc.name, f.nb_nights),
                "pass_days": days,
                "is_option": option,
                "price": pc.price,
                "currency": pc.currency,
                "price_eur": price_eur,
                "fees_included": pc.fees_included,
                "price_per_day_eur": per_day,
                "price_per_artist_eur": per_artist,
            })
    return pd.DataFrame(rows, columns=_FEST_COLUMNS)


_CAPACITY_BANDS = [
    (0, 5000, "< 5 000 / jour"),
    (5000, 15000, "5 000 – 15 000 / jour"),
    (15000, 40000, "15 000 – 40 000 / jour"),
    (40000, float("inf"), "> 40 000 / jour"),
]


def capacity_band(capacity) -> Optional[str]:
    if capacity is None or pd.isna(capacity):
        return None
    for lo, hi, label in _CAPACITY_BANDS:
        if lo <= capacity < hi:
            return label
    return None


def per_day_base(df: pd.DataFrame) -> pd.DataFrame:
    """Lignes comparables en prix/jour : pass plein tarif datés (hors options,
    VIP et tarifs réduits, qui fausseraient la comparaison)."""
    return df[
        df["price_per_day_eur"].notna()
        & ~df["is_option"].fillna(False).astype(bool)
        & ~df["pass_type"].isin([PASS_VIP, PASS_REDUCED])
    ]


def summarize_festivals(df: pd.DataFrame) -> dict:
    """Statistiques festival : par type de pass, prix/jour, prix/artiste, cadre, jauge."""
    priced = df[df["price_eur"].notna()]
    main = priced[~priced["is_option"].fillna(False).astype(bool)]
    base = per_day_base(priced)
    n_fest = df.groupby(["festival", "edition"], dropna=False).ngroups

    by_type = {
        str(t): _describe(grp["price_eur"])
        for t, grp in sorted(priced.groupby("pass_type", dropna=True), key=lambda kv: _pass_type_order(kv[0]))
    }
    by_setting = {
        str(st): _describe(grp["price_per_day_eur"])
        for st, grp in base.groupby("setting", dropna=True)
    }
    banded = base.assign(_band=base["capacity"].map(capacity_band)).dropna(subset=["_band"])
    band_order = {label: i for i, (_, _, label) in enumerate(_CAPACITY_BANDS)}
    by_capacity = {
        str(b): _describe(grp["price_per_day_eur"])
        for b, grp in sorted(banded.groupby("_band"), key=lambda kv: band_order[kv[0]])
    }
    coverage = (
        df.drop_duplicates(["festival", "edition"])
        .groupby("price_confidence")["price_confidence"].count().to_dict()
    )
    return {
        "mode": "festival",
        "festivals_trouves": int(n_fest),
        "tarifs_collectes": int(priced.shape[0]),
        "global": _describe(main["price_eur"]),
        "par_categorie": by_type,
        "prix_par_jour": _describe(base["price_per_day_eur"]),
        "prix_par_artiste": _describe(base["price_per_artist_eur"]),
        "par_cadre": by_setting,
        "par_jauge": by_capacity,
        "couverture_prix": {str(k): int(v) for k, v in coverage.items()},
    }


def _stats_table(title: str, first_col: str, data: dict) -> list[str]:
    rows = [(k, v) for k, v in data.items() if v.get("count")]
    if not rows:
        return []
    lines = [f"## {title}\n", f"| {first_col} | N | Min | Max | Moyenne | Médiane |", "|---|---|---|---|---|---|"]
    for k, v in rows:
        lines.append(f"| {k} | {v['count']} | {v['min']} | {v['max']} | {v['mean']} | {v['median']} |")
    lines.append("")
    return lines


def summary_festivals_to_markdown(summary: dict) -> str:
    lines: list[str] = ["# Benchmark festivals — synthèse\n"]
    lines.append(f"- Festivals trouvés : **{summary['festivals_trouves']}**")
    lines.append(f"- Tarifs collectés : **{summary['tarifs_collectes']}**\n")
    for key, label in (("global", "Global hors options (EUR)"), ("prix_par_jour", "Prix par jour (EUR, plein tarif hors VIP/options)"),
                       ("prix_par_artiste", "Prix par artiste vu (EUR)")):
        g = summary[key]
        if g.get("count"):
            lines.append(f"## {label}\n")
            lines.append(f"- Min : {g['min']} | Max : {g['max']} | Moyenne : {g['mean']} | "
                         f"Médiane : {g['median']} | Écart-type : {g['std']}\n")
    lines += _stats_table("Par type de pass (EUR)", "Type de pass", summary["par_categorie"])
    lines += _stats_table("Prix par jour selon le cadre (EUR)", "Cadre", summary["par_cadre"])
    lines += _stats_table("Prix par jour selon la jauge (EUR)", "Jauge", summary["par_jauge"])
    lines.append("## Couverture des prix\n")
    for conf, n in summary["couverture_prix"].items():
        lines.append(f"- {conf} : {n}")
    return "\n".join(lines)
