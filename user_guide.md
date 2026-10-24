# Guide d'utilisation de ticket2pr

Ce guide explique, étape par étape, comment utiliser ticket2pr de A à Z. Commence par la
partie 1 et avance dans l'ordre : chaque étape ajoute une seule chose à la précédente.

---

## Sommaire

1. [Ce que fait ticket2pr](#1-ce-que-fait-ticket2pr)
2. [Ce qu'il te faut](#2-ce-quil-te-faut)
3. [Installer](#3-installer)
4. [Étape 1 : vérifier que tout marche (gratuit)](#4-étape-1--vérifier-que-tout-marche-gratuit)
5. [Étape 2 : voir le vrai agent travailler (environ 0,20 $)](#5-étape-2--voir-le-vrai-agent-travailler-environ-020-)
6. [Étape 3 : l'essayer sur ton propre code, sans Jira ni forge](#6-étape-3--lessayer-sur-ton-propre-code-sans-jira-ni-forge)
7. [Étape 4 : brancher Jira](#7-étape-4--brancher-jira)
8. [Étape 5 : brancher ta forge (une section par forge)](#8-étape-5--brancher-ta-forge)
   - [GitHub](#81-github-githubcom)
   - [GitHub Enterprise Server](#82-github-enterprise-server)
   - [GitLab (gitlab.com)](#83-gitlab-gitlabcom)
   - [GitLab auto-hébergé](#84-gitlab-auto-hébergé)
   - [Bitbucket Cloud](#85-bitbucket-cloud-bitbucketorg)
   - [Bitbucket Data Center](#86-bitbucket-data-center-auto-hébergé)
   - [Azure DevOps](#87-azure-devops-services-et-server)
   - [Gitea, Forgejo, Codeberg](#88-gitea-forgejo-codeberg)
9. [Étape 6 : traiter un vrai ticket à la main](#9-étape-6--traiter-un-vrai-ticket-à-la-main)
10. [Étape 7 : le mode automatique (webhook Jira)](#10-étape-7--le-mode-automatique-webhook-jira)
11. [Suivre les jobs](#11-suivre-les-jobs)
12. [Ce que tu reçois](#12-ce-que-tu-reçois)
13. [Les réglages utiles](#13-les-réglages-utiles)
14. [La sécurité en 6 règles](#14-la-sécurité-en-6-règles)
15. [Dépannage](#15-dépannage)

---

## 1. Ce que fait ticket2pr

Tu écris un ticket Jira (« la page 2 répète le dernier produit de la page 1 »), tu lui mets
l'étiquette `ticket2pr`, et quelques minutes plus tard tu as une **pull request** prête à
relire, avec le code corrigé, les tests lancés et le coût affiché.

```text
Ticket Jira  ──►  ticket2pr  ──►  agent Claude (dans un bac à sable)
 (étiquette        │                 lit le ticket, modifie le code,
  "ticket2pr")     │                 lance les tests
                   ▼
            Pull request sur ta forge  +  commentaire sur le ticket Jira
            (GitHub, GitLab, Bitbucket,     (lien, résultat des tests, coût)
             Azure DevOps, Gitea...)
```

Ce qu'il faut retenir :

- **Rien n'est fusionné automatiquement.** ticket2pr ouvre une PR ; un humain la relit et la
  fusionne (ou pas).
- **Le coût est plafonné** par ticket (5 $ par défaut).
- **L'agent travaille dans un bac à sable** : il ne voit pas tes secrets et ne peut pas
  pousser de code lui-même. C'est ticket2pr qui pousse, après des vérifications.
- **Si les tests échouent**, la PR est ouverte en **brouillon** (draft), pour que personne ne
  la prenne pour une PR prête.

---

## 2. Ce qu'il te faut

| Quoi | Pourquoi | Comment vérifier |
|---|---|---|
| Linux (ou un conteneur Docker) | le bac à sable utilise bubblewrap, propre à Linux | `uname` affiche `Linux` |
| Python 3.12 ou plus | ticket2pr est écrit en Python | `python3 --version` |
| `uv` | installe les dépendances | `uv --version` (sinon : `curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| `git` | clone et pousse les dépôts | `git --version` |
| `bubblewrap` et `socat` | le bac à sable | `which bwrap socat` (sinon : `sudo apt install -y bubblewrap socat`) |
| Un accès à Claude | l'agent | une clé API (`ANTHROPIC_API_KEY`) **ou** être connecté avec `claude` |

Pour la suite (étapes 4 à 7) il te faudra aussi un compte Jira Cloud et un compte sur ta forge
(GitHub, GitLab, etc.). Pas besoin pour les étapes 1 à 3.

---

## 3. Installer

Dans un terminal :

```bash
cd ~/ticket2pr          # le dossier du projet
uv sync                 # installe tout (une seule fois)
uv run ticket2pr --help # doit afficher la liste des commandes
```

Les commandes que tu vas utiliser :

| Commande | À quoi elle sert |
|---|---|
| `uv run ticket2pr doctor` | vérifie ta configuration et dit ce qui manque |
| `uv run ticket2pr eval` | lance les tâches d'essai fournies avec le projet |
| `uv run ticket2pr run <TICKET>` | traite **un** ticket tout de suite |
| `uv run ticket2pr serve` | démarre le service qui écoute Jira (mode automatique) |
| `uv run ticket2pr jobs` | liste les derniers jobs, leur statut, leur coût |

### Optionnel : économiser des tokens dans ton propre Claude Code

Si tu développes sur ce projet avec Claude Code, tu peux installer des outils qui réduisent
ta consommation de tokens : les plugins **claude-mem**, **caveman** et **ponytail**, et les
outils **rtk**, **pxpipe** et **graphify**.

```bash
make dev-tools              # ou, sans make : bash scripts/dev-tools.sh
```

- Le script saute ce qui est déjà installé : tu peux le relancer sans risque.
- Il modifie ta configuration personnelle (`~/.claude`, `~/.local`), pas le projet.
- Il télécharge et exécute l'installateur officiel de rtk (version figée) : lis le script
  avant si tu veux savoir exactement ce qu'il fait.
- Redémarre Claude Code ensuite.
- Ces outils servent **à toi**, pas à l'agent de ticket2pr : l'agent ne charge volontairement
  aucun plugin (un plugin s'exécuterait hors du bac à sable, avec les secrets du service).

---

## 4. Étape 1 : vérifier que tout marche (gratuit)

Le projet contient 4 petits problèmes d'essai, avec leur solution. Cette commande les
« résout » avec la solution connue, sans IA : c'est gratuit et ça vérifie que toute la chaîne
fonctionne (clonage, tests, création de la PR).

```bash
uv run ticket2pr eval --agent oracle
```

Résultat attendu, à la fin :

```text
Resolved 100% of 4 task(s) · total $0.00
```

Si tu vois `the sandbox cannot start (missing: socat)`, installe ce qui manque
(`sudo apt install -y bubblewrap socat`) et relance.

---

## 5. Étape 2 : voir le vrai agent travailler (environ 0,20 $)

Même chose, mais cette fois c'est Claude qui cherche et écrit la correction.

1. Donne ta clé Claude (ou utilise ta connexion `claude` si tu en as une) :

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

2. Lance une seule tâche :

   ```bash
   uv run ticket2pr eval --agent claude --task paginate
   ```

Résultat attendu :

```text
 Task      Ticket    Status     Resolved  Cost   Tokens  Turns
 paginate  SHOP-101  succeeded  yes       $0.17  87,140  10
```

`Resolved: yes` veut dire que les tests cachés de la tâche passent avec le code écrit par
l'agent.

---

## 6. Étape 3 : l'essayer sur ton propre code, sans Jira ni forge

Ici tu utilises un dépôt git sur ton disque et un ticket écrit dans un fichier. Rien ne part
sur Internet à part les appels à Claude.

### 6.1 Prépare les dossiers

```text
~/essai/
├── repos/
│   └── acme/
│       └── shop/        ← ton dépôt git (un "git clone" ou une copie)
└── tickets/
    └── SHOP-1.yaml      ← le ticket
```

```bash
mkdir -p ~/essai/repos/acme ~/essai/tickets
git clone <url-de-ton-dépôt> ~/essai/repos/acme/shop
```

Le dépôt doit avoir une branche `main` (sinon, ajoute `T2P_BASE_BRANCH=master` à l'étape
suivante).

### 6.2 Écris le ticket

Fichier `~/essai/tickets/SHOP-1.yaml` :

```yaml
title: La page 2 de la liste des produits répète le dernier produit de la page 1
description: |
  Avec 10 produits par page, le produit n°10 apparaît aussi en tête de la page 2.
  Corrige la pagination et ajoute un test.
labels: [ticket2pr]
```

- Le nom du fichier est la clé du ticket (`SHOP-1`).
- `labels` doit être une **liste** (avec les crochets).

### 6.3 Lance

```bash
cd ~/ticket2pr
uv run ticket2pr run SHOP-1 --repo acme/shop \
  --tickets-dir ~/essai/tickets --repos-dir ~/essai/repos
```

### 6.4 Regarde le résultat

- La branche `ticket2pr/shop-1` a été poussée dans `~/essai/repos/acme/shop` :

  ```bash
  git -C ~/essai/repos/acme/shop log --oneline ticket2pr/shop-1 -3
  git -C ~/essai/repos/acme/shop diff main ticket2pr/shop-1
  ```

- La « pull request » est un fichier JSON dans `~/essai/repos/_prs/` (titre, description,
  brouillon ou non).
- Le « commentaire Jira » est dans `~/essai/tickets/SHOP-1.comments.md`.

Les tests sont détectés tout seuls (pytest, npm test, go test, cargo test, make test...). Si ça
ne suffit pas, indique la commande : `export T2P_TEST_COMMAND="uv run pytest -q"`.

---

## 7. Étape 4 : brancher Jira

### 7.1 Crée un compte bot (conseillé) et son token

1. Crée un utilisateur Jira dédié, par exemple `ticket2pr-bot@ta-societe.com`, qui peut
   **lire** les projets concernés et **commenter**.
2. Connecté avec ce compte, va sur
   <https://id.atlassian.com/manage-profile/security/api-tokens> → **Create API token** →
   copie le token.

### 7.2 Crée ton fichier de configuration

```bash
cd ~/ticket2pr
cp .env.example .env
```

Ouvre `.env` et remplis la partie Jira :

```dotenv
ANTHROPIC_API_KEY=sk-ant-...

T2P_JIRA_BASE_URL=https://ta-societe.atlassian.net
T2P_JIRA_EMAIL=ticket2pr-bot@ta-societe.com
T2P_JIRA_API_TOKEN=<le token créé ci-dessus>
T2P_JIRA_WEBHOOK_SECRET=<une longue phrase aléatoire, servira à l'étape 7>
T2P_TRIGGER_LABEL=ticket2pr
```

Pour générer un secret aléatoire : `openssl rand -hex 32`.

### 7.3 Dis quel projet Jira va dans quel dépôt

C'est la ligne `T2P_REPOS`. La clé est le préfixe des tickets Jira (`SHOP` pour `SHOP-42`) :

```dotenv
T2P_REPOS={"SHOP": "acme/shop", "API": "acme/backend"}
```

Le format du dépôt dépend de ta forge : voir l'étape suivante.

---

## 8. Étape 5 : brancher ta forge

Une **forge**, c'est le site qui héberge ton code et ses pull requests : GitHub, GitLab, etc.
Pour toutes les forges, c'est le même principe, avec 3 lignes dans `.env` :

```dotenv
T2P_SCM_PROVIDER=<github | gitlab | bitbucket | bitbucket-server | azure | gitea>
T2P_SCM_TOKEN=<le token créé sur la forge>
T2P_SCM_API_URL=<seulement pour une forge auto-hébergée>
```

**Conseil valable partout** : crée un **compte bot** dédié sur ta forge et protège ta
branche principale pour que personne (bot compris) ne puisse fusionner sans relecture.

**Le mode fork (conseillé quand c'est possible)** : au lieu de pousser sa branche dans ton
dépôt, ticket2pr la pousse dans une **copie (fork) appartenant au bot**, et ouvre la PR depuis
ce fork. Avantage : la CI de ton dépôt, qui a accès à tes secrets, ne tourne pas sur du code
que personne n'a encore relu. Pour l'activer : crée le fork une fois avec le compte bot, puis
ajoute `T2P_FORK_OWNER=<nom-du-bot>` dans `.env`.

Une fois ta forge configurée, vérifie :

```bash
uv run ticket2pr doctor
```

Chaque ligne doit être verte (✓). Une ligne jaune (!) est une recommandation, pas une erreur.

### 8.1 GitHub (github.com)

**Créer le token**

1. Avec le compte bot : *Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token*.
2. *Repository access* : **Only select repositories** → choisis tes dépôts.
3. *Permissions* : **Contents : Read and write** et **Pull requests : Read and write**.
   Rien d'autre.

**Configuration**

```dotenv
T2P_SCM_PROVIDER=github
T2P_SCM_TOKEN=github_pat_...
T2P_REPOS={"SHOP": "acme/shop"}
```

**Mode fork** : oui. Attention, un token « fine-grained » ne couvre qu'un seul propriétaire.
Comme le fork appartient au bot et le dépôt à ton organisation, il faut soit un token
*classic* avec le droit `public_repo` (ou `repo` pour un dépôt privé), soit une GitHub App
installée sur les deux.

```dotenv
T2P_FORK_OWNER=acme-bot
```

**Brouillons** : natifs. Si ton offre GitHub ne permet pas les brouillons, une PR normale est
ouverte et signalée comme « tests en échec ».

> L'ancien nom `T2P_GITHUB_TOKEN` fonctionne toujours.

### 8.2 GitHub Enterprise Server

Comme GitHub, avec l'adresse de ton serveur suivie de `/api/v3` :

```dotenv
T2P_SCM_PROVIDER=github
T2P_SCM_TOKEN=...
T2P_SCM_API_URL=https://github.ta-societe.com/api/v3
T2P_REPOS={"SHOP": "acme/shop"}
```

### 8.3 GitLab (gitlab.com)

**Créer le token**

1. Dans le projet : *Settings → Access tokens → Add new token*.
2. *Role* : **Developer**. *Scopes* : **api** (GitLab n'a pas de droit plus fin pour ouvrir
   des merge requests).

**Configuration**

```dotenv
T2P_SCM_PROVIDER=gitlab
T2P_SCM_TOKEN=glpat-...
T2P_REPOS={"SHOP": "acme/equipe-web/shop"}
```

Le dépôt s'écrit avec **tous ses groupes** : `groupe/sous-groupe/projet`, comme dans l'adresse
du projet.

**Mode fork** : oui, mais avec le **token personnel du compte bot** (*Preferences → Access
tokens*, scope `api`), car un token de projet ne voit pas le fork du bot.

**Brouillons** : GitLab n'a pas de case « brouillon » dans son API ; ticket2pr met `Draft: ` au
début du titre, ce qui est la manière officielle de GitLab.

**Bon à savoir** : ticket2pr neutralise les commandes rapides GitLab (`/merge`, `/approve`...)
que l'agent pourrait écrire dans la description.

### 8.4 GitLab auto-hébergé

Comme GitLab.com, avec l'adresse de ton serveur suivie de `/api/v4` :

```dotenv
T2P_SCM_PROVIDER=gitlab
T2P_SCM_TOKEN=glpat-...
T2P_SCM_API_URL=https://gitlab.ta-societe.com/api/v4
T2P_REPOS={"SHOP": "acme/shop"}
```

### 8.5 Bitbucket Cloud (bitbucket.org)

**Créer le token**

1. Dans le dépôt : *Repository settings → Security → Access tokens → Create access token*
   (ou au niveau du workspace pour plusieurs dépôts).
2. *Scopes* : **Repositories : Write** et **Pull requests : Write**.

**Configuration**

```dotenv
T2P_SCM_PROVIDER=bitbucket
T2P_SCM_TOKEN=ATCTT3x...
T2P_REPOS={"SHOP": "mon-workspace/shop"}
```

**Mode fork** : non (un token d'accès Bitbucket ne couvre qu'un dépôt ou un workspace, donc il
ne peut pas atteindre à la fois le fork du bot et ton dépôt). ticket2pr refuse
`T2P_FORK_OWNER` avec Bitbucket Cloud. Protège plutôt ta CI pour qu'elle ne lance rien de
sensible sur les branches `ticket2pr/*`.

**Brouillons** : natifs ; si refusés, PR normale.

### 8.6 Bitbucket Data Center (auto-hébergé)

**Créer le token**

1. *Repository settings → HTTP access tokens → Create token* (ou dans ton profil :
   *Manage account → HTTP access tokens*).
2. *Permissions* : **Repository write**.

**Configuration** (l'adresse de l'API est obligatoire) :

```dotenv
T2P_SCM_PROVIDER=bitbucket-server
T2P_SCM_TOKEN=...
T2P_SCM_API_URL=https://bitbucket.ta-societe.com/rest/api/1.0
T2P_REPOS={"SHOP": "PROJ/shop"}
```

Le dépôt s'écrit `CLÉ-DU-PROJET/nom-du-dépôt` (la majuscule ou minuscule n'a pas
d'importance).

**Mode fork** : oui, avec le token **personnel** du bot. Le fork d'un utilisateur s'appelle
`~nom` : `T2P_FORK_OWNER=~ticket2pr-bot`.

**Brouillons** : natifs à partir de la version 8.18 ; avant, PR normale.

### 8.7 Azure DevOps (Services et Server)

**Créer le token**

1. *User settings* (icône en haut à droite) → *Personal access tokens → New Token*.
2. *Scopes* : **Code → Read & write**. Rien d'autre.

**Configuration (Azure DevOps Services, dev.azure.com)**

```dotenv
T2P_SCM_PROVIDER=azure
T2P_SCM_TOKEN=<le PAT>
T2P_REPOS={"SHOP": "mon-organisation/Mon Projet/shop"}
```

Le dépôt s'écrit `organisation/projet/dépôt`. Les espaces dans le nom du projet ou du dépôt
sont acceptés.

**Configuration (Azure DevOps Server, sur tes serveurs)**

```dotenv
T2P_SCM_PROVIDER=azure
T2P_SCM_TOKEN=<le PAT>
T2P_SCM_API_URL=https://tfs.ta-societe.com/tfs
T2P_REPOS={"SHOP": "DefaultCollection/Mon Projet/shop"}
```

Sur un Server, l'adresse est celle du serveur (avec `/tfs` si ton installation l'utilise) et
la **collection** prend la place de l'organisation dans `T2P_REPOS`. Les versions 2020 et
plus récentes sont prises en charge.

**Mode fork** : non (refusé par ticket2pr).

**Brouillons** : natifs.

**Bon à savoir** : Azure DevOps limite la description d'une PR à 4 000 caractères ; au-delà,
la fin (le résumé de l'agent) est coupée.

### 8.8 Gitea, Forgejo, Codeberg

**Créer le token**

1. Avec le compte bot : *Settings → Applications → Generate New Token*.
2. Droits : **repository : Read and Write**.

**Configuration** (l'adresse de l'API est obligatoire) :

```dotenv
T2P_SCM_PROVIDER=gitea
T2P_SCM_TOKEN=...
T2P_SCM_API_URL=https://codeberg.org/api/v1
T2P_REPOS={"SHOP": "acme/shop"}
```

Pour ton propre serveur Gitea ou Forgejo : `https://git.ta-societe.com/api/v1`.

**Mode fork** : oui, avec le token du bot : `T2P_FORK_OWNER=ticket2pr-bot`.

**Brouillons** : ticket2pr met `WIP: ` au début du titre, ce que Gitea et Forgejo
reconnaissent comme brouillon.

---

## 9. Étape 6 : traiter un vrai ticket à la main

Jira et ta forge sont configurés. Avant d'automatiser, teste sur un vrai ticket :

```bash
cd ~/ticket2pr
uv run ticket2pr doctor          # tout doit être vert
uv run ticket2pr run SHOP-42     # un vrai ticket Jira
```

- Avec `run`, l'étiquette `ticket2pr` n'est **pas** obligatoire (ajoute `--require-label` pour
  l'exiger).
- Le dépôt est trouvé grâce à `T2P_REPOS` (`SHOP` → `acme/shop`). Pour forcer un autre dépôt :
  `--repo acme/autre`.
- À la fin, la commande affiche le statut, le coût et le lien de la PR.

---

## 10. Étape 7 : le mode automatique (webhook Jira)

Ici ticket2pr tourne en continu et réagit tout seul quand quelqu'un ajoute l'étiquette
`ticket2pr` à un ticket.

### 10.1 Démarre le service

Au choix :

```bash
# directement (sur Linux, avec le bac à sable)
uv run ticket2pr serve            # écoute sur 127.0.0.1:8080
```

```bash
# ou avec Docker (le conteneur sert alors de bac à sable)
docker compose up --build -d
```

Vérifie qu'il répond : `curl http://127.0.0.1:8080/healthz`.

### 10.2 Rends-le joignable par Jira

Jira Cloud doit pouvoir appeler ton service depuis Internet. Mets-le derrière ton reverse
proxy (HTTPS) ou, pour un essai, un tunnel (par exemple `cloudflared tunnel --url
http://127.0.0.1:8080`). Note l'adresse publique obtenue, par exemple
`https://ticket2pr.ta-societe.com`.

### 10.3 Crée le webhook dans Jira

1. Jira : *Settings (⚙) → System → WebHooks → Create a WebHook*.
2. *URL* : `https://ticket2pr.ta-societe.com/webhooks/jira`
3. *Secret* : exactement la valeur de `T2P_JIRA_WEBHOOK_SECRET`.
4. *Events* : **Issue → created** et **Issue → updated**.
5. Enregistre.

### 10.4 Utilise-le

Ajoute l'étiquette `ticket2pr` à un ticket d'un projet présent dans `T2P_REPOS`. C'est tout :
le job démarre, et le ticket reçoit un commentaire avec le lien de la PR quand c'est fini.

Pour relancer un ticket (par exemple après avoir précisé la description) : retire l'étiquette
puis remets-la. La PR existante est mise à jour, pas dupliquée.

### 10.5 Tester sans Jira

Tu peux simuler un appel de Jira :

```bash
export T2P_JIRA_WEBHOOK_SECRET=<ton secret>
./examples/send-webhook.sh examples/jira-issue-created.json
```

---

## 11. Suivre les jobs

```bash
uv run ticket2pr jobs                     # les 20 derniers
uv run ticket2pr jobs --status failed     # seulement les échecs
```

Il existe aussi une petite API, si tu définis `T2P_API_TOKEN` dans `.env` :

```bash
curl -H "Authorization: Bearer $T2P_API_TOKEN" http://127.0.0.1:8080/jobs
curl -H "Authorization: Bearer $T2P_API_TOKEN" http://127.0.0.1:8080/jobs/12
```

Les statuts possibles :

| Statut | Signification |
|---|---|
| `queued` | en attente |
| `running` | l'agent travaille |
| `succeeded` | PR ouverte, tests verts |
| `needs_review` | PR ouverte mais tests rouges ou absents (souvent en brouillon) |
| `failed` | pas de PR : budget épuisé, aucun changement, règle de sécurité... (la raison est indiquée) |
| `skipped` | le ticket n'a pas l'étiquette |

---

## 12. Ce que tu reçois

**Sur la forge**, une branche `ticket2pr/<clé-du-ticket>` (par exemple `ticket2pr/shop-42`)
et une PR qui contient :

- le lien vers le ticket ;
- le résultat des tests (et la fin de leur sortie s'ils échouent) ;
- la taille du changement (fichiers, lignes ajoutées et supprimées) ;
- le modèle, le coût, les tokens, le nombre de tours et la durée ;
- les actions que les garde-fous ont bloquées, s'il y en a ;
- le résumé écrit par l'agent.

**Sur le ticket Jira**, un commentaire : lien de la PR, état des tests, coût. En cas d'échec, la
raison.

**Ce que tu fais ensuite** : tu relis la PR comme celle d'un collègue, tu demandes des
changements (en précisant le ticket et en relançant) ou tu fusionnes.

---

## 13. Les réglages utiles

Tous les réglages sont dans `docs/configuration.md`. Les plus utiles, à mettre dans `.env` :

| Réglage | Défaut | À quoi il sert |
|---|---|---|
| `T2P_MAX_BUDGET_USD` | `5` | plafond de dépense par ticket (retry compris) |
| `T2P_MAX_TURNS` | `80` | nombre maximum d'actions de l'agent par essai |
| `T2P_MODEL` | `claude-opus-5-5` | le modèle Claude utilisé |
| `T2P_EFFORT` | `high` | effort de réflexion : `low` … `max` |
| `T2P_TEST_COMMAND` | auto | la commande de tests, si la détection ne suffit pas |
| `T2P_TEST_NETWORK` | `false` | autoriser le réseau pendant les tests (ex. `uv run` qui installe) |
| `T2P_BASE_BRANCH` | `main` | la branche de départ et la cible des PR |
| `T2P_MAX_CHANGED_FILES` | `25` | au-delà, pas de PR (changement trop gros) |
| `T2P_PROTECTED_PATHS` | CI, `.env`, clés | fichiers que ticket2pr ne commitera jamais |
| `T2P_NETWORK_ALLOWLIST` | registres de paquets | sites que l'agent peut joindre |

Pour aider l'agent, tu peux ajouter dans ton dépôt un fichier `AGENTS.md`, `CLAUDE.md` ou
`CONTRIBUTING.md` qui explique comment le projet est organisé et comment lancer les tests :
ticket2pr le lui transmet.

---

## 14. La sécurité en 6 règles

1. **Des tokens au strict minimum** (voir chaque forge plus haut) et un **compte bot** dédié.
2. **Protège ta branche principale** : personne ne fusionne sans relecture.
3. **Active le mode fork** quand ta forge le permet, sinon empêche ta CI de donner des
   secrets aux branches `ticket2pr/*`.
4. **Garde le bac à sable activé** (`T2P_SANDBOX=true`) ; ne le désactive que dans un
   conteneur isolé (c'est ce que fait `docker compose`).
5. **Une clé Claude dédiée** avec une limite de dépense côté Anthropic.
6. **Ne mets pas de secrets ailleurs** que dans le dossier de ticket2pr, son `.env` ou son
   dossier de données : le reste du disque est lisible (en lecture seule) par l'agent.

Le détail est dans `docs/security.md`.

---

## 15. Dépannage

Toujours commencer par :

```bash
uv run ticket2pr doctor
```

| Message ou symptôme | Cause probable | Solution |
|---|---|---|
| `the sandbox cannot start (missing: socat)` | outil du bac à sable absent | `sudo apt install -y bubblewrap socat` |
| `bwrap cannot start here` | conteneur sans droits de namespaces | lancer dans Docker avec `T2P_SANDBOX=false` (comme `docker compose`) |
| `... needs T2P_SCM_TOKEN` | token de forge vide | remplir `T2P_SCM_TOKEN` |
| `... needs T2P_SCM_API_URL` | forge auto-hébergée sans adresse | remplir `T2P_SCM_API_URL` (voir ta forge) |
| `invalid gitlab repository for SHOP` | mauvais format dans `T2P_REPOS` | suivre le format de ta forge (section 8) |
| `T2P_FORK_OWNER is not supported with azure` | pas de mode fork sur cette forge | retirer `T2P_FORK_OWNER` |
| `no repository mapped for SHOP-42 (T2P_REPOS)` | projet absent de `T2P_REPOS` | ajouter `"SHOP": "..."` |
| `already has a job in progress` | un job tourne déjà pour ce ticket | attendre, ou si un `run` a été tué, démarrer `serve` une fois pour le libérer |
| job `failed`, « budget » | le plafond est atteint | préciser le ticket ou monter `T2P_MAX_BUDGET_USD` |
| job `failed`, « without changing any file » | l'agent n'a rien modifié | rendre le ticket plus précis |
| job `failed`, « only touches protected paths » | l'agent n'a modifié que des fichiers protégés (CI...) | c'est voulu : ces fichiers se modifient à la main |
| job `needs_review` | tests rouges ou introuvables | relire la PR ; définir `T2P_TEST_COMMAND` si les tests ne sont pas trouvés |
| la PR n'est pas mise à jour, une nouvelle est refusée | quelqu'un a poussé sur la branche du bot | normal : ticket2pr n'écrase jamais le travail d'un humain |

Pour garder le dossier de travail d'un job et regarder ce que l'agent a fait :
`uv run ticket2pr run SHOP-42 --keep-workspace`.

---

© 2026 Chawki JABALLI. Distribué sous [licence MIT](LICENSE).
