"""Application web (Flask) du benchmarker.

Conçue pour un hébergement auto-hébergé derrière Cloudflare :
- servie par un serveur WSGI de production (waitress) ;
- protégée par un mot de passe partagé (BENCHMARKER_AUTH_TOKEN) ;
- la clé API Anthropic reste côté serveur (variable d'environnement), jamais
  saisie dans le navigateur.

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
from typing import Optional

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
from ..models import Concert, SearchCriteria
from ..pipeline import build_benchmark, resolve_target_artists
from ..recommend import recommend

OUT_DIR = Path(os.environ.get("BENCHMARKER_OUT_DIR", "out"))


# --- État des jobs en mémoire ----------------------------------------------
@dataclass
class Job:
    id: str
    status: str = "running"  # running | done | error
    log: list = field(default_factory=list)
    concerts: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    error: Optional[str] = None


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


# --- Application ------------------------------------------------------------
def create_app() -> Flask:
    app = Flask(__name__)
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
        criteria = _criteria_from_payload(data)
        profile = data.get("profile") or None
        strategy = data.get("strategy") or None
        with_prices = bool(data.get("with_prices", True))

        job = Job(id=uuid.uuid4().hex[:12])
        with _JOBS_LOCK:
            _JOBS[job.id] = job

        _write_meta(job.id, criteria, profile, strategy, with_prices, status="running")

        thread = threading.Thread(
            target=_run_job,
            args=(job, criteria, profile, strategy, with_prices),
            daemon=True,
        )
        thread.start()
        return jsonify(job_id=job.id)

    @app.route("/api/status/<job_id>")
    @login_required
    def api_status(job_id: str):
        job = _JOBS.get(job_id)
        if not job:
            return jsonify(error="job inconnu"), 404
        return jsonify(status=job.status, log=job.log, error=job.error)

    @app.route("/api/result/<job_id>")
    @login_required
    def api_result(job_id: str):
        job = _JOBS.get(job_id)
        if job and job.status == "running":
            return jsonify(error="job en cours"), 409
        if job and job.status == "done":
            return jsonify(summary=job.summary, rows=_rows(job.concerts))

        # Pas (ou plus) en mémoire (redémarrage serveur, ancien run…) : on
        # retombe sur les fichiers persistés pour que l'historique reste
        # consultable au-delà de la durée de vie du process.
        concerts = _load_concerts_from_disk(job_id)
        if concerts is None:
            return jsonify(error="job inconnu"), 404
        df = analysis.concerts_to_frame(concerts)
        return jsonify(summary=analysis.summarize(df), rows=_rows(concerts))

    @app.route("/api/recommend/<job_id>", methods=["POST"])
    @login_required
    def api_recommend(job_id: str):
        job = _JOBS.get(job_id)
        concerts = job.concerts if (job and job.concerts) else _load_concerts_from_disk(job_id)
        if not concerts:
            return jsonify(error="pas de benchmark disponible"), 404
        data = request.get_json(force=True) or {}
        df = analysis.concerts_to_frame(concerts)
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
    criteria: SearchCriteria,
    profile: Optional[str],
    strategy: Optional[str],
    with_prices: bool,
) -> None:
    def log(msg: str) -> None:
        job.log.append(msg)

    try:
        client = build_client()  # clé résolue côté serveur (env)
        settings = config.resolve_settings(profile, strategy)
        # Résolu explicitement ici (et non seulement dans build_benchmark) pour
        # que la méta/l'historique reflètent les critères réellement utilisés
        # (target_artists, style déduit) — idempotent, donc aucun coût
        # supplémentaire quand build_benchmark la ré-appliquera juste après.
        criteria = resolve_target_artists(client, criteria, settings, log)
        _write_meta(job.id, criteria, profile, strategy, with_prices, status="running")

        concerts = build_benchmark(
            criteria,
            client=client,
            with_prices=with_prices,
            profile=profile,
            strategy=strategy,
            progress=log,
        )
        job.concerts = concerts
        df = analysis.concerts_to_frame(concerts)
        job.summary = analysis.summarize(df)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        base = OUT_DIR / f"benchmark_{job.id}"
        df.to_csv(base.with_suffix(".csv"), index=False)
        base.with_suffix(".json").write_text(
            json.dumps([c.model_dump() for c in concerts], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        base.with_name(base.name + "_synthese.md").write_text(
            analysis.summary_to_markdown(job.summary), encoding="utf-8"
        )
        log("Terminé.")
        job.status = "done"
        _write_meta(
            job.id, criteria, profile, strategy, with_prices,
            status="done", summary=job.summary,
        )
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        log(f"Erreur : {exc}")
        job.status = "error"
        _write_meta(
            job.id, criteria, profile, strategy, with_prices,
            status="error", error=str(exc),
        )


# --- Historique / persistance -----------------------------------------------
def _meta_path(job_id: str) -> Path:
    return OUT_DIR / f"benchmark_{job_id}_meta.json"


def _write_meta(
    job_id: str,
    criteria: SearchCriteria,
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


def _list_history() -> list[dict]:
    """Liste les runs passés (les plus récents d'abord) à partir des métadonnées persistées."""
    if not OUT_DIR.exists():
        return []
    runs = []
    for meta_file in OUT_DIR.glob("benchmark_*_meta.json"):
        try:
            runs.append(json.loads(meta_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs


def _load_concerts_from_disk(job_id: str) -> Optional[list[Concert]]:
    """Recharge les `Concert` d'un run passé depuis son export JSON."""
    path = OUT_DIR / f"benchmark_{job_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Concert(**c) for c in data]
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


def _rows(concerts: list[Concert]) -> list[dict]:
    """Représentation compacte pour le tableau de l'UI."""
    out = []
    for c in concerts:
        eur = c.prices_eur(config.FX_TO_EUR)
        prices_str = ", ".join(f"{p.name} {p.price:g}{p.currency}" for p in c.prices)
        out.append(
            {
                "artist": c.artist,
                "date": c.date,
                "venue": c.venue,
                "city": c.city,
                "capacity": c.capacity,
                "genre": c.genre,
                "event_type": c.event_type.value,
                "price_confidence": c.price_confidence.value,
                "prices": prices_str,
                "min_eur": round(min(eur), 2) if eur else None,
                "max_eur": round(max(eur), 2) if eur else None,
                "source_url": c.source_url,
                "notes": c.notes,
            }
        )
    return out
