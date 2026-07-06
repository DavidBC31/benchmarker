# Spécification technique d'hébergement — Benchmarker Billetterie

Document autonome destiné à l'agent/opérateur chargé de déployer l'application.
Aucune connaissance préalable du projet n'est requise. Suivre les sections dans
l'ordre. Une section « Definition of Done » en fin de document liste les
vérifications d'acceptation.

---

## 1. Objectif

Faire tourner **en permanence** l'interface web de l'outil « Benchmarker
Billetterie » sur un **Mac Studio (macOS)**, et l'exposer sur Internet **de
façon sécurisée** via **Cloudflare Tunnel + Cloudflare Access**, sur un
sous-domaine d'un domaine **déjà géré par Cloudflare**.

L'application est un service web Python (Flask servi par waitress) qui, sur
demande, lance des recherches web via l'API Anthropic (Claude) et rend des
pages de billetterie avec un navigateur headless (Playwright/Chromium) pour en
extraire des grilles tarifaires. **Un run consomme des crédits API et dure
plusieurs minutes.** L'accès doit donc être strictement restreint.

### Architecture cible

```
Navigateur ──HTTPS──> Cloudflare (Access = gate identité)
        └── tunnel chiffré sortant ──> cloudflared (Mac Studio)
                                          └──> 127.0.0.1:8080  (app waitress)
```

- L'app **n'écoute qu'en local** (127.0.0.1). Aucun port n'est ouvert sur la box/routeur.
- Le tunnel est **sortant** : cloudflared se connecte à Cloudflare, pas l'inverse.

---

## 2. Source du code

- **Dépôt** : `https://github.com/DavidBC31/benchmarker` (privé — nécessite des identifiants git : PAT GitHub ou SSH configuré sur la machine).
- **Branche à déployer** : `claude/affectionate-edison-cn1yd4`
  ⚠️ Le code vit sur cette branche ; la branche par défaut peut être vide. **Toujours `git checkout` cette branche après clone.**
