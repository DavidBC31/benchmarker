"""Reco tarifaire par comparables.

À partir d'un benchmark existant, propose une fourchette de prix par catégorie
pour un couple (artiste, salle) donné — ou par type de pass pour un festival —
en s'appuyant sur les événements comparables. Mêmes règles que les
statistiques (`analysis.stat_base_*`) : plein tarif uniquement, une valeur par
événement, événements hors profil exclus. Approche transparente : on montre
les filtres et la taille de l'échantillon retenus.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .analysis import PASS_OPTION, PASS_OTHER, PASS_REDUCED, stat_base_concerts, stat_base_festivals

# Nombre minimal d'événements pour qu'un filtre (ou une phase de prix) soit
# appliqué : en dessous, l'échantillon devient trop étroit pour être fiable.
_MIN_SAMPLE = 3


def _quartiles(s: pd.Series) -> Optional[dict]:
    """Fourchette 25e–75e percentile + médiane (None si série vide)."""
    s = pd.to_numeric(s, errors="coerce").dropna()
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


def _profile_warning(info: dict) -> Optional[str]:
    if info.get("profil_relache"):
        return (f"peu de comparables dans le profil : préconisation calculée sur tous les événements, "
                f"dont {info.get('evenements_hors_profil', 0)} hors profil — à interpréter avec prudence")
    return None


class _Sample:
    """Échantillon d'événements restreint par filtres successifs.

    Un filtre n'est appliqué que s'il laisse au moins `_MIN_SAMPLE` événements
    (sinon il est noté comme ignoré) : mieux vaut un échantillon un peu plus
    large et documenté qu'une préconisation tirée d'un seul comparable.
    """

    def __init__(self, df: pd.DataFrame, keys: list[str]):
        self.df, self.keys = df, keys
        self.applied: list[str] = []
        self.skipped: list[str] = []

    def n_events(self, df: Optional[pd.DataFrame] = None) -> int:
        d = self.df if df is None else df
        return int(d.drop_duplicates(self.keys).shape[0])

    def narrow(self, mask: pd.Series, label: str) -> None:
        narrowed = self.df[mask.reindex(self.df.index, fill_value=False).astype(bool)]
        if self.n_events(narrowed) >= _MIN_SAMPLE:
            self.df = narrowed
            self.applied.append(label)
        else:
            self.skipped.append(f"{label} (trop peu de comparables)")


def recommend(
    df: pd.DataFrame,
    *,
    genre: Optional[str] = None,
    capacity: Optional[int] = None,
    artist: Optional[str] = None,
) -> dict:
    """Propose une grille tarifaire indicative à partir des dates comparables.

    Filtre le benchmark par style et/ou jauge proche, puis calcule une
    fourchette (25e–75e percentile) et une médiane par catégorie, ainsi qu'un
    prix d'entrée (catégorie la moins chère de chaque date).
    """
    base, info = stat_base_concerts(df)
    if base.empty:
        return {"error": "Benchmark sans tarif plein exploitable."}
    keys = ["artist", "date", "venue"]
    sample = _Sample(base, keys)
    if genre:
        sample.narrow(sample.df["genre"].str.contains(genre, case=False, na=False), f"style~='{genre}'")
    band = _capacity_band(capacity)
    if band is not None:
        cap = pd.to_numeric(sample.df["capacity"], errors="coerce")
        sample.narrow(cap.between(band[0], band[1]), f"jauge∈[{band[0]},{band[1]}]")

    per_cat: dict[str, dict] = {}
    for cat, grp in sample.df.groupby("category"):
        q = _quartiles(grp["price_eur"])
        if q:
            per_cat[str(cat)] = q

    return {
        "cible": {"artist": artist, "genre": genre, "capacity": capacity},
        "filtres_appliques": sample.applied or ["aucun (échantillon global)"],
        "filtres_ignores": sample.skipped,
        "taille_echantillon": sample.n_events(),
        "avertissement_profil": _profile_warning(info),
        "reco_par_categorie": per_cat,
        "prix_entree": _quartiles(sample.df.groupby(keys)["price_eur"].min()),
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
    if reco.get("filtres_ignores"):
        lines.append(f"- Filtres ignorés : {', '.join(reco['filtres_ignores'])}")
    if reco.get("avertissement_profil"):
        lines.append(f"- ⚠ {reco['avertissement_profil']}")
    lines.append(f"- Dates comparables retenues : {reco['taille_echantillon']}\n")
    if reco["reco_par_categorie"]:
        lines.append("| Catégorie | N | Fourchette recommandée (EUR) | Médiane |")
        lines.append("|---|---|---|---|")
        for cat, r in reco["reco_par_categorie"].items():
            lo, hi = r["fourchette_recommandee"]
            lines.append(f"| {cat} | {r['n']} | {lo} – {hi} | {r['median']} |")
    else:
        lines.append("_Aucune catégorie comparable trouvée._")
    if reco.get("prix_entree"):
        r = reco["prix_entree"]
        lines.append(f"\n- **Prix d'entrée** (catégorie la moins chère) : "
                     f"{r['fourchette_recommandee'][0]} – {r['fourchette_recommandee'][1]} EUR (médiane {r['median']})")
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

    Filtres successifs (chacun appliqué seulement s'il laisse au moins 3
    festivals) : programmation, cadre, jauge journalière ±40 %, nombre de soirs
    ±1, puis phase de prix « normal » si assez de festivals l'ont. Renvoie une
    fourchette par type de pass, un prix par jour de référence, une estimation
    du pass couvrant tous les soirs de la cible et une estimation du pass 1
    jour au prorata du plateau (artistes par soir).
    """
    by_type, per_fest, info = stat_base_festivals(df)
    if by_type.empty:
        return {"error": "Benchmark sans tarif plein exploitable."}
    keys = ["festival", "edition"]

    # Les filtres portent sur les festivals : on les applique à la table « une
    # ligne par festival », puis on restreint `by_type` aux festivals retenus.
    fest = by_type.drop_duplicates(keys)[keys + ["setting", "capacity", "genres", "nb_nights", "price_phase"]]
    fest = fest.merge(per_fest[keys + ["price_per_day_eur", "price_per_artist_eur", "multi_day_per_day_eur"]],
                      on=keys, how="left")
    sample = _Sample(fest, keys)
    if genre:
        sample.narrow(sample.df["genres"].str.contains(genre, case=False, na=False), f"programmation~='{genre}'")
    if setting:
        sample.narrow(sample.df["setting"] == setting, f"cadre={setting}")
    band = _capacity_band(capacity)
    if band is not None:
        cap = pd.to_numeric(sample.df["capacity"], errors="coerce")
        sample.narrow(cap.between(band[0], band[1]), f"jauge/jour∈[{band[0]},{band[1]}]")
    if nb_nights:
        nights = pd.to_numeric(sample.df["nb_nights"], errors="coerce")
        sample.narrow(nights.between(nb_nights - 1, nb_nights + 1), f"soirs∈[{nb_nights - 1},{nb_nights + 1}]")
    # Comparer à phase de prix égale : on privilégie le tarif « normal ».
    sample.narrow(sample.df["price_phase"] == "normal", "phase de prix = normal")
    phase_note = None
    if "phase de prix = normal" not in sample.applied:
        phases = sorted({str(p) for p in sample.df["price_phase"].dropna()})
        if len(phases) > 1 or (phases and phases != ["normal"]):
            phase_note = f"phases de prix mélangées dans l'échantillon : {', '.join(phases)}"

    selected = set(sample.df[keys].itertuples(index=False, name=None))
    types = by_type[[k in selected for k in by_type[keys].itertuples(index=False, name=None)]]
    per_type: dict[str, dict] = {}
    for ptype, grp in types.groupby("pass_type"):
        if ptype in (PASS_OPTION, PASS_REDUCED, PASS_OTHER):
            continue
        q = _quartiles(grp["price_eur"])
        if q:
            per_type[str(ptype)] = q

    reco: dict = {
        "mode": "festival",
        "cible": {
            "name": name, "genre": genre, "capacity": capacity, "nb_nights": nb_nights,
            "setting": setting, "artists_per_night": artists_per_night,
        },
        "filtres_appliques": sample.applied or ["aucun (échantillon global)"],
        "filtres_ignores": sample.skipped,
        "avertissement_phase": phase_note,
        "avertissement_profil": _profile_warning(info),
        "taille_echantillon": sample.n_events(),
        "reco_par_categorie": per_type,
        "prix_par_jour": _quartiles(sample.df["price_per_day_eur"]),
    }

    if nb_nights and nb_nights > 1:
        multi = _quartiles(sample.df["multi_day_per_day_eur"])
        if multi:
            lo, hi = multi["fourchette_recommandee"]
            # Arrondi à l'euro : estimation dérivée (évite les 138,99 €).
            reco["pass_tous_soirs_estime"] = {
                "jours": nb_nights,
                "fourchette_recommandee": [round(lo * nb_nights), round(hi * nb_nights)],
                "median": round(multi["median"] * nb_nights),
                "base": f"prix/jour des pass multi-jours comparables × {nb_nights} soirs (n={multi['n']})",
            }

    if artists_per_night:
        per_artist = _quartiles(sample.df["price_per_artist_eur"])
        if per_artist and per_artist["n"] >= _MIN_SAMPLE:
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
    if reco.get("filtres_ignores"):
        lines.append(f"- Filtres ignorés : {', '.join(reco['filtres_ignores'])}")
    if reco.get("avertissement_phase"):
        lines.append(f"- ⚠ {reco['avertissement_phase']}")
    if reco.get("avertissement_profil"):
        lines.append(f"- ⚠ {reco['avertissement_profil']}")
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
