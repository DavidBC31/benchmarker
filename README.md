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

> 💡 **URLs génériques.** La découverte remonte parfois l'URL d'un portail
> d'événements (page d'accueil) plutôt que la fiche de vente précise. Dans ce
> cas, l'extraction détecte l'URL générique et tente **une recherche ciblée
> unique** (peu coûteuse) pour retrouver la fiche directe avant d'abandonner —
> voir la note `URL affinée : …` ou `Pas de fiche événement directe trouvée`
> dans les résultats.

> 💡 **Protections anti-bot.** Certaines billetteries coupent la connexion
> face à un navigateur headless (ex. `ERR_HTTP2_PROTOCOL_ERROR`). Le rendu
> Playwright désactive HTTP/2, masque le signal `navigator.webdriver` et
> retente une fois avec un contexte frais sur ce type d'erreur réseau. Si ça
> échoue malgré tout, la note `playwright : …` l'indique et le fallback
> `web_fetch` prend le relais.

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

#### Cibler par artiste de référence

Plutôt que (ou en complément de) `--genre`, on peut fournir un artiste de référence : l'outil recherche d'abord des artistes comparables (même style, notoriété, taille de salle), puis oriente la découverte sur cette liste précise plutôt que sur un style générique.

```bash
python -m benchmarker.cli search \
  --reference-artist "PLK" \
  --location France \
  --capacity-min 3000 --capacity-max 20000 \
  -o out/benchmark.csv
```

Le style musical est aussi déduit automatiquement si `--genre` n'est pas fourni. Cette résolution ne coûte qu'un seul appel de recherche supplémentaire, effectué une fois en amont de la découverte.

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

### Mode Festival

Même logique que le mode Artiste, avec des critères propres aux festivals : **jauge journalière**, **cadre** (urbain / périurbain / rural), **programmation**, **nombre de soirs**, **nombre d'artistes par soir**. Un **festival de référence** optionnel permet de cibler des festivals comparables (même jauge, cadre, programmation et format).

```bash
python -m benchmarker.cli festival \
  --reference-festival "Les Vieilles Charrues" \
  --setting rural --nights-min 2 --nights-max 4 \
  --capacity-min 15000 --capacity-max 60000 \
  -o out/festivals.csv
```

Les grilles sont extraites avec la même mécanique que les concerts (le site officiel du festival sert de source de repli privilégiée). Les noms de pass, très hétérogènes (« Pass Samedi », « Forfait 3J », « Pass intégral », « Camping »…), sont normalisés en types comparables : **Pass N jours**, **VIP / Premium**, **Tarif réduit**, **Option** (camping, parking…). L'analyse ajoute :

- **prix par jour** (prix du pass / nombre de jours, plein tarif hors VIP et options) ;
- **prix par artiste vu** (prix par jour / artistes par soir) — pour comparer des formats différents ;
- ventilation du prix par jour **par cadre** et **par tranche de jauge**.

Préconisation pour un festival cible :

```bash
python -m benchmarker.cli festival-recommend --benchmark out/festivals.json \
  --genre rap --capacity 25000 --nights 3 --setting rural --artists-per-night 10
```

Renvoie une fourchette par type de pass, un prix par jour de référence, une estimation du pass couvrant tous les soirs de la cible, et une estimation du pass 1 jour au prorata du plateau (artistes par soir).

### Justesse et cohérence des résultats

Les statistiques et la préconisation suivent les mêmes règles, pour comparer à périmètre égal :