- Répertoire de travail cible suggéré : `/Users/serveurit/Projets/benchmarker`
  (adapter `<PROJECT_DIR>` partout ci-dessous à l'emplacement réel).

Tous les fichiers de déploiement référencés sont dans `deploy/` :
`run.sh`, `com.benchmarker.web.plist`, `benchmarker.env.example`,
`cloudflared-config.example.yml`, `README.md` (guide pas-à-pas).

---

## 3. Prérequis machine

| Élément | Exigence | Vérif |
|---|---|---|
| macOS | Récent (Apple Silicon OK) | `sw_vers` |
| Python | ≥ 3.9 (système ou Homebrew) | `python3 --version` |
| Homebrew | requis pour `cloudflared` | `brew --version` |
| git | avec accès au dépôt privé | `git --version` |
| Compte Anthropic | **avec crédits API disponibles** | console.anthropic.com → Billing |
| Domaine Cloudflare | déjà actif (nameservers Cloudflare) | dashboard Cloudflare |

> Sans crédits API, l'app démarre mais tout run échoue avec
> `Your credit balance is too low`. Provisionner les crédits avant recette.

---

## 4. Installation de l'application

```bash
# 4.1 Cloner et se placer sur la bonne branche
cd /Users/serveurit/Projets
git clone https://github.com/DavidBC31/benchmarker.git
cd benchmarker
git checkout claude/affectionate-edison-cn1yd4

# 4.2 Environnement virtuel + dépendances
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4.3 Navigateur Playwright (installé dans le profil utilisateur)
python -m playwright install chromium

# 4.4 Dossier de sortie (logs + fichiers de benchmark)
mkdir -p out
```

Dépendances installées (via `requirements.txt`) : `anthropic`, `pydantic`,
`pandas`, `openpyxl`, `python-dateutil`, `playwright`, `flask`, `waitress`.

---

## 5. Configuration (secrets et variables)

Créer le fichier de secrets **non versionné** à la racine du projet :

```bash
cp deploy/benchmarker.env.example benchmarker.env
python -c "import secrets;print('AUTH  =', secrets.token_urlsafe(24))"
python -c "import secrets;print('SECRET=', secrets.token_urlsafe(24))"
chmod 600 benchmarker.env
```

Éditer `benchmarker.env` et renseigner :

| Variable | Rôle | Valeur |
|---|---|---|
| `ANTHROPIC_API_KEY` | Clé API Claude (côté serveur uniquement) | `sk-ant-…` fournie par le propriétaire |
| `BENCHMARKER_AUTH_TOKEN` | Mot de passe de l'interface | valeur `AUTH` générée ci-dessus |
| `BENCHMARKER_SECRET_KEY` | Signature des cookies de session (stable) | valeur `SECRET` générée ci-dessus |
| `BENCHMARKER_HOST` | Interface d'écoute | **`127.0.0.1`** (ne jamais mettre `0.0.0.0`) |
| `BENCHMARKER_PORT` | Port local | `8080` |
| `BENCHMARKER_FETCH_STRATEGY` | Stratégie prix par défaut | `auto` |

Variables optionnelles (défauts raisonnables si absentes) :
`BENCHMARKER_OUT_DIR` (défaut `out`), `BENCHMARKER_MODEL` (défaut
`claude-opus-4-8`), `BENCHMARKER_CHROMIUM_PATH` (auto-détecté).

> **Contrainte de sécurité forte** : la clé API ne doit exister **que** dans
> `benchmarker.env` (chmod 600), jamais en dur dans le code, un plist, un
> commit, ou l'interface web.

---

## 6. Test manuel (avant service)

```bash
bash deploy/run.sh
# Sortie attendue : "Benchmarker web sur http://127.0.0.1:8080"
```

Dans un autre terminal :

```bash
curl -sI http://127.0.0.1:8080/login | head -1   # -> HTTP/1.1 200 OK
curl -sI http://127.0.0.1:8080/            | head -1   # -> 302 (redirige vers /login)
```

Dans un navigateur local : `http://127.0.0.1:8080` → écran de connexion →
saisir `BENCHMARKER_AUTH_TOKEN` → l'interface s'affiche. `Ctrl-C` pour arrêter.

---

## 7. Service permanent (launchd — LaunchAgent)

L'app doit tourner **comme l'utilisateur connecté** (accès au cache Playwright
dans `~/Library/Caches/ms-playwright`). Utiliser un **LaunchAgent**, pas un
Daemon.

```bash
PROJ="/Users/serveurit/Projets/benchmarker"
sed "s#__PROJECT_DIR__#$PROJ#g" "$PROJ/deploy/com.benchmarker.web.plist" \
  > ~/Library/LaunchAgents/com.benchmarker.web.plist
launchctl load -w ~/Library/LaunchAgents/com.benchmarker.web.plist
```

Vérifications :

```bash
launchctl list | grep benchmarker         # présent, code de sortie 0
tail -n 40 "$PROJ/out/web.log"            # "Benchmarker web sur http://127.0.0.1:8080"
curl -sI http://127.0.0.1:8080/login | head -1   # 200
```

Cycle de vie :

```bash
# stop
launchctl unload ~/Library/LaunchAgents/com.benchmarker.web.plist
# start
launchctl load -w ~/Library/LaunchAgents/com.benchmarker.web.plist
```

Le plist a `RunAtLoad=true` et `KeepAlive=true` (redémarrage auto au boot et
après crash). Logs : `out/web.log` et `out/web.err.log`.

---

## 8. Tunnel Cloudflare (domaine existant)

```bash
brew install cloudflared

# 8.1 Authentifier cloudflared sur le compte/domaine Cloudflare
cloudflared tunnel login            # ouvre le navigateur, sélectionner le domaine

# 8.2 Créer le tunnel (génère un <TUNNEL_ID> et un fichier ~/.cloudflared/<TUNNEL_ID>.json)
cloudflared tunnel create benchmarker

# 8.3 Router le sous-domaine vers ce tunnel (crée le CNAME dans Cloudflare)
cloudflared tunnel route dns benchmarker benchmarker.<DOMAINE>
```

Config `~/.cloudflared/config.yml` (modèle : `deploy/cloudflared-config.example.yml`) :

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /Users/serveurit/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: benchmarker.<DOMAINE>
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Installer le tunnel en service (démarre au boot) :

```bash
sudo cloudflared service install
```

Vérifications :

```bash
cloudflared tunnel info benchmarker                 # tunnel HEALTHY, 1+ connexion
curl -sI https://benchmarker.<DOMAINE>/login | head -1   # 200 (ou redirection Access, cf. §9)
```

---

## 9. Sécurité — Cloudflare Access (OBLIGATOIRE)

Sans Access, l'URL publique n'est protégée que par le mot de passe applicatif.
Ajouter une couche d'identité **avant** d'atteindre le Mac :

1. Dashboard Cloudflare → **Zero Trust** → **Access** → **Applications** →
   *Add an application* → **Self-hosted**.
2. **Application domain** : `benchmarker.<DOMAINE>`.
3. **Session duration** : au choix (ex. 24 h).
4. **Policies** → *Add a policy* :
   - Action : **Allow**
   - Rule : **Emails** → liste blanche des adresses autorisées (propriétaire + équipe).
5. Enregistrer.

Effet : Cloudflare exige une vérification d'identité (code e-mail ou IdP) avant
de laisser passer le trafic. Le mot de passe applicatif reste comme second
facteur.

Contrôle : ouvrir `https://benchmarker.<DOMAINE>` depuis un navigateur non
authentifié → doit afficher l'écran Cloudflare Access, **pas** l'application.

---

## 10. Exploitation

| Besoin | Commande |
|---|---|
| Logs application | `tail -f <PROJECT_DIR>/out/web.log <PROJECT_DIR>/out/web.err.log` |
| Redémarrer l'app | `launchctl unload …/com.benchmarker.web.plist && launchctl load -w …/com.benchmarker.web.plist` |
| Mettre à jour le code | `cd <PROJECT_DIR> && git pull` puis redémarrer l'app |
| Changer le mot de passe | éditer `benchmarker.env` puis redémarrer l'app |
| Statut tunnel | `cloudflared tunnel info benchmarker` |
| Fichiers de benchmark produits | `<PROJECT_DIR>/out/benchmark_<jobid>.{csv,json}` + `_synthese.md` |

---

## 11. Contraintes et pièges connus

- **Crédits API** : indispensables pour tout run. Une erreur « credit balance
  too low » n'est pas un bug de déploiement.
- **Playwright sous launchd** : le navigateur est installé dans le profil
  utilisateur → LaunchAgent obligatoire (pas LaunchDaemon), lancé par
  l'utilisateur connecté.
- **Ne pas binder `0.0.0.0`** : l'app doit rester sur `127.0.0.1`. L'exposition
  passe uniquement par le tunnel.
- **`out/` doit exister** avant le chargement du LaunchAgent (les logs y sont
  écrits) — voir §4.4.
- **Durée d'un run** : plusieurs minutes ; l'UI interroge la progression par
  polling. Ne pas placer de timeout agressif côté proxy/Access (garder des
  timeouts généreux).
- **Un seul processus / usage mono-utilisateur** : l'état des jobs est en
  mémoire du process. Un redémarrage de l'app perd les jobs en cours (les
  fichiers déjà écrits dans `out/` restent).
