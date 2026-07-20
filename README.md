# Benchmarker Billetterie

Outil de **benchmark tarifaire** pour le spectacle vivant musical, propulsé par les LLM (Claude).

On lui donne des **critères** (géographiques, de jauge, de style musical, de période) ; il va chercher sur le web les **dates** correspondantes, en extrait **artiste, date, lieu, prix par catégorie** et des **infos clés** (festival, date unique…), puis produit un **fichier benchmark** et une **analyse statistique**. Sur cette base, il peut aussi **préconiser une grille tarifaire** pour un couple artiste / salle donné.

## Comment ça marche

Approche **hybride** en deux temps (la découverte web seule ne suffit pas pour des prix par catégorie fiables) :

| Étape | Module | Rôle |
|-------|--------|------|
| 1. Découverte | `discovery.py` | Recherche web (LLM) → liste de dates candidates |
| 2. Extraction | `extraction.py` / `browser.py` | Récupération de chaque page billetterie → grille tarifaire structurée |
| 3. Analyse | `analysis.py` | Stats pandas : min / max / moyenne / médiane, par catégorie et par style |
| 4. Reco | `recommend.py` | Fourchette tarifaire par comparables (style + jauge proches) |

> ⚠️ **Le point dur, ce sont les prix par catégorie.** Les grilles (Carré Or / Fosse / Cat. 1-2-3) sont souvent chargées en JavaScript, varient dans le temps (dynamic pricing, frais de loc) et ne sont pas toujours complètes. Chaque prix est donc **horodaté** et assorti d'un **niveau de confiance** (`grille_complete`, `grille_partielle`, `prix_a_partir_de`, `aucun_prix`). La couverture ne sera pas de 100 % — une validation humaine sur les cas incomplets reste recommandée.

## Installation

```bash
pip install -r requirements.txt
```

