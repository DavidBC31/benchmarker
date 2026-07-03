#!/bin/bash
# Lance l'interface web du benchmarker avec l'environnement du projet.
# Utilisé par le service launchd (voir deploy/README.md).
set -euo pipefail

# Racine du projet = dossier parent de ce script.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Environnement virtuel.
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

# Secrets / configuration (non versionnés).
if [ -f "benchmarker.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "benchmarker.env"
  set +a
fi

exec python -m benchmarker.web
