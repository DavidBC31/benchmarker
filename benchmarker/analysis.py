"""Analyse statistique du benchmark et export.

Transforme une liste de `Concert` (mode Artiste) ou de `Festival` (mode
Festival) en tableaux exploitables (une ligne par tarif) et calcule des
statistiques normalisées en EUR.

Principes de calcul, pour comparer à périmètre égal :
- plein tarif uniquement (tarifs réduits, enfant, habitants, PMR, groupes et
  options exclus des statistiques, mais conservés dans les exports) ;
- une valeur par événement et par catégorie / type de pass (médiane si une
  catégorie apparaît plusieurs fois, ex. « Pass Vendredi » et « Pass Dimanche ») ;
- grilles identiques sur plusieurs dates d'un même artiste comptées une fois
  (sinon un artiste à 5 dates pèse 5 fois plus qu'un artiste à 1 date) ;
- prix plein tarif sous `MIN_PLAUSIBLE_PRICE_EUR` écartés (erreur probable) ;
- événements « hors profil » (score de comparabilité < `COMPARABILITY_MIN`)
  écartés des statistiques.
Ce qui est écarté est compté dans `exclusions`, pour rester transparent.
"""

from __future__ import annotations

import re
from typing import List, Optional

import pandas as pd

from . import config
from .models import Concert, Festival, PricedItem

# =============================================================================
# Classification des tarifs (commune aux deux modes)
# =============================================================================
_REDUCED_RE = re.compile(
    r"r[ée]duit|[ée]tudiant|enfant|b[ée]b[ée]|jeune|junior|senior|solidaire|ch[ôo]meur|demandeur"
    r"|\bpmr\b|\bpsh\b|handicap|-\s?\d+\s?ans|moins de \d+ ans|pass\s?culture|groupe|\bce\b|\bcse\b"
    r"|comit[ée]|habitant|r[ée]sident|abonn[ée]|adh[ée]rent|invitation|gratuit"
    # Tarifs « locaux » nommés par gentilé : « Tarif ardennais », « Tarif lyonnais »…
    r"|tarif\s+[a-zàâçéèêëîïôûùüÿ-]+(?:ais|ois|ain|aine|ien|ienne)s?\b",
    re.I,
)
_OPTION_RE = re.compile(r"camping|parking|navette|consigne|glamping|caravane|tente", re.I)
_VIP_RE = re.compile(r"\bvip\b|premium|golden|backstage|lounge|confort|carr[ée] or", re.I)


def is_reduced(name: str) -> bool:
    """Tarif non comparable au plein tarif (réduit, enfant, habitants, PMR, groupe…)."""
    return bool(_REDUCED_RE.search(name or ""))


def is_option(name: str) -> bool:
    """Option vendue à part (camping, parking…), exclue des stats de prix."""
    n = (name or "").strip().lower()
    if n.startswith("option"):
        return True
    return bool(_OPTION_RE.search(n)) and "pass" not in n and "billet" not in n


def tariff_kind(name: str) -> str:
    if is_option(name):
        return "option"
    if is_reduced(name):
        return "reduit"
    return "plein"


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


def _in_profile(df: pd.DataFrame) -> pd.Series:
    """Événements sans score (non évalués) ou au-dessus du seuil de comparabilité."""
    score = pd.to_numeric(df["comparability"], errors="coerce")
    return score.isna() | (score >= config.COMPARABILITY_MIN)


def _profile_mask(ok: pd.DataFrame, keys: list[str]) -> tuple[pd.Series, bool]:
    """Masque « dans le profil ». S'il reste moins de 2 événements tarifés dans
    le profil, on relâche (tous les événements, signalé) plutôt que d'afficher
    des statistiques vides — constaté : seuls des festivals hors profil avaient
    une grille."""
    mask = _in_profile(ok)
    if (~mask).any() and ok[mask].drop_duplicates(keys).shape[0] < 2:
        return pd.Series(True, index=ok.index), True
    return mask, False


