"""Point d'entrée : `python -m benchmarker.web`.

Sert l'application avec waitress (serveur WSGI de production). Écoute par
défaut sur 127.0.0.1:8080 — Cloudflare Tunnel se connecte à ce port local.
"""

from __future__ import annotations

import os

from .app import create_app


def main() -> None:
    host = os.environ.get("BENCHMARKER_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCHMARKER_PORT", "8080"))
    app = create_app()

    if not os.environ.get("BENCHMARKER_AUTH_TOKEN"):
        print(
            "AVERTISSEMENT : BENCHMARKER_AUTH_TOKEN non défini — l'appli est "
            "SANS mot de passe. Ne l'exposez pas sur Internet ainsi.",
            flush=True,
        )

    try:
        from waitress import serve

        print(f"Benchmarker web sur http://{host}:{port}", flush=True)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        # Repli : serveur de dev Flask (uniquement pour un test local rapide).
        print("waitress absent — serveur de dev Flask (local uniquement).", flush=True)
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