- **Plein tarif uniquement** : tarifs réduits, enfant, habitants (« Tarif ardennais »…), PMR, groupes et options (camping, parking) sont conservés dans les exports mais exclus des statistiques.
- **Une valeur par événement** et par catégorie / type de pass. Les grilles identiques sur plusieurs dates d'un même artiste comptent une fois. Le **prix d'entrée** (catégorie la moins chère de chaque date) complète les statistiques par catégorie.
- **Contrôles de cohérence** : prix anormalement bas, catégorie en double à prix différents, pass multi-jours moins cher qu'un pass 1 jour ou plus cher que la somme des jours… Ces cas sont signalés (⚠ dans le tableau) ; les prix sous 5 € sont aussi écartés des statistiques.
- **Édition et phase de prix** : chaque grille est rattachée à l'édition réellement affichée (une page peut déjà montrer l'édition suivante) et à sa phase (early bird, normal, dernière minute, sur place). La préconisation festival compare au tarif « normal » quand au moins 3 comparables l'ont, sinon elle signale le mélange.
- **Score de comparabilité (0-100)** de chaque événement avec la cible : critères saisis, sinon profil chiffré de la référence (jauge, cadre, soirs, artistes par soir), sinon l'événement de référence lui-même. La jauge agit en multiplicateur (un écart de jauge ne se compense pas par une programmation proche). Sous 40, l'événement est **hors profil** : affiché, mais exclu des statistiques. S'il reste moins de 2 événements tarifés dans le profil, les statistiques portent sur tous les événements, avec un avertissement.
- **Valeurs estimées** (jauge reprise de la capacité habituelle d'une salle, artistes par soir approximés) signalées par « ≈ ». Le prix par artiste vu n'est affiché qu'avec au moins 3 festivals dont le nombre d'artistes par soir est connu.

Côté efficience, **3 événements sont extraits en parallèle** (un navigateur par fil : même coût, environ 3 fois plus rapide), et chaque grille est **mémorisée** 7 jours (échecs : 24 h) dans `cache/prices/` : relancer un benchmark sur les mêmes événements ne repaie pas l'extraction et rend le même résultat.

### Base de référence (mémoire cumulative)

Les résultats ne repartent plus de zéro à chaque run : une base locale (`data/reference.sqlite`) conserve chaque événement tarifé (date de concert ou édition de festival).

- **Chaque run l'enrichit.** Une grille plus fraîche remplace l'ancienne, mais une grille moins bonne (échec, simple « à partir de ») ne remplace jamais une meilleure, et une valeur absente n'efface jamais une valeur connue.
- **Chaque run y puise.** Après la découverte, les événements déjà connus parmi les cibles (artistes / festivals comparables) ou dans le profil (comparabilité ≥ 40), tarifés depuis moins d'un an et dans la période demandée, sont ajoutés aux résultats avec un badge **mémoire**, sans nouvelle extraction. Un run dont la découverte ne trouve rien peut ainsi rester utile.
- **La préconisation** peut s'appuyer sur toute la base (case « Inclure toute la base de référence »), et sa cible est préremplie depuis les critères du run.
- **Onglet Base** : recherche (insensible aux accents), correction à la main (jauge, cadre, soirs, artistes par soir, grille « Catégorie = prix »…), exclusion et restauration. Un champ corrigé est **verrouillé** : aucun run ne l'écrase (une grille corrigée verrouille aussi sa confiance et sa phase). Un événement exclu n'est jamais réintroduit.
- **Amorçage** : à la première mise en route, les runs passés présents dans `out/` sont importés.

## Interface web

Une interface web locale permet de piloter l'outil sans terminal, avec une direction artistique dédiée (glassmorphism iOS 18, navbar pilule, bleu `#1450E2`) :

- **Artiste** : formulaire de critères (dont un champ optionnel « Artiste de référence » pour cibler des artistes comparables plutôt que/en complément d'un style), progression en direct, résultats (tuiles de synthèse, tableau par catégorie, détail des dates avec la colonne `price_confidence` colorée et les notes), téléchargements CSV/JSON/synthèse, préconisation tarifaire.
- **Festival** : même parcours avec les critères festival (festival de référence, programmation, jauge/jour, cadre, soirs, artistes/soir) ; résultats par type de pass, prix par jour, prix par artiste, par cadre et par jauge ; préconisation de prix de pass.
- **Historique** : runs Artiste et Festival (badge de mode), persistés sur disque — survivent aux redémarrages du serveur —, avec critères utilisés, statut, compteurs, bouton **Voir** (recharge les résultats dans le bon onglet) et **Recharger les critères** (pré-remplit le formulaire correspondant).
- **Base** : la base de référence cumulative (voir plus haut) — recherche, correction, exclusion.

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
| `BENCHMARKER_MAX_SEARCH_USES` | `6` | Recherches web max par découverte |
| `BENCHMARKER_URL_REFINE_MAX_USES` | `4` | Recherche ciblée max pour retrouver une URL directe ou une source alternative (par date concernée) |
| `BENCHMARKER_MAX_FETCH_USES` | `2` | Fetch max par date |
| `BENCHMARKER_FETCH_MAX_CONTENT_TOKENS` | `6000` | Plafond de contenu ramené par `web_fetch` |
| `BENCHMARKER_SIMILAR_ARTISTS_MAX_USES` | `4` | Recherches web max pour résoudre un artiste de référence |
| `BENCHMARKER_SIMILAR_ARTISTS_COUNT` | `6` | Nombre d'artistes comparables visés |
| `BENCHMARKER_SIMILAR_FESTIVALS_MAX_USES` | `4` | Recherches web max pour résoudre un festival de référence |
| `BENCHMARKER_SIMILAR_FESTIVALS_COUNT` | `6` | Nombre de festivals comparables visés |
| `BENCHMARKER_DISCOVERY_SEARCH_USES_PER_TARGET` | `2` | Recherches de découverte par artiste/festival ciblé (plafonné par `BENCHMARKER_MAX_SEARCH_USES_CAP`, défaut 20) |
| `BENCHMARKER_LLM_REQUEST_TIMEOUT_S` | `180` | Timeout par appel LLM (allongé automatiquement pour une découverte à gros budget) |
| `BENCHMARKER_EXTRACTION_WORKERS` | `3` | Événements extraits en parallèle (un navigateur headless par fil) |
| `BENCHMARKER_CACHE_DIR` | `cache/prices` | Mémoire des grilles extraites |
| `BENCHMARKER_PRICE_CACHE_TTL_DAYS` | `7` | Durée de mémoire d'une grille trouvée (jours) |
| `BENCHMARKER_PRICE_CACHE_FAIL_TTL_HOURS` | `24` | Durée de mémoire d'un échec (heures) |
| `BENCHMARKER_MIN_PLAUSIBLE_PRICE_EUR` | `5` | Prix plein tarif sous lequel un prix est jugé aberrant |
| `BENCHMARKER_COMPARABILITY_MIN` | `40` | Score sous lequel un événement est hors profil |
| `BENCHMARKER_BASE_PATH` | `data/reference.sqlite` | Base de référence (mémoire cumulative) |
| `BENCHMARKER_BASE_MAX_AGE_DAYS` | `365` | Âge maximal d'une grille en base pour être reprise dans un run |

> Pour minimiser encore le coût : `BENCHMARKER_FETCH_STRATEGY=playwright` supprime totalement le fallback payant (au risque de rater les pages où le rendu ne suffit pas).

## Structure

```
benchmarker/
├── config.py       # modèle, outils, sources, devises
├── models.py       # schéma Pydantic (Concert, Festival, critères des deux modes…)
├── llm.py          # client + boucle d'outils serveur + extraction structurée
├── similar_artists.py  # résolution artiste de référence → artistes comparables
├── festivals.py    # mode Festival : festivals comparables + découverte
├── comparability.py  # score de comparabilité avec la cible
├── price_cache.py  # mémoire des grilles extraites
├── knowledge.py    # base de référence cumulative (SQLite)
├── discovery.py    # étape 1 : découverte des dates
├── extraction.py   # étape 2 : extraction des prix (concerts et festivals)
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