- **Secrets** : `benchmarker.env` est git-ignored et en chmod 600. Ne jamais le
  committer.

---

## 12. Référence — points d'entrée HTTP de l'app

Tous les endpoints (hors `/login`) exigent une session authentifiée.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/login`, POST `/login`, `/logout` | Auth mot de passe applicatif |
| GET | `/` | Interface (formulaire + résultats) |
| POST | `/api/run` | Démarre un benchmark (JSON critères) → `{job_id}` |
| GET | `/api/status/<job_id>` | Progression `{status, log, error}` |
| GET | `/api/result/<job_id>` | Résultats `{summary, rows}` |
| POST | `/api/recommend/<job_id>` | Préconisation tarifaire |
| GET | `/download/<job_id>.<ext>` | Téléchargement `csv`/`json`/`…_synthese.md` |

Écoute : `http://127.0.0.1:8080` (waitress, 8 threads).

---

## 13. Definition of Done (recette)

Le déploiement est validé si **toutes** ces assertions passent :

1. `launchctl list | grep benchmarker` → service présent, dernier code de sortie `0`.
2. `curl -sI http://127.0.0.1:8080/login` → `200 OK`.
3. Après reboot du Mac, l'app et le tunnel redémarrent seuls (re-vérifier 1 & 2).
4. `cloudflared tunnel info benchmarker` → tunnel **HEALTHY** avec au moins une connexion active.
5. `https://benchmarker.<DOMAINE>` depuis un navigateur **non listé** dans Access → écran **Cloudflare Access** (accès refusé sans identité).
6. Depuis un navigateur **autorisé** : passage Access → écran de connexion de l'app → après mot de passe, interface fonctionnelle.
7. Un benchmark de test (ex. 3 dates) lancé depuis l'UI aboutit à un tableau de
   résultats et des fichiers téléchargeables (suppose des crédits API dispo).
8. `benchmarker.env` est en `chmod 600` et **absent** du dépôt git.
9. L'app écoute bien sur `127.0.0.1` uniquement :
   `lsof -iTCP:8080 -sTCP:LISTEN` ne montre que `127.0.0.1:8080`.

---

## 14. Rollback / désinstallation

```bash
# Arrêter et retirer le service app
launchctl unload ~/Library/LaunchAgents/com.benchmarker.web.plist
rm ~/Library/LaunchAgents/com.benchmarker.web.plist

# Retirer le tunnel
sudo cloudflared service uninstall
cloudflared tunnel delete benchmarker
# Puis supprimer l'enregistrement DNS benchmarker.<DOMAINE> dans le dashboard Cloudflare
# et l'application dans Zero Trust → Access.
```