Fournir des identifiants Anthropic (l'un des deux) :

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# ou : ant auth login   (le SDK récupère le profil automatiquement)
```

## Utilisation

### Construire un benchmark

Depuis un fichier de critères :

```bash
python -m benchmarker.cli search --criteria examples/criteria.example.json -o out/benchmark.csv
```

Ou en ligne de commande :

```bash
python -m benchmarker.cli search \
  --genre rap --genre hip-hop \
  --location France \
  --capacity-min 3000 --capacity-max 20000 \
  --date-from 2026-09-01 --date-to 2026-12-31 \
  --max-results 15 \
  -o out/benchmark.csv
```

Sorties générées :
- `out/benchmark.csv` — une ligne par tarif (format « long », prix normalisés en EUR) ;
- `out/benchmark.json` — données brutes fidèles au schéma ;
- `out/benchmark_synthese.md` — synthèse statistique.

Option `--no-prices` : découverte seule (rapide), sans extraction tarifaire.

#### Stratégie de récupération des prix

Certaines billetteries chargent leur grille tarifaire en JavaScript, invisible pour un simple fetch. On choisit la stratégie via `--fetch-strategy` :

| Valeur | Comportement |
|--------|--------------|
| `web_fetch` (défaut) | Le LLM récupère la page (outil serveur `web_fetch`). Rapide, mais rate les grilles en JS. |
| `playwright` | Rend la page dans Chromium (JS exécuté, bandeaux cookies gérés) puis structure. Plus fiable, plus lent. |
| `auto` | `web_fetch` d'abord ; bascule sur Playwright si la grille récupérée est jugée insuffisante. |

```bash
python -m benchmarker.cli search --criteria examples/criteria.example.json \
  --fetch-strategy auto -o out/benchmark.csv
```

Chromium doit être disponible. En local : `python -m playwright install chromium`. Dans un environnement où il est pré-installé (`PLAYWRIGHT_BROWSERS_PATH`), rien à faire — le module le détecte. Un `PageRenderer` unique est réutilisé sur toutes les dates d'un run.

### Préconiser des tarifs

```bash
python -m benchmarker.cli recommend \
  --benchmark out/benchmark.json \
  --artist "Nom Artiste" --genre rap --capacity 7000
```

Renvoie une fourchette (25e–75e percentile) et une médiane par catégorie, avec l'échantillon de comparables retenu.

## Interface web

Une interface web locale permet de piloter l'outil sans terminal : formulaire de critères, progression en direct, tableau des résultats (avec la colonne `price_confidence`), téléchargements et préconisation tarifaire.

Lancement en local :

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="ta-clé"
export BENCHMARKER_AUTH_TOKEN="un-mot-de-passe"   # protège l'accès
python -m benchmarker.web
# -> http://127.0.0.1:8080
```

**Hébergement permanent sur Mac Studio + Cloudflare** (service launchd + tunnel + Cloudflare Access) : voir [`deploy/README.md`](deploy/README.md). La clé API reste côté serveur ; l'accès est protégé par mot de passe applicatif **et** Cloudflare Access.

## Profils qualité / coût

Un sélecteur (interface web et `--profile` en CLI) permet d'arbitrer coût vs couverture par run :

| Profil | Découverte | Structuration prix | Idéal pour |
|--------|-----------|--------------------|-----------|
| **eco** (défaut) | Sonnet 5, effort medium, 5 recherches | Haiku | Coût minimal |
| **equilibre** | Sonnet 5, effort high, 8 recherches | Sonnet 5 | Meilleure couverture des dates |
| **max** | Opus 4.8, effort high, 8 recherches | Sonnet 5 | Qualité maximale |

Dans les trois cas, les prix passent d'abord par le **rendu gratuit** (Playwright) ; seule la découverte et la structuration montent en gamme. Méthode conseillée : lancer en `eco`, et si trop de `partielle`/`aucun_prix` ou trop peu de dates, relancer la même recherche en `equilibre` pour comparer le gain vs le surcoût.

```bash
python -m benchmarker.cli search --criteria examples/criteria.example.json --profile equilibre -o out/benchmark.csv
```

Profil par défaut surchargeable via `BENCHMARKER_PROFILE`.

## Coûts et optimisation

La consommation de crédits API est maîtrisée par trois choix, du plus impactant au moindre :

1. **Rendu gratuit d'abord.** En stratégie `auto` (défaut), chaque page est d'abord rendue **localement avec Playwright** (aucun token API), et le LLM n'intervient que pour **structurer** le texte obtenu. La récupération payante `web_fetch` (qui fait entrer toute la page dans le contexte) n'est déclenchée **qu'en dernier recours**, si le rendu gratuit ne donne pas une grille suffisante.
2. **Le bon modèle pour chaque tâche.** La structuration (texte → JSON) est mécanique → **Haiku** (le moins cher). Les boucles avec outils web (découverte, `web_fetch`) → **Sonnet 5** (bon marché et suffisant), pas Opus.
3. **Effort et volume réduits.** Effort bas, moins d'appels d'outils, et plafond sur le contenu ramené par `web_fetch`.

Concrètement, sur un run typique, l'extraction d'une date coûte le plus souvent **un seul appel Haiku** (structuration du rendu gratuit), au lieu d'une boucle Opus + structuration Opus.

## Configuration

Réglable par variables d'environnement (voir `benchmarker/config.py`) :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `BENCHMARKER_DISCOVERY_MODEL` | `claude-sonnet-5` | Modèle de la découverte (outils web) |
| `BENCHMARKER_EXTRACTION_MODEL` | `claude-sonnet-5` | Modèle du `web_fetch` (fallback payant) |
| `BENCHMARKER_STRUCTURE_MODEL` | `claude-haiku-4-5` | Modèle de structuration (texte → JSON) |
| `BENCHMARKER_FETCH_STRATEGY` | `auto` | `auto` (Playwright puis fallback) / `playwright` / `web_fetch` |
| `BENCHMARKER_DISCOVERY_EFFORT` | `medium` | Effort de la découverte |
| `BENCHMARKER_EXTRACTION_EFFORT` | `low` | Effort du `web_fetch` |
| `BENCHMARKER_MAX_SEARCH_USES` | `5` | Recherches web max par découverte |
| `BENCHMARKER_MAX_FETCH_USES` | `2` | Fetch max par date |
| `BENCHMARKER_FETCH_MAX_CONTENT_TOKENS` | `6000` | Plafond de contenu ramené par `web_fetch` |

> Pour minimiser encore le coût : `BENCHMARKER_FETCH_STRATEGY=playwright` supprime totalement le fallback payant (au risque de rater les pages où le rendu ne suffit pas).

## Structure

```
benchmarker/
├── config.py       # modèle, outils, sources, devises
├── models.py       # schéma Pydantic (Concert, PriceCategory, SearchCriteria…)
├── llm.py          # client + boucle d'outils serveur + extraction structurée
├── discovery.py    # étape 1 : découverte des dates
├── extraction.py   # étape 2 : extraction des prix
├── analysis.py     # stats pandas + export
├── recommend.py    # préconisation par comparables
├── pipeline.py     # orchestration bout-en-bout
└── cli.py          # interface ligne de commande
```

## Limites connues / pistes

- **Couverture prix** partielle par nature — prévoir une revue humaine des cas `grille_partielle` / `prix_a_partir_de`.
- **Volatilité** : un prix n'a de sens qu'horodaté (dynamic pricing).
- **Hors zone €** : conversion via des taux indicatifs (`config.FX_TO_EUR`) — à brancher sur une source live si le périmètre s'étend à l'Europe / l'international.
- **Anti-bot / JS** : certaines billetteries résistent au fetch simple ; une brique Playwright (déjà envisageable) pourrait fiabiliser ces cas.
