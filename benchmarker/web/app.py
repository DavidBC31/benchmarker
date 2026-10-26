"""Application web (Flask) du benchmarker.

Conçue pour un hébergement auto-hébergé derrière Cloudflare :
- servie par un serveur WSGI de production (waitress) ;
- protégeable par un mot de passe partagé (BENCHMARKER_AUTH_TOKEN), en plus
  ou à la place de Cloudflare Access ;
- la clé API Anthropic reste côté serveur (variable d'environnement), jamais
  saisie dans le navigateur.

Deux modes de benchmark : « artist » (dates de concerts comparables) et
« festival » (éditions de festivals comparables, grilles de pass).

Les benchmarks tournent en tâche de fond ; l'UI interroge la progression.
Chaque run écrit un fichier de métadonnées (`_meta.json`) en plus des exports,
ce qui permet de reconstituer l'historique même après un redémarrage du
serveur (les jobs en mémoire, eux, sont perdus au redémarrage).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional, Union

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .. import analysis, config
from ..llm import build_client
from ..models import SETTING_LABELS, Concert, Festival, FestivalCriteria, SearchCriteria
from ..pipeline import (
    build_benchmark,
    build_festival_benchmark,
    resolve_target_artists,
    resolve_target_festivals,
)
from ..recommend import recommend, recommend_festival

# Ancré à la racine du projet (et non au cwd du process, qui varie selon la
# façon dont le service est lancé — launchd, terminal, etc. — et a causé des
# runs "perdus" quand deux instances résolvaient des OUT_DIR différents).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = Path(os.environ.get("BENCHMARKER_OUT_DIR") or (_PROJECT_ROOT / "out"))

ARTIST = "artist"
FESTIVAL = "festival"

AnyCriteria = Union[SearchCriteria, FestivalCriteria]


# --- État des jobs en mémoire ----------------------------------------------
@dataclass
class Job:
    id: str
    mode: str = ARTIST
    status: str = "running"  # running | done | error
    log: list = field(default_factory=list)
    items: list = field(default_factory=list)  # Concert ou Festival selon le mode
    summary: dict = field(default_factory=dict)
    error: Optional[str] = None


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


# --- Application ------------------------------------------------------------
def create_app() -> Flask:
    app = Flask(__name__)
    # Conserve l'ordre des clés construit côté analyse (types de pass, tranches
    # de jauge…) : par défaut Flask trie les clés JSON alphabétiquement.
    app.json.sort_keys = False
    app.secret_key = os.environ.get("BENCHMARKER_SECRET_KEY") or os.urandom(24).hex()
    auth_token = os.environ.get("BENCHMARKER_AUTH_TOKEN")

    # --- Authentification (mot de passe partagé) ---------------------------
    def _authed() -> bool:
        return (not auth_token) or session.get("authed") is True

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not _authed():
                if request.path.startswith("/api/"):
                    return jsonify(error="non authentifié"), 401
                return redirect(url_for("login"))
            return fn(*a, **kw)

        return wrapper

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth_token:
            return redirect(url_for("index"))
        if request.method == "POST":
            if request.form.get("password") == auth_token:
                session["authed"] = True
                return redirect(url_for("index"))
            return render_template("login.html", error="Mot de passe incorrect."), 401
        return render_template("login.html", error=None)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- Pages -------------------------------------------------------------
    @app.route("/")
    @login_required
    def index():
        has_key = bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        return render_template(
            "index.html",
            has_key=has_key,
            default_strategy=config.FETCH_STRATEGY,
            default_profile=config.DEFAULT_PROFILE,
            auth_enabled=bool(auth_token),
        )

    # --- API : lancement ---------------------------------------------------
    @app.route("/api/run", methods=["POST"])
    @login_required
    def api_run():
        data = request.get_json(force=True) or {}
        mode = FESTIVAL if data.get("mode") == FESTIVAL else ARTIST
        criteria = (
            _festival_criteria_from_payload(data) if mode == FESTIVAL
            else _criteria_from_payload(data)
        )
        profile = data.get("profile") or None
        strategy = data.get("strategy") or None
        with_prices = bool(data.get("with_prices", True))

        job = Job(id=uuid.uuid4().hex[:12], mode=mode)
        with _JOBS_LOCK:
            _JOBS[job.id] = job

        _write_meta(job.id, mode, criteria, profile, strategy, with_prices, status="running")

        thread = threading.Thread(
            target=_run_job,
            args=(job, criteria, profile, strategy, with_prices),
            daemon=True,
        )
        thread.start()
        return jsonify(job_id=job.id, mode=mode)

    @app.route("/api/status/<job_id>")
    @login_required
    def api_status(job_id: str):
        job = _JOBS.get(job_id)
        if not job:
            return jsonify(error="job inconnu"), 404
        return jsonify(status=job.status, log=job.log, error=job.error, mode=job.mode)

    @app.route("/api/result/<job_id>")
    @login_required
    def api_result(job_id: str):
        job = _JOBS.get(job_id)
        if job and job.status == "running":
            return jsonify(error="job en cours"), 409
        if job and job.status == "done":
            return jsonify(mode=job.mode, summary=job.summary, rows=_rows(job.mode, job.items))

        # Pas (ou plus) en mémoire (redémarrage serveur, ancien run…) : on
        # retombe sur les fichiers persistés pour que l'historique reste
        # consultable au-delà de la durée de vie du process.
        mode = _mode_from_meta(job_id)
        items = _load_items_from_disk(job_id, mode)
        if items is None:
            return jsonify(error="job inconnu"), 404
        summary = _summarize(mode, _frame(mode, items))
        return jsonify(mode=mode, summary=summary, rows=_rows(mode, items))

    @app.route("/api/recommend/<job_id>", methods=["POST"])
    @login_required
    def api_recommend(job_id: str):
        job = _JOBS.get(job_id)
        mode = job.mode if job else _mode_from_meta(job_id)
        items = job.items if (job and job.items) else _load_items_from_disk(job_id, mode)
        if not items:
            return jsonify(error="pas de benchmark disponible"), 404
        data = request.get_json(force=True) or {}
        df = _frame(mode, items)
        if mode == FESTIVAL:
            setting = data.get("setting") or None
            reco = recommend_festival(
                df,
                name=data.get("name") or None,
                genre=data.get("genre") or None,
                capacity=_int_or_none(data.get("capacity")),
                nb_nights=_int_or_none(data.get("nb_nights")),
                setting=setting if setting in SETTING_LABELS else None,
                artists_per_night=_int_or_none(data.get("artists_per_night")),
            )
        else:
            reco = recommend(
                df,
                genre=data.get("genre") or None,
                capacity=_int_or_none(data.get("capacity")),
                artist=data.get("artist") or None,
            )
        return jsonify(reco=reco)

    # --- Historique ----------------------------------------------------------
    @app.route("/api/history")
    @login_required
    def api_history():
        return jsonify(runs=_list_history())

    # --- Téléchargements ---------------------------------------------------
    @app.route("/download/<job_id>.<ext>")
    @login_required
    def download(job_id: str, ext: str):
        path = OUT_DIR / f"benchmark_{job_id}.{ext}"
        if not path.exists():
            return jsonify(error="fichier introuvable"), 404
        return send_file(path, as_attachment=True)

    return app


# --- Exécution d'un job -----------------------------------------------------
def _run_job(
    job: Job,
    criteria: AnyCriteria,
    profile: Optional[str],
    strategy: Optional[str],
    with_prices: bool,
) -> None:
    def log(msg: str) -> None:
        job.log.append(msg)

    try:
        client = build_client()  # clé résolue côté serveur (env)
        settings = config.resolve_settings(profile, strategy)
        # Résolu explicitement ici (et non seulement dans le pipeline) pour que
        # la méta/l'historique reflètent les critères réellement utilisés
        # (cibles comparables, style déduit) — idempotent, donc aucun coût
        # supplémentaire quand le pipeline la ré-appliquera juste après.
        if job.mode == FESTIVAL:
            criteria = resolve_target_festivals(client, criteria, settings, log)
        else:
            criteria = resolve_target_artists(client, criteria, settings, log)
        _write_meta(job.id, job.mode, criteria, profile, strategy, with_prices, status="running")

        build = build_festival_benchmark if job.mode == FESTIVAL else build_benchmark
        items = build(
            criteria,
            client=client,
            with_prices=with_prices,
            profile=profile,
            strategy=strategy,
            progress=log,
        )
        job.items = items
        df = _frame(job.mode, items)
        job.summary = _summarize(job.mode, df)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        base = OUT_DIR / f"benchmark_{job.id}"
        df.to_csv(base.with_suffix(".csv"), index=False)
        base.with_suffix(".json").write_text(
            json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        base.with_name(base.name + "_synthese.md").write_text(
            _markdown(job.mode, job.summary), encoding="utf-8"
        )
        log("Terminé.")
        job.status = "done"
        _write_meta(
            job.id, job.mode, criteria, profile, strategy, with_prices,
            status="done", summary=job.summary,
        )
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        log(f"Erreur : {exc}")
        job.status = "error"
        _write_meta(
            job.id, job.mode, criteria, profile, strategy, with_prices,
            status="error", error=str(exc),
        )


# --- Dispatch par mode ---------------------------------------------------------
def _frame(mode: str, items: list):
    return analysis.festivals_to_frame(items) if mode == FESTIVAL else analysis.concerts_to_frame(items)


def _summarize(mode: str, df) -> dict:
    return analysis.summarize_festivals(df) if mode == FESTIVAL else analysis.summarize(df)


def _markdown(mode: str, summary: dict) -> str:
    if mode == FESTIVAL:
        return analysis.summary_festivals_to_markdown(summary)
    return analysis.summary_to_markdown(summary)


# --- Historique / persistance -----------------------------------------------
def _meta_path(job_id: str) -> Path:
    return OUT_DIR / f"benchmark_{job_id}_meta.json"


def _write_meta(
    job_id: str,
    mode: str,
    criteria: AnyCriteria,
    profile: Optional[str],
    strategy: Optional[str],
    with_prices: bool,
    *,
    status: str,
    summary: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Persiste les métadonnées d'un run pour l'historique (survit aux redémarrages)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _meta_path(job_id)
    meta = {}
    if path.exists():
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta.update(
        {
            "job_id": job_id,
            "mode": mode,
            "created_at": meta.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "criteria": criteria.model_dump(),
            "profile": profile or config.DEFAULT_PROFILE,
            "strategy": strategy,
            "with_prices": with_prices,
            "status": status,
        }
    )
    if summary is not None:
        meta["summary"] = summary
    if error is not None:
        meta["error"] = error
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_meta(job_id: str) -> dict:
    try:
        return json.loads(_meta_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _mode_from_meta(job_id: str) -> str:
    # Les runs antérieurs au mode Festival n'ont pas de champ "mode" : artiste.
    return FESTIVAL if _read_meta(job_id).get("mode") == FESTIVAL else ARTIST


def _list_history() -> list[dict]:
    """Liste les runs passés (les plus récents d'abord) à partir des métadonnées persistées."""
    if not OUT_DIR.exists():
        return []
    runs = []
    for meta_file in OUT_DIR.glob("benchmark_*_meta.json"):
        try:
            run = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        run.setdefault("mode", ARTIST)
        runs.append(run)
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs


def _load_items_from_disk(job_id: str, mode: str) -> Optional[list]:
    """Recharge les `Concert` / `Festival` d'un run passé depuis son export JSON."""
    path = OUT_DIR / f"benchmark_{job_id}.json"
    if not path.exists():
        return None
    model = Festival if mode == FESTIVAL else Concert
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [model(**c) for c in data]
    except (json.JSONDecodeError, OSError, ValueError):
        return None


# --- Helpers ---------------------------------------------------------------
def _criteria_from_payload(data: dict) -> SearchCriteria:
    return SearchCriteria(
        reference_artist=(data.get("reference_artist") or "").strip() or None,
        genres=_as_list(data.get("genres")),
        location=data.get("location") or None,
        countries=_as_list(data.get("countries")) or ["France"],
        capacity_min=_int_or_none(data.get("capacity_min")),
        capacity_max=_int_or_none(data.get("capacity_max")),
        date_from=data.get("date_from") or None,
        date_to=data.get("date_to") or None,
        max_results=_int_or_none(data.get("max_results")) or 15,
        extra=data.get("extra") or None,
    )


def _festival_criteria_from_payload(data: dict) -> FestivalCriteria:
    return FestivalCriteria(
        reference_festival=(data.get("reference_festival") or "").strip() or None,
        genres=_as_list(data.get("genres")),
        location=data.get("location") or None,
        countries=_as_list(data.get("countries")) or ["France"],
        settings=[s for s in _as_list(data.get("settings")) if s in SETTING_LABELS],
        capacity_min=_int_or_none(data.get("capacity_min")),
        capacity_max=_int_or_none(data.get("capacity_max")),
        nights_min=_int_or_none(data.get("nights_min")),
        nights_max=_int_or_none(data.get("nights_max")),
        artists_per_night_min=_int_or_none(data.get("artists_per_night_min")),
        artists_per_night_max=_int_or_none(data.get("artists_per_night_max")),
        date_from=data.get("date_from") or None,
        date_to=data.get("date_to") or None,
        max_results=_int_or_none(data.get("max_results")) or 10,
        extra=data.get("extra") or None,
    )


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value) if value not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _rows(mode: str, items: list) -> list[dict]:
    """Représentation compacte pour le tableau de l'UI."""
    out = []
    for i in items:
        eur = i.prices_eur(config.FX_TO_EUR)
        common = {
            "city": i.city,
            "capacity": i.capacity,
            "price_confidence": i.price_confidence.value,
            "prices": ", ".join(f"{p.name} {p.price:g}{p.currency}" for p in i.prices),
            "min_eur": round(min(eur), 2) if eur else None,
            "max_eur": round(max(eur), 2) if eur else None,
            "source_url": i.source_url,
            "notes": i.notes,
        }
        if mode == FESTIVAL:
            out.append({
                **common,
                "festival": i.name,
                "edition": i.edition,
                "date_start": i.date_start,
                "date_end": i.date_end,
                "region": i.region,
                "setting": i.setting.value,
                "genres": i.genres,
                "headliners": ", ".join(i.headliners),
                "nb_nights": i.nb_nights,
                "artists_per_night": i.artists_per_night,
            })
        else:
            out.append({
                **common,
                "artist": i.artist,
                "date": i.date,
                "venue": i.venue,
                "genre": i.genre,
                "event_type": i.event_type.value,
            })
    return out
