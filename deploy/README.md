# Hébergement sur Mac Studio + Cloudflare

Objectif : faire tourner l'interface web du benchmarker **en permanence** sur le Mac Studio, et l'exposer **de façon sécurisée** sur Internet via Cloudflare Tunnel — sans ouvrir de port sur ta box.

Architecture :

```
Navigateur ──HTTPS──> Cloudflare (Access) ──tunnel chiffré──> cloudflared (Mac) ──> 127.0.0.1:8080 (appli)
```

Trois couches à mettre en place : **l'appli** (service launchd), **le tunnel** (cloudflared), **l'authentification** (mot de passe appli + Cloudflare Access).

---

## 1. Préparer l'appli

Depuis le dossier du projet, l'environnement virtuel doit exister avec les dépendances installées :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Crée le fichier de secrets (non versionné) :

```bash
cp deploy/benchmarker.env.example benchmarker.env
python -c "import secrets;print('BENCHMARKER_AUTH_TOKEN=', secrets.token_urlsafe(24))"
python -c "import secrets;print('BENCHMARKER_SECRET_KEY=', secrets.token_urlsafe(24))"
```

Édite `benchmarker.env` : colle ta **vraie clé API** dans `ANTHROPIC_API_KEY`, et les deux valeurs générées ci-dessus. **Sécurise le fichier** :

```bash
chmod 600 benchmarker.env
```

Test manuel avant d'en faire un service :

```bash
bash deploy/run.sh
# -> "Benchmarker web sur http://127.0.0.1:8080"
```

Ouvre http://127.0.0.1:8080 dans un navigateur : tu dois tomber sur l'écran de connexion. `Ctrl-C` pour arrêter.

---

## 2. Service permanent (launchd)

Pour que l'appli redémarre toute seule (au boot, ou si elle crashe) :

```bash
# Remplace le chemin par celui de ton projet
PROJ="/Users/serveurit/Projets/benchmarker"
sed "s#__PROJECT_DIR__#$PROJ#g" deploy/com.benchmarker.web.plist \
  > ~/Library/LaunchAgents/com.benchmarker.web.plist
launchctl load -w ~/Library/LaunchAgents/com.benchmarker.web.plist
```

Vérifie :

```bash
launchctl list | grep benchmarker
tail -f out/web.log
```

Arrêt / relance :

```bash
launchctl unload ~/Library/LaunchAgents/com.benchmarker.web.plist   # stop
launchctl load  -w ~/Library/LaunchAgents/com.benchmarker.web.plist # start
```

> C'est un **LaunchAgent** (pas un Daemon) : il tourne comme ton utilisateur, donc il a accès au navigateur Playwright installé dans ton profil.

---

## 3. Tunnel Cloudflare

Prérequis : un **domaine géré par Cloudflare** (sinon, voir « Test rapide » plus bas).

```bash
brew install cloudflared
cloudflared tunnel login                       # ouvre le navigateur, choisis ton domaine
cloudflared tunnel create benchmarker          # crée le tunnel + un fichier <ID>.json
cloudflared tunnel route dns benchmarker benchmarker.tondomaine.fr
```

Crée `~/.cloudflared/config.yml` à partir du modèle :

```bash
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
# édite : mets le <TUNNEL_ID>, le bon chemin credentials, ton hostname
```

Lance le tunnel en service (démarre au boot) :

```bash
sudo cloudflared service install
```

Ton appli est maintenant sur `https://benchmarker.tondomaine.fr`.

### Test rapide sans domaine

Juste pour essayer (URL aléatoire, éphémère, **non protégée par Access**) :

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

Ça imprime une URL `https://xxxx.trycloudflare.com`. À réserver aux tests — le mot de passe de l'appli reste ta seule barrière ici.

---

## 4. Sécurité — Cloudflare Access (fortement recommandé)

L'appli **dépense tes crédits API** : ne la laisse pas accessible au monde entier. Deux barrières :

1. **Mot de passe de l'appli** (`BENCHMARKER_AUTH_TOKEN`) — déjà en place.
2. **Cloudflare Access** (Zero Trust) — filtre *avant* même d'atteindre le Mac :

   - Dashboard Cloudflare → **Zero Trust** → **Access** → **Applications** → *Add an application* → **Self-hosted**.
   - Domaine : `benchmarker.tondomaine.fr`.
   - **Policy** : *Allow*, règle *Emails* → ton adresse (et celles de ton équipe).
   - Cloudflare envoie alors un code à ton e-mail (ou via Google/GitHub) avant de laisser passer.

Avec Access, même si quelqu'un trouve l'URL, il ne verra jamais l'appli sans être dans ta liste.

> Ne mets jamais `BENCHMARKER_HOST=0.0.0.0`. L'appli n'écoute qu'en local ; c'est le tunnel qui l'expose. Garder `127.0.0.1` empêche tout accès direct par le réseau local.

---

## Mémo exploitation

| Besoin | Commande |
|--------|----------|
| Logs appli | `tail -f out/web.log out/web.err.log` |
| Redémarrer l'appli | `launchctl unload …plist && launchctl load -w …plist` |
| Statut tunnel | `cloudflared tunnel info benchmarker` |
| Mettre à jour le code | `git pull` puis redémarrer l'appli |
| Changer le mot de passe | éditer `benchmarker.env` puis redémarrer l'appli |
