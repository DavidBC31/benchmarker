"""Base de référence : mémoire cumulative des événements tarifés.

Chaque run y enregistre ses concerts / éditions de festival tarifés ; les runs
suivants y puisent les événements déjà connus dans le profil (sans repayer
leur extraction), et la préconisation peut s'appuyer sur toute la base. Les
résultats s'enrichissent et se stabilisent d'un run à l'autre au lieu de
repartir de zéro.

Règles de fusion lors d'un enregistrement :
- une valeur absente du nouveau run n'efface jamais une valeur connue ;
- une grille moins bonne (échec, simple « à partir de ») ne remplace jamais
  une meilleure grille déjà en base ;
- un champ corrigé à la main est verrouillé : aucun run ne l'écrase ;
- un événement exclu à la main n'est jamais réintroduit.

Stockage SQLite (une connexion par opération : sûr avec les fils du serveur).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Union

from pydantic import ValidationError

from . import config
from .comparability import score_concerts, score_festivals
from .models import Concert, Festival, FestivalCriteria, PriceConfidence, PricedItem, SearchCriteria
from .price_cache import cache_key as identity

ARTIST, FESTIVAL = "artist", "festival"
_RANK = {PriceConfidence.NONE.value: 0, PriceConfidence.FROM_PRICE.value: 1,
         PriceConfidence.PARTIAL.value: 2, PriceConfidence.COMPLETE.value: 3}
# Champs liés à la grille : conservés ensemble (on ne mélange pas deux grilles).
_PRICE_FIELDS = ("prices", "price_confidence", "price_phase", "priced_at", "source_url", "notes", "anomalies")
# Spécifiques à un run (cible, provenance) : jamais stockés.
_RUN_FIELDS = ("comparability", "comparability_detail", "from_base")
# Champs modifiables à la main (l'identité — artiste/date/salle, festival/édition — ne l'est pas).
EDITABLE = {
    ARTIST: ("city", "capacity", "genre", "prices", "price_confidence", "price_phase", "source_url", "notes"),
    FESTIVAL: ("city", "region", "setting", "capacity", "genres", "nb_nights", "artists_per_night",
               "prices", "price_confidence", "price_phase", "source_url", "notes"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    label TEXT NOT NULL,
    search TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    priced_at TEXT,
    locked_fields TEXT NOT NULL DEFAULT '[]',
    excluded INTEGER NOT NULL DEFAULT 0,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_mode ON events(mode, excluded);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: Optional[str]) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(path or config.BASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def mode_of(item: PricedItem) -> str:
    return FESTIVAL if isinstance(item, Festival) else ARTIST


def _model(mode: str):
    return Festival if mode == FESTIVAL else Concert


def _label(item: PricedItem) -> str:
    if isinstance(item, Festival):
        return f"{item.name} {item.edition or ''}".strip()
    return f"{item.artist} — {item.venue or item.city or '?'} {item.date or ''}".strip()


def _store_dict(item: PricedItem) -> dict:
    data = item.model_dump(mode="json")
    for f in _RUN_FIELDS:
        data.pop(f, None)
    return data


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == "inconnu"


def _merge(old: dict, new: dict, locked: set) -> dict:
    # Une grille corrigée à la main verrouille aussi ses champs liés
    # (confiance, phase, date de tarification…) : sinon un run pourrait
    # requalifier une grille qu'il n'a pas le droit de modifier.
    if locked & set(_PRICE_FIELDS):
        locked = locked | set(_PRICE_FIELDS)
    old_est, new_est = set(old.get("estimated_fields") or []), set(new.get("estimated_fields") or [])
    out = dict(old)
    from_new: set = set()
    for k, v in new.items():
        if k in locked or k == "estimated_fields" or (_empty(v) and not _empty(old.get(k))):
            continue
        # Une valeur trouvée n'est jamais remplacée par une estimation
        # (constaté : une jauge « estimée » à 10 000 contredisant 33 000 trouvés).
        if k in new_est and k not in old_est and not _empty(old.get(k)):
            continue
        out[k] = v
        from_new.add(k)
    out["estimated_fields"] = sorted(
        f for f in old_est | new_est if (f in new_est if f in from_new else f in old_est)
    )
    # Ne jamais dégrader une grille : on garde l'ancienne, champs liés compris.
    if _RANK.get(new.get("price_confidence"), 0) < _RANK.get(old.get("price_confidence"), 0):
        for k in _PRICE_FIELDS:
            if k in old:
                out[k] = old[k]
    return out


# --- Enregistrement -----------------------------------------------------------
def record(items: Iterable[PricedItem], *, source: str = "", path: Optional[Path] = None) -> dict:
    """Enregistre/fusionne les événements tarifés. Renvoie les compteurs."""
    counts = {"ajouts": 0, "mises_a_jour": 0, "ignores": 0}
    with closing(_connect(path)) as conn, conn:
        for item in items:
            if item.from_base or item.price_confidence == PriceConfidence.NONE:
                counts["ignores"] += 1
                continue
            key, mode, new = identity(item), mode_of(item), _store_dict(item)
            # Grille sans horodatage : datée de son enregistrement (au plus tard).
            new["priced_at"] = new.get("priced_at") or _now()
            row = conn.execute("SELECT * FROM events WHERE id = ?", (key,)).fetchone()
            label = _label(item)
            search = _norm(f"{label} {new.get('city') or ''}")
            if row is None:
                conn.execute(
                    "INSERT INTO events (id, mode, label, search, data, first_seen, last_seen, priced_at, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, mode, label, search, json.dumps(new, ensure_ascii=False), _now(), _now(),
                     new.get("priced_at"), source),
                )
                counts["ajouts"] += 1
            elif row["excluded"]:
                counts["ignores"] += 1
            else:
                merged = _merge(json.loads(row["data"]), new, set(json.loads(row["locked_fields"])))
                conn.execute(
                    "UPDATE events SET data = ?, last_seen = ?, priced_at = ?, source = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), _now(), merged.get("priced_at"), source, key),
                )
                counts["mises_a_jour"] += 1
    return counts


# --- Lecture ------------------------------------------------------------------
def _entry(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "mode": row["mode"], "label": row["label"],
        "data": json.loads(row["data"]), "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        "priced_at": row["priced_at"], "locked_fields": json.loads(row["locked_fields"]),
        "excluded": bool(row["excluded"]), "source": row["source"],
    }


def list_entries(mode: Optional[str] = None, q: str = "", *, excluded: bool = False,
                 path: Optional[Path] = None) -> List[dict]:
    sql, args = "SELECT * FROM events WHERE excluded = ?", [1 if excluded else 0]
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    if q.strip():
        for token in _norm(q).split():
            sql += " AND search LIKE ?"
            args.append(f"%{token}%")
    sql += " ORDER BY last_seen DESC, label"
    with closing(_connect(path)) as conn:
        return [_entry(r) for r in conn.execute(sql, args)]


def stats(path: Optional[Path] = None) -> dict:
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            "SELECT mode, excluded, COUNT(*) AS n, MAX(last_seen) AS last FROM events GROUP BY mode, excluded"
        ).fetchall()
    out = {ARTIST: 0, FESTIVAL: 0, "exclus": 0, "derniere_maj": None}
    for r in rows:
        if r["excluded"]:
            out["exclus"] += r["n"]
        else:
            out[r["mode"]] = r["n"]
        if r["last"] and (out["derniere_maj"] is None or r["last"] > out["derniere_maj"]):
            out["derniere_maj"] = r["last"]
    return out


def items(mode: str, *, path: Optional[Path] = None) -> List[PricedItem]:
    """Événements actifs de la base, en objets du modèle (marqués `from_base`)."""
    model = _model(mode)
    out = []
    for e in list_entries(mode, path=path):
        item = model.model_validate(e["data"])
        item.from_base = True
        out.append(item)
    return out


# --- Utilisation dans un run ---------------------------------------------------
def _fresh(item: PricedItem, max_age_days: int) -> bool:
    try:
        priced = datetime.fromisoformat(item.priced_at)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - priced <= timedelta(days=max_age_days)


def _in_period(item: PricedItem, date_from: Optional[str], date_to: Optional[str]) -> bool:
    d = (item.sort_date() or "")[:10]
    if not d:
        return True
    return (not date_from or d >= date_from) and (not date_to or d <= date_to)


def matching_events(
    criteria: Union[SearchCriteria, FestivalCriteria],
    *,
    exclude_ids: Iterable[str] = (),
    log: Callable[[str], None] = lambda _m: None,
    path: Optional[Path] = None,
) -> List[PricedItem]:
    """Événements déjà connus à ajouter au run : tarifés, récents, dans la
    période, et soit parmi les cibles (artistes / festivals comparables), soit
    dans le profil (score de comparabilité ≥ seuil). Notés `from_base`."""
    is_fest = isinstance(criteria, FestivalCriteria)
    mode = FESTIVAL if is_fest else ARTIST
    skip = set(exclude_ids)
    pool = [
        i for i in items(mode, path=path)
        if identity(i) not in skip and i.price_confidence != PriceConfidence.NONE
        and _fresh(i, config.BASE_MAX_AGE_DAYS) and _in_period(i, criteria.date_from, criteria.date_to)
    ]
    if not pool:
        return []
    (score_festivals if is_fest else score_concerts)(pool, criteria)
    targets = [_norm(t) for t in (criteria.target_festivals if is_fest else criteria.target_artists)]

    def is_target(i: PricedItem) -> bool:
        name = _norm(i.name if is_fest else i.artist)
        return any(t and (t == name or t in name or name in t) for t in targets)

    def in_profile(i: PricedItem) -> bool:
        return i.comparability is not None and i.comparability >= config.COMPARABILITY_MIN

    chosen = [i for i in pool if is_target(i) or in_profile(i)]
    chosen.sort(key=lambda i: -(i.comparability or 0))
    chosen = chosen[: criteria.max_results]
    for i in chosen:
        i.notes = f"base de référence (tarifé le {(i.priced_at or '?')[:10]})" + (f" | {i.notes}" if i.notes else "")
    if chosen:
        log(f"Base de référence : {len(chosen)} {'festival(s)' if is_fest else 'date(s)'} déjà connu(s) "
            f"ajouté(s) (cibles ou dans le profil, sans nouvelle extraction).")
    return chosen


# --- Corrections manuelles -------------------------------------------------------
_PRICE_LINE = re.compile(r"^(.+?)\s*[=:]\s*(\d+(?:[.,]\d+)?)\s*(€|eur|[a-z]{3})?\s*$", re.I)


def parse_prices(text: str) -> list[dict]:
    """« Catégorie = prix [devise] », une par ligne. Lève ValueError sinon."""
    out = []
    for n, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        m = _PRICE_LINE.match(line.strip())
        if not m:
            raise ValueError(f"ligne {n} illisible : « {line.strip()} » (format attendu : Catégorie = 45)")
        cur = (m.group(3) or "EUR").upper()
        out.append({"name": m.group(1).strip(), "price": float(m.group(2).replace(",", ".")),
                    "currency": "EUR" if cur == "€" else cur})
    return out


def update(entry_id: str, changes: dict, *, path: Optional[Path] = None) -> dict:
    """Corrige une entrée à la main ; les champs modifiés sont verrouillés.
    Lève KeyError (entrée inconnue) ou ValueError (champ ou valeur invalide)."""
    with closing(_connect(path)) as conn, conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise KeyError(entry_id)
        mode, data = row["mode"], json.loads(row["data"])
        unknown = set(changes) - set(EDITABLE[mode])
        if unknown:
            raise ValueError(f"champ(s) non modifiable(s) : {', '.join(sorted(unknown))}")
        changed = {k: v for k, v in changes.items() if v != data.get(k)}
        if "prices" in changed and isinstance(changed["prices"], str):
            changed["prices"] = parse_prices(changed["prices"])
        candidate = {**data, **changed}
        candidate["estimated_fields"] = [f for f in data.get("estimated_fields", []) if f not in changed]
        try:
            validated = _model(mode).model_validate(candidate).model_dump(mode="json")
        except ValidationError as exc:
            err = exc.errors()[0]
            raise ValueError(f"{'.'.join(map(str, err['loc']))} : {err['msg']}") from exc
        for f in _RUN_FIELDS:
            validated.pop(f, None)
        locked = sorted(set(json.loads(row["locked_fields"])) | set(changed))
        conn.execute(
            "UPDATE events SET data = ?, locked_fields = ?, priced_at = ?, last_seen = ? WHERE id = ?",
            (json.dumps(validated, ensure_ascii=False), json.dumps(locked), validated.get("priced_at"), _now(), entry_id),
        )
        return _entry(conn.execute("SELECT * FROM events WHERE id = ?", (entry_id,)).fetchone())


def set_excluded(entry_id: str, excluded: bool, *, path: Optional[Path] = None) -> None:
    with closing(_connect(path)) as conn, conn:
        cur = conn.execute("UPDATE events SET excluded = ? WHERE id = ?", (1 if excluded else 0, entry_id))
        if cur.rowcount == 0:
            raise KeyError(entry_id)


# --- Amorçage depuis l'historique -------------------------------------------------
def seed_from_history(out_dir: Path, *, path: Optional[Path] = None) -> Optional[dict]:
    """À la création de la base, importe les runs passés de `out_dir`
    (exports JSON + méta). Sans effet si la base existe déjà."""
    base = Path(path or config.BASE_PATH)
    if base.exists() or not Path(out_dir).exists():
        return None
    totals = {"runs": 0, "ajouts": 0, "mises_a_jour": 0, "ignores": 0}
    metas = sorted(Path(out_dir).glob("benchmark_*_meta.json"), key=lambda p: p.stat().st_mtime)
    with closing(_connect(base)):
        pass  # crée la base même si l'historique est vide (amorçage unique)
    for meta_file in metas:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            job_id = meta["job_id"]
            raw = json.loads((Path(out_dir) / f"benchmark_{job_id}.json").read_text(encoding="utf-8"))
            model = _model(meta.get("mode") or ARTIST)
            run_items = [model.model_validate(r) for r in raw]
        except Exception:  # noqa: BLE001 — run incomplet ou ancien format : ignoré
            continue
        c = record(run_items, source=f"historique {job_id}", path=base)
        totals["runs"] += 1
        for k in ("ajouts", "mises_a_jour", "ignores"):
            totals[k] += c[k]
    return totals