def _common_cols(item: PricedItem) -> dict:
    return {
        "price_confidence": item.price_confidence.value,
        "price_phase": item.price_phase,
        "comparability": item.comparability,
        "anomalies": " | ".join(item.anomalies),
        "estimated_fields": ", ".join(item.estimated_fields),
        "priced_at": item.priced_at,
        "notes": item.notes,
        "source_url": item.source_url,
    }


def _to_eur(price: float, currency: str) -> Optional[float]:
    rate = config.FX_TO_EUR.get(currency.upper())
    return round(price * rate, 2) if rate is not None else None


# =============================================================================
# Mode Artiste
# =============================================================================
# Schéma explicite du DataFrame « long ». Garantit que toutes les colonnes
# existent même quand `concerts` est vide (0 date trouvée).
_FRAME_COLUMNS = [
    "artist", "date", "venue", "city", "country", "capacity", "genre",
    "event_type", "source_url", "price_confidence", "price_phase", "comparability",
    "anomalies", "estimated_fields", "priced_at", "notes",
    "category", "tariff", "price", "currency", "price_eur", "fees_included",
]
_EVENT_KEYS = ["artist", "date", "venue"]


def concerts_to_frame(concerts: List[Concert]) -> pd.DataFrame:
    """Aplati les concerts en un DataFrame « long » : une ligne par tarif.

    Les dates sans prix apparaissent tout de même (price NaN) pour garder une
    trace de la couverture. Renvoie toujours les mêmes colonnes.
    """
    rows: list[dict] = []
    for c in concerts:
        base = {
            "artist": c.artist, "date": c.date, "venue": c.venue, "city": c.city,
            "country": c.country, "capacity": c.capacity, "genre": c.genre,
            "event_type": c.event_type.value, **_common_cols(c),
        }
        if not c.prices:
            rows.append({**base, **{k: None for k in _FRAME_COLUMNS if k not in base}})
            continue
        for pc in c.prices:
            rows.append({
                **base,
                "category": pc.name,
                "tariff": tariff_kind(pc.name),
                "price": pc.price,
                "currency": pc.currency,
                "price_eur": _to_eur(pc.price, pc.currency),
                "fees_included": pc.fees_included,
            })
    return pd.DataFrame(rows, columns=_FRAME_COLUMNS)


