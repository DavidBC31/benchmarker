"""Reco tarifaire par comparables.

À partir d'un benchmark existant, propose une fourchette de prix par catégorie
pour un couple (artiste, salle) donné, en s'appuyant sur les dates comparables
(style et jauge proches). Approche transparente : on montre l'échantillon retenu.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def _capacity_band(capacity: Optional[int]) -> Optional[tuple[int, int]]:
    """Fourchette de jauge « comparable » autour d'une capacité cible (±40 %)."""
    if capacity is None:
        return None
    return int(capacity * 0.6), int(capacity * 1.4)


def recommend(
    df: pd.DataFrame,
    *,
    genre: Optional[str] = None,
    capacity: Optional[int] = None,
    artist: Optional[str] = None,
) -> dict:
    """Propose une grille tarifaire indicative à partir des comparables.

    Filtre le benchmark par style et/ou jauge proche, puis calcule une
    fourchette (25e–75e percentile) et une médiane par catégorie.
    """
    priced = df[df["price_eur"].notna()].copy()
    if priced.empty:
        return {"error": "Benchmark sans tarif exploitable."}

    sample = priced
    filters_applied: list[str] = []

    if genre:
        g = sample[sample["genre"].str.contains(genre, case=False, na=False)]
        if not g.empty:
            sample = g
            filters_applied.append(f"style~='{genre}'")

    band = _capacity_band(capacity)
    if band is not None and "capacity" in sample.columns:
        c = sample[sample["capacity"].between(band[0], band[1])]
        if not c.empty:
            sample = c
            filters_applied.append(f"jauge∈[{band[0]},{band[1]}]")

    per_cat: dict[str, dict] = {}
    for cat, grp in sample.groupby("category", dropna=True):
        s = grp["price_eur"].dropna()
        if s.empty:
            continue
        per_cat[str(cat)] = {
            "n": int(s.count()),
            "fourchette_recommandee": [
                round(float(s.quantile(0.25)), 2),
                round(float(s.quantile(0.75)), 2),
            ],
            "median": round(float(s.median()), 2),
        }

    return {
        "cible": {"artist": artist, "genre": genre, "capacity": capacity},
        "filtres_appliques": filters_applied or ["aucun (échantillon global)"],
        "taille_echantillon": int(sample.shape[0]),
        "reco_par_categorie": per_cat,
    }


def recommendation_to_markdown(reco: dict) -> str:
    """Rend la reco en Markdown."""
    if "error" in reco:
        return f"**Erreur** : {reco['error']}"
    lines = ["# Préconisation tarifaire\n"]
    cible = reco["cible"]
    lines.append(
        f"- Cible : {cible.get('artist') or '?'} | style : {cible.get('genre') or '?'} "
        f"| jauge : {cible.get('capacity') or '?'}"
    )
    lines.append(f"- Filtres : {', '.join(reco['filtres_appliques'])}")
    lines.append(f"- Taille d'échantillon : {reco['taille_echantillon']}\n")
    if reco["reco_par_categorie"]:
        lines.append("| Catégorie | N | Fourchette recommandée (EUR) | Médiane |")
        lines.append("|---|---|---|---|")
        for cat, r in reco["reco_par_categorie"].items():
            lo, hi = r["fourchette_recommandee"]
            lines.append(f"| {cat} | {r['n']} | {lo} – {hi} | {r['median']} |")
    else:
        lines.append("_Aucune catégorie comparable trouvée._")
    return "\n".join(lines)
