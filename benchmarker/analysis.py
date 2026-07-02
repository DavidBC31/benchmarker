"""Analyse statistique du benchmark et export.

Transforme une liste de `Concert` en tableaux exploitables (une ligne par
tarif) et calcule des statistiques : min, max, moyenne, médiane, par catégorie
et par style. Tout est normalisé en EUR.
"""

from __future__ import annotations

from typing import List

import pandas as pd

from . import config
from .models import Concert


def concerts_to_frame(concerts: List[Concert]) -> pd.DataFrame:
    """Aplati les concerts en un DataFrame « long » : une ligne par tarif.

    Les dates sans prix apparaissent tout de même (price NaN) pour garder une
    trace de la couverture.
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
    return pd.DataFrame(rows)


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
    n_dates = df["source_url"].nunique(dropna=True) or df.groupby(
        ["artist", "date", "venue"], dropna=False
    ).ngroups

    by_category = {
        str(cat): _describe(grp["price_eur"])
        for cat, grp in priced.groupby("category", dropna=True)
    }
    by_genre = {
        str(genre): _describe(grp["price_eur"])
        for genre, grp in priced.groupby("genre", dropna=True)
    }

    coverage = (
        df.groupby("price_confidence")["price_confidence"].count().to_dict()
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
