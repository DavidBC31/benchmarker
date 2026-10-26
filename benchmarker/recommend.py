"""Reco tarifaire par comparables.

À partir d'un benchmark existant, propose une fourchette de prix par catégorie
pour un couple (artiste, salle) donné — ou par type de pass pour un festival —
en s'appuyant sur les événements comparables. Approche transparente : on
montre les filtres et l'échantillon retenus.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .analysis import per_day_base


def _quartiles(s: pd.Series) -> Optional[dict]:
    """Fourchette 25e–75e percentile + médiane (None si série vide)."""
    s = s.dropna()
    if s.empty:
        return None
    return {
        "n": int(s.count()),
        "fourchette_recommandee": [round(float(s.quantile(0.25)), 2), round(float(s.quantile(0.75)), 2)],
        "median": round(float(s.median()), 2),
    }


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
        q = _quartiles(grp["price_eur"])
        if q:
            per_cat[str(cat)] = q

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


# =============================================================================
# Mode Festival
# =============================================================================
def recommend_festival(
    df: pd.DataFrame,
    *,
    name: Optional[str] = None,
    genre: Optional[str] = None,
    capacity: Optional[int] = None,
    nb_nights: Optional[int] = None,
    setting: Optional[str] = None,
    artists_per_night: Optional[int] = None,
) -> dict:
    """Propose des prix de pass pour un festival cible à partir des comparables.

    Filtres successifs (chacun appliqué seulement s'il laisse un échantillon) :
    programmation, cadre, jauge journalière ±40 %, nombre de soirs ±1. Renvoie
    une fourchette par type de pass, un prix par jour de référence, et deux
    estimations dérivées : le pass couvrant tous les soirs de la cible (prix/jour
    des pass multi-jours × nb de soirs) et le pass 1 jour au prorata du nombre
    d'artistes par soir (prix par artiste vu × artistes/soir).
    """
    priced = df[df["price_eur"].notna() & ~df["is_option"].fillna(False).astype(bool)].copy()
    if priced.empty:
        return {"error": "Benchmark sans tarif exploitable."}

    sample = priced
    filters_applied: list[str] = []

    def _narrow(mask: pd.Series, label: str) -> None:
        nonlocal sample
        narrowed = sample[mask.reindex(sample.index, fill_value=False)]
        if not narrowed.empty:
            sample = narrowed
            filters_applied.append(label)

    if genre:
        _narrow(sample["genres"].str.contains(genre, case=False, na=False), f"programmation~='{genre}'")
    if setting:
        _narrow(sample["setting"] == setting, f"cadre={setting}")
    band = _capacity_band(capacity)
    if band is not None:
        _narrow(sample["capacity"].between(band[0], band[1]), f"jauge/jour∈[{band[0]},{band[1]}]")
    if nb_nights:
        _narrow(sample["nb_nights"].between(nb_nights - 1, nb_nights + 1), f"soirs∈[{nb_nights - 1},{nb_nights + 1}]")

    per_type: dict[str, dict] = {}
    for ptype, grp in sample.groupby("pass_type", dropna=True):
        q = _quartiles(grp["price_eur"])
        if q:
            per_type[str(ptype)] = q

    base = per_day_base(sample)
    reco: dict = {
        "mode": "festival",
        "cible": {
            "name": name, "genre": genre, "capacity": capacity, "nb_nights": nb_nights,
            "setting": setting, "artists_per_night": artists_per_night,
        },
        "filtres_appliques": filters_applied or ["aucun (échantillon global)"],
        "taille_echantillon": int(sample.drop_duplicates(["festival", "edition"]).shape[0]),
        "reco_par_categorie": per_type,
        "prix_par_jour": _quartiles(base["price_per_day_eur"]),
    }

    if nb_nights and nb_nights > 1:
        multi = _quartiles(base[base["pass_days"] >= 2]["price_per_day_eur"])
        if multi:
            lo, hi = multi["fourchette_recommandee"]
            # Arrondi à l'euro : estimation dérivée (et évite les artefacts du
            # type 46,33 × 3 = 138,99 dus à l'arrondi du prix/jour).
            reco["pass_tous_soirs_estime"] = {
                "jours": nb_nights,
                "fourchette_recommandee": [round(lo * nb_nights), round(hi * nb_nights)],
                "median": round(multi["median"] * nb_nights),
                "base": f"prix/jour des pass multi-jours comparables × {nb_nights} soirs (n={multi['n']})",
            }

    if artists_per_night:
        per_artist = _quartiles(base["price_per_artist_eur"])
        if per_artist:
            lo, hi = per_artist["fourchette_recommandee"]
            reco["pass_1_jour_selon_plateau"] = {
                "fourchette_recommandee": [round(lo * artists_per_night), round(hi * artists_per_night)],
                "median": round(per_artist["median"] * artists_per_night),
                "base": f"prix par artiste vu des comparables × {artists_per_night} artistes/soir (n={per_artist['n']})",
            }
    return reco


def recommendation_festival_to_markdown(reco: dict) -> str:
    if "error" in reco:
        return f"**Erreur** : {reco['error']}"
    c = reco["cible"]
    lines = ["# Préconisation tarifaire — festival\n"]
    lines.append(
        f"- Cible : {c.get('name') or '?'} | programmation : {c.get('genre') or '?'} | "
        f"jauge/jour : {c.get('capacity') or '?'} | soirs : {c.get('nb_nights') or '?'} | "
        f"cadre : {c.get('setting') or '?'} | artistes/soir : {c.get('artists_per_night') or '?'}"
    )
    lines.append(f"- Filtres : {', '.join(reco['filtres_appliques'])}")
    lines.append(f"- Festivals comparables retenus : {reco['taille_echantillon']}\n")
    if reco["reco_par_categorie"]:
        lines.append("| Type de pass | N | Fourchette recommandée (EUR) | Médiane |")
        lines.append("|---|---|---|---|")
        for t, r in reco["reco_par_categorie"].items():
            lo, hi = r["fourchette_recommandee"]
            lines.append(f"| {t} | {r['n']} | {lo} – {hi} | {r['median']} |")
        lines.append("")
    for key, label in (("prix_par_jour", "Prix par jour de référence"),
                       ("pass_tous_soirs_estime", "Pass tous soirs (estimation)"),
                       ("pass_1_jour_selon_plateau", "Pass 1 jour selon le plateau (estimation)")):
        r = reco.get(key)
        if r:
            lo, hi = r["fourchette_recommandee"]
            extra = f" — {r['base']}" if r.get("base") else ""
            lines.append(f"- **{label}** : {lo} – {hi} EUR (médiane {r['median']}){extra}")
    return "\n".join(lines)