def stat_base_concerts(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Une ligne par (date, catégorie) comparable au plein tarif, grilles
    dupliquées retirées, + compteurs de ce qui a été écarté."""
    priced = df[df["price_eur"].notna()].copy()
    priced[_EVENT_KEYS] = priced[_EVENT_KEYS].fillna("")
    plein = priced[priced["tariff"] == "plein"]
    ok = plein[plein["price_eur"] >= config.MIN_PLAUSIBLE_PRICE_EUR]
    in_profile, relaxed = _profile_mask(ok, _EVENT_KEYS)
    kept = ok[in_profile]

    base = (
        kept.groupby(_EVENT_KEYS + ["category"], as_index=False, sort=False)
        .agg(price_eur=("price_eur", "median"), city=("city", "first"), capacity=("capacity", "first"),
             genre=("genre", "first"), comparability=("comparability", "first"))
    )
    # Grilles identiques sur plusieurs dates d'un même artiste : une seule fois.
    first_event_by_grid: dict = {}
    for key, g in base.groupby(_EVENT_KEYS, sort=False):
        grid = (key[0], tuple(sorted(zip(g["category"].astype(str), g["price_eur"].round(2)))))
        first_event_by_grid.setdefault(grid, key)
    keep_events = set(first_event_by_grid.values())
    n_events = base.groupby(_EVENT_KEYS).ngroups
    base = base[[tuple(k) in keep_events for k in base[_EVENT_KEYS].itertuples(index=False)]]

    info = {
        "tarifs_reduits_ou_options": int((priced["tariff"] != "plein").sum()),
        "prix_aberrants": int((plein["price_eur"] < config.MIN_PLAUSIBLE_PRICE_EUR).sum()),
        "evenements_hors_profil": int(ok[~_in_profile(ok)].drop_duplicates(_EVENT_KEYS).shape[0]),
        "grilles_dupliquees": int(n_events - len(keep_events)),
        "profil_relache": relaxed,
    }
    return base, info


def summarize(df: pd.DataFrame) -> dict:
    """Statistiques plein tarif : globales, prix d'entrée, par catégorie, par style."""
    base, info = stat_base_concerts(df)
    n_dates = df.groupby(_EVENT_KEYS, dropna=False).ngroups
    # Prix d'entrée = catégorie la moins chère de chaque date : chaque date
    # pèse 1, quel que soit le nombre de catégories de sa grille.
    entry = base.groupby(_EVENT_KEYS)["price_eur"].min()
    coverage = (
        df.drop_duplicates(_EVENT_KEYS)
        .groupby("price_confidence")["price_confidence"].count().to_dict()
    )
    return {
        "mode": "artist",
        "dates_trouvees": int(n_dates),
        "tarifs_collectes": int(df["price_eur"].notna().sum()),
        "global": _describe(base["price_eur"]),
        "prix_entree": _describe(entry),
        "par_categorie": {str(c): _describe(g["price_eur"]) for c, g in base.groupby("category")},
        "par_style": {str(s): _describe(g["price_eur"]) for s, g in base.groupby("genre", dropna=True)},
        "couverture_prix": {str(k): int(v) for k, v in coverage.items()},
        "exclusions": info,
    }


def _exclusions_md(info: dict) -> list[str]:
    labels = {
        "tarifs_reduits_ou_options": "tarifs réduits / spécifiques / options",
        "prix_aberrants": f"prix aberrants (< {config.MIN_PLAUSIBLE_PRICE_EUR:g} €)",
        "evenements_hors_profil": f"événements hors profil (comparabilité < {config.COMPARABILITY_MIN})",
        "grilles_dupliquees": "grilles dupliquées (même artiste, même grille)",
    }
    info = info or {}
    if info.get("profil_relache"):
        labels.pop("evenements_hors_profil")
    parts = [f"{n} {labels[k]}" for k, n in info.items() if n and k in labels]
    out = [f"_Écartés des statistiques : {', '.join(parts)}._\n"] if parts else []
    if info.get("profil_relache"):
        out.append(f"_⚠ Moins de 2 événements tarifés dans le profil : statistiques calculées sur tous les "
                   f"événements, dont {info.get('evenements_hors_profil', 0)} hors profil._\n")
    return out


def _stats_table(title: str, first_col: str, data: dict) -> list[str]:
    rows = [(k, v) for k, v in data.items() if v.get("count")]
    if not rows:
        return []
    lines = [f"## {title}\n", f"| {first_col} | N | Min | Max | Moyenne | Médiane |", "|---|---|---|---|---|---|"]
    for k, v in rows:
        lines.append(f"| {k} | {v['count']} | {v['min']} | {v['max']} | {v['mean']} | {v['median']} |")
    lines.append("")
    return lines


def _describe_md(label: str, g: dict) -> list[str]:
    if not g.get("count"):
        return []
    return [f"## {label}\n", f"- Min : {g['min']} | Max : {g['max']} | Moyenne : {g['mean']} | "
                             f"Médiane : {g['median']} | Écart-type : {g['std']} (n={g['count']})\n"]


def summary_to_markdown(summary: dict) -> str:
    """Rend le résumé statistique en Markdown lisible."""
    lines: list[str] = ["# Benchmark billetterie — synthèse\n"]
    lines.append(f"- Dates trouvées : **{summary['dates_trouvees']}**")
    lines.append(f"- Tarifs collectés : **{summary['tarifs_collectes']}**\n")
    lines += _exclusions_md(summary.get("exclusions"))
    lines += _describe_md("Global plein tarif (EUR)", summary["global"])
    lines += _describe_md("Prix d'entrée par date (EUR)", summary.get("prix_entree", {}))
    lines += _stats_table("Par catégorie de place (EUR)", "Catégorie", summary["par_categorie"])
    lines += _stats_table("Par style musical (EUR)", "Style", summary["par_style"])
    lines.append("## Couverture des prix\n")
    for conf, n in summary["couverture_prix"].items():
        lines.append(f"- {conf} : {n}")
    return "\n".join(lines)


# =============================================================================
# Mode Festival
# =============================================================================
# Les noms de pass sont très hétérogènes d'un festival à l'autre (« Pass
# Samedi », « Forfait 3J », « Pass intégral », « Camping »…) : on les ramène à
# un type normalisé (Pass N jours / VIP / réduit / option) pour les comparer, et
# on dérive un prix par jour et un prix par artiste vu.
_WEEKDAYS = (
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_DAYS_RE = re.compile(r"(\d+)\s*(?:jours?|j\b|soirs?|soir[ée]es?|days?)", re.I)
_FULL_RE = re.compile(r"int[ée]gral|complet|\bfull\b|tous les jours|all days|pass festival", re.I)
_WEEKEND_RE = re.compile(r"week-?end", re.I)
_ONE_DAY_RE = re.compile(r"journ[ée]e|pass jour|day pass|\bsoir[ée]e\b|\bjour\b|\bday\b", re.I)

PASS_OPTION = "Option (camping, parking…)"
PASS_VIP = "VIP / Premium"
PASS_REDUCED = "Tarif réduit / spécifique"
PASS_OTHER = "Autre"


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
    if is_reduced(name):
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
    "nb_artists_total", "source_url", "price_confidence", "price_phase", "comparability",
    "anomalies", "estimated_fields", "priced_at", "notes",
    "category", "pass_type", "pass_days", "is_option", "tariff", "price", "currency",
    "price_eur", "fees_included", "price_per_day_eur", "price_per_artist_eur",
]
_FEST_KEYS = ["festival", "edition"]
_STANDARD_PASS = re.compile(r"^Pass \d+ jours?$")


def festivals_to_frame(festivals: List[Festival]) -> pd.DataFrame:
    """Aplati les festivals en DataFrame « long » : une ligne par tarif.

    `price_per_day_eur` = prix / nombre de jours du pass ; `price_per_artist_eur`
    = prix par jour / artistes par soir (coût d'un artiste vu). Ce dernier n'est
    calculé que si le nombre d'artistes par soir a été trouvé, pas estimé.
    """
    rows: list[dict] = []
    for f in festivals:
        base = {
            "festival": f.name, "edition": f.edition, "date_start": f.date_start,
            "date_end": f.date_end, "city": f.city, "region": f.region, "country": f.country,
            "setting": f.setting.value, "capacity": f.capacity, "genres": f.genres,
            "headliners": ", ".join(f.headliners), "nb_nights": f.nb_nights,
            "artists_per_night": f.artists_per_night, "nb_artists_total": f.nb_artists_total,
            **_common_cols(f),
        }
        if not f.prices:
            rows.append({**base, **{k: None for k in _FEST_COLUMNS if k not in base}})
            continue
        apn_found = f.artists_per_night and "artists_per_night" not in f.estimated_fields
        for pc in f.prices:
            price_eur = _to_eur(pc.price, pc.currency)
            option = is_option(pc.name)
            days = None if option else pass_days(pc.name, f.nb_nights)
            per_day = round(price_eur / days, 2) if (price_eur is not None and days) else None
            per_artist = round(per_day / f.artists_per_night, 2) if (per_day is not None and apn_found) else None
            rows.append({
                **base,
                "category": pc.name,
                "pass_type": pass_type(pc.name, f.nb_nights),
                "pass_days": days,
                "is_option": option,
                "tariff": tariff_kind(pc.name),
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


_FEST_DESCRIPTIVE = dict(
    setting=("setting", "first"), capacity=("capacity", "first"), genres=("genres", "first"),
    nb_nights=("nb_nights", "first"), artists_per_night=("artists_per_night", "first"),
    price_phase=("price_phase", "first"), comparability=("comparability", "first"),
)


def stat_base_festivals(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Bases statistiques festival + compteurs de ce qui a été écarté.

    - `by_type` : une ligne par (festival, type de pass), médiane des prix ;
    - `per_fest` : une ligne par festival plein tarif : prix par jour (médiane
      des pass standard), prix/jour des pass multi-jours, prix par artiste vu.
    Chaque festival pèse ainsi 1, quel que soit le nombre de lignes de sa grille.
    """
    priced = df[df["price_eur"].notna()].copy()
    priced[_FEST_KEYS] = priced[_FEST_KEYS].fillna("")
    aberrant = (priced["tariff"] == "plein") & (priced["price_eur"] < config.MIN_PLAUSIBLE_PRICE_EUR)
    ok = priced[~aberrant]
    in_profile, relaxed = _profile_mask(ok, _FEST_KEYS)
    kept = ok[in_profile]

    by_type = kept.groupby(_FEST_KEYS + ["pass_type"], as_index=False, sort=False).agg(
        price_eur=("price_eur", "median"), **_FEST_DESCRIPTIVE
    )
    std = kept[kept["pass_type"].str.match(_STANDARD_PASS) & kept["price_per_day_eur"].notna()]
    per_fest = std.groupby(_FEST_KEYS, as_index=False, sort=False).agg(
        price_per_day_eur=("price_per_day_eur", "median"),
        price_per_artist_eur=("price_per_artist_eur", "median"),
        **_FEST_DESCRIPTIVE,
    )
    multi = std[pd.to_numeric(std["pass_days"], errors="coerce") >= 2].groupby(_FEST_KEYS, as_index=False)["price_per_day_eur"].median()
    per_fest = per_fest.merge(multi.rename(columns={"price_per_day_eur": "multi_day_per_day_eur"}),
                              on=_FEST_KEYS, how="left")
    info = {
        "tarifs_reduits_ou_options": int((priced["tariff"] != "plein").sum()),
        "prix_aberrants": int(aberrant.sum()),
        "evenements_hors_profil": int(ok[~_in_profile(ok)].drop_duplicates(_FEST_KEYS).shape[0]),
        "profil_relache": relaxed,
    }
    return by_type, per_fest, info


def summarize_festivals(df: pd.DataFrame) -> dict:
    """Statistiques festival : type de pass, prix/jour, prix/artiste, cadre, jauge, phase."""
    by_type, per_fest, info = stat_base_festivals(df)
    n_fest = df.groupby(_FEST_KEYS, dropna=False).ngroups
    comparable_types = by_type[~by_type["pass_type"].isin([PASS_OPTION, PASS_REDUCED, PASS_OTHER])]
    banded = per_fest.assign(_band=per_fest["capacity"].map(capacity_band)).dropna(subset=["_band"])
    band_order = {label: i for i, (_, _, label) in enumerate(_CAPACITY_BANDS)}
    per_artist = _describe(per_fest["price_per_artist_eur"])
    coverage = (
        df.drop_duplicates(_FEST_KEYS)
        .groupby("price_confidence")["price_confidence"].count().to_dict()
    )
    return {
        "mode": "festival",
        "festivals_trouves": int(n_fest),
        "tarifs_collectes": int(df["price_eur"].notna().sum()),
        "global": _describe(comparable_types["price_eur"]),
        "par_categorie": {
            str(t): _describe(g["price_eur"])
            for t, g in sorted(by_type.groupby("pass_type"), key=lambda kv: _pass_type_order(kv[0]))
        },
        "prix_par_jour": _describe(per_fest["price_per_day_eur"]),
        "prix_par_artiste": per_artist,
        # Indicateur fragile : n'est affiché qu'avec au moins 3 festivals dont
        # le nombre d'artistes par soir a été trouvé (pas estimé).
        "prix_par_artiste_fiable": per_artist.get("count", 0) >= 3,
        "par_cadre": {str(s): _describe(g["price_per_day_eur"]) for s, g in per_fest.groupby("setting")},
        "par_jauge": {
            str(b): _describe(g["price_per_day_eur"])
            for b, g in sorted(banded.groupby("_band"), key=lambda kv: band_order[kv[0]])
        },
        "par_phase": {
            str(p): _describe(g["price_per_day_eur"])
            for p, g in per_fest.assign(price_phase=per_fest["price_phase"].fillna("non précisée")).groupby("price_phase")
        },
        "couverture_prix": {str(k): int(v) for k, v in coverage.items()},
        "exclusions": info,
    }


def summary_festivals_to_markdown(summary: dict) -> str:
    lines: list[str] = ["# Benchmark festivals — synthèse\n"]
    lines.append(f"- Festivals trouvés : **{summary['festivals_trouves']}**")
    lines.append(f"- Tarifs collectés : **{summary['tarifs_collectes']}**\n")
    lines += _exclusions_md(summary.get("exclusions"))
    lines += _describe_md("Pass plein tarif, une valeur par festival et type (EUR)", summary["global"])
    lines += _describe_md("Prix par jour, une valeur par festival (EUR)", summary["prix_par_jour"])
    if summary.get("prix_par_artiste_fiable"):
        lines += _describe_md("Prix par artiste vu (EUR)", summary["prix_par_artiste"])
    else:
        lines.append("_Prix par artiste vu : non affiché (moins de 3 festivals dont le nombre d'artistes par soir est connu)._\n")
    lines += _stats_table("Par type de pass (EUR)", "Type de pass", summary["par_categorie"])
    lines += _stats_table("Prix par jour selon le cadre (EUR)", "Cadre", summary["par_cadre"])
    lines += _stats_table("Prix par jour selon la jauge (EUR)", "Jauge", summary["par_jauge"])
    lines += _stats_table("Prix par jour selon la phase de prix (EUR)", "Phase", summary.get("par_phase", {}))
    lines.append("## Couverture des prix\n")
    for conf, n in summary["couverture_prix"].items():
        lines.append(f"- {conf} : {n}")
    return "\n".join(lines)


# =============================================================================
# Contrôles de cohérence des grilles (les deux modes)
# =============================================================================
def check_grid(item: PricedItem) -> list[str]:
    """Incohérences d'une grille, signalées (pas corrigées) pour vérification."""
    out: list[str] = []
    plein = [pc for pc in item.prices if tariff_kind(pc.name) == "plein"]
    for pc in plein:
        if pc.price < config.MIN_PLAUSIBLE_PRICE_EUR:
            out.append(f"prix anormalement bas : {pc.name} {pc.price:g} {pc.currency} (écarté des stats)")
    seen: dict[str, float] = {}
    for pc in plein:
        key = pc.name.strip().lower()
        if key in seen and seen[key] != pc.price:
            out.append(f"catégorie en double à prix différents : {pc.name}")
        seen[key] = pc.price

    valid = [pc for pc in plein if pc.price >= config.MIN_PLAUSIBLE_PRICE_EUR]
    if isinstance(item, Festival):
        standard = [(pass_days(pc.name, item.nb_nights), pc) for pc in valid if not _VIP_RE.search(pc.name)]
        one_day = [pc.price for d, pc in standard if d == 1]
        if one_day:
            lo, hi = min(one_day), max(one_day)
            for d, pc in standard:
                if d and d >= 2 and pc.price < lo:
                    out.append(f"{pc.name} ({pc.price:g}) moins cher qu'un pass 1 jour ({lo:g})")
                elif d and d >= 2 and pc.price > d * hi * 1.05:
                    out.append(f"{pc.name} ({pc.price:g}) plus cher que {d} pass 1 jour ({d} × {hi:g})")
    elif len(valid) >= 2:
        ratio = max(pc.price for pc in valid) / min(pc.price for pc in valid)
        if ratio > 10:
            out.append(f"écart extrême dans la grille (×{ratio:.0f}) : vérifier un package VIP ou une erreur")
    return list(dict.fromkeys(out))
