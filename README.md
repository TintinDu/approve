# mr-approver

Un petit outil qui tourne dans un terminal et, toutes les 5 minutes :

1. **approuve** les merge requests GitLab des collègues que tu lui désignes ;
2. (si Slack est branché) **ajoute ta réaction** (`:lgtm:` par défaut) sur le message Slack qui annonçait la MR.

Il n'utilise **aucune IA** : ce sont de simples appels à GitLab et à Slack, donc il ne coûte rien à faire tourner.

> **Avertissement** : l'outil approuve des merge requests **sans relire le code**. À n'utiliser que là où ton équipe l'a convenu. Une approbation automatique ne remplace pas une revue.

Ce qu'il **ne fait jamais** : merger, commenter, résoudre un fil de discussion, approuver un brouillon (« draft ») ou une MR dont le titre contient « DO NOT MERGE ».

---

## Démarrage en 2 minutes (GitLab seul, aucune configuration)

Il te faut Python 3.11+ et [`glab`](https://gitlab.com/gitlab-org/cli) connecté à ton GitLab :

```bash
brew install python glab
glab auth login --hostname <ton-serveur-gitlab>
./install.sh                              # installe la commande « approve »
approve alice:all --dry-run --once        # essai à blanc : montre ce qui serait approuvé, n'écrit rien
approve alice:all                         # pour de vrai
```

Remplace `alice` par le **username GitLab** d'un collègue. `:all` veut dire « toutes ses MR ouvertes ».

**Aucun fichier à écrire** : l'outil retrouve tout seul ton serveur et ton compte depuis `glab`. (Si `glab` est connecté à plusieurs serveurs, ajoute `--host ton-serveur`.)

> `install.sh` te dit si `~/.local/bin` n'est pas dans ton `PATH` et quelle ligne ajouter.

Ça tourne jusqu'à **Ctrl-C**.

### Ce que Slack ajoute

Sans Slack, il n'y a ni réaction, ni possibilité de n'approuver que les MR **annoncées** dans Slack. Branche Slack si tu veux ça (voir [Brancher Slack](#brancher-slack)) ; tout le reste marche déjà.

---

## Brancher Slack

Lance simplement :

```bash
approve --init
```

L'outil détecte ce qui est possible et te guide. **Deux façons** de lui donner accès à Slack :

### Option A : tu as Hermes (aucun jeton à créer)

Si [Hermes](#à-propos-de-hermes) est installé et autorisé sur Slack, `approve --init` le **détecte tout seul** et réutilise cette autorisation. Rien d'autre à faire.

### Option B : un jeton Slack à toi (sans Hermes)

`approve --init` te demande un jeton, et t'explique comment l'obtenir en 4 clics :

1. Ouvre <https://api.slack.com/apps> et choisis **Create New App > From an app manifest**.
2. Choisis ton espace de travail et colle le contenu du fichier [`slack-app-manifest.json`](slack-app-manifest.json) de ce dépôt.
3. Clique **Install to Workspace** (ton administrateur Slack peut devoir l'approuver).
4. Menu **OAuth & Permissions** : copie le **User OAuth Token** (il commence par `xoxp-`) et colle-le quand `approve --init` le demande.

Le jeton est rangé dans `~/.mr-approver/slack_token` (lisible par toi seul). Il donne trois droits seulement : chercher des messages, lire et poser des réactions. L'outil vérifie ces droits à l'enregistrement du jeton.

Tu peux aussi le fournir par la variable `MR_APPROVER_SLACK_TOKEN` (il n'est alors pas écrit sur le disque).

Pour forcer le jeton alors que Hermes est installé : `approve --init --token`.

Ensuite : `approve --doctor` vérifie tout.

---

## Utilisation

| Commande | Effet |
|---|---|
| `approve alice:all` | boucle : approuve **toutes** les MR ouvertes d'`alice` |
| `approve alice` | boucle : n'approuve que les MR d'`alice` dont le **lien a été annoncé sur Slack** (demande Slack) |
| `approve alice bob:all` | plusieurs personnes à la fois |
| `approve alice:all --dry-run --once` | simulation, une seule passe, n'écrit rien |
| `approve --why alice bob` | **explique** pourquoi chaque MR ouverte est (ou n'est pas) approuvée ; lecture seule, utilisable pendant qu'une instance tourne |
| `approve add dave` / `approve remove dave` / `approve list` | gère la **liste mémorisée** de personnes |
| `approve` (sans nom) | lance la boucle sur la liste mémorisée |
| `approve --stop` | arrête proprement l'instance qui tourne, **d'où qu'elle ait été lancée** |
| `approve --update` | met l'outil à jour et montre les nouveautés (voir [Mettre à jour](#mettre-à-jour)) |
| `approve --version` | affiche la version installée |
| `approve --status` | est-ce que ça tourne ? bilan des dernières 24 h |
| `approve --pause` / `approve --resume` | suspend / reprend sans tuer le processus |
| `approve --init` | branche Slack et écrit la configuration |
| `approve --doctor` | diagnostic de l'installation |
| `approve --help` | liste de toutes les options |

Sans accès Slack, `approve alice` (sans `:all`) est refusé avec la commande exacte à taper, plutôt que d'approuver à l'aveugle.

### Pourquoi cette MR n'est pas approuvée ?

```bash
approve --why alice bob
```

Pour chaque MR ouverte, l'outil indique le verdict et la raison : **à approuver**, **non annoncée** (aucun lien dans Slack), **non approuvable** (tu n'es pas approbateur), **DO NOT MERGE**, **brouillon**. Les MR déjà approuvées sont simplement comptées.

### La liste mémorisée

```bash
approve add dave carol:all     # mémorise ces personnes (le nom est vérifié tout de suite)
approve list                   # montre la liste et le mode de chacun
approve                        # lance la boucle sur la liste
approve remove dave
```

Les noms écrits sur la ligne de commande l'emportent sur la liste.

### Le mode de chaque personne

- **Sans suffixe** : seulement les MR dont le lien est annoncé sur Slack (mode prudent).
- **`:all`** : toutes ses MR ouvertes. À réserver aux personnes dont tu approuves tout.
- **`:announced`** : force le mode prudent.

Si le nom que tu tapes désigne la même personne qu'un raccourci de ta config (par exemple `alice.martin` pour le raccourci `alice`), elle **garde le mode du raccourci**. L'outil rappelle le mode de chacun à chaque lancement.

---

## Comment ça décide

- La MR doit être **ouverte** et **hors brouillon**.
- Elle n'est approuvée que si **tu es un approbateur autorisé** pour elle (sinon GitLab refuse, et l'outil l'ignore sans bruit).
- L'approbation est liée à la **version exacte** de la MR. Si le collègue pousse un nouveau commit et que GitLab annule ton approbation, elle est **rétablie** à la passe suivante.
- La réaction Slack n'est posée que si **toutes** les MR citées dans le même message sont approuvées par toi. Les messages de bots sont ignorés.
- Une MR que tu as approuvée puis qui a été mergée avant que son annonce soit trouvée reste suivie 48 h (`merged_grace_hours`), pour que la réaction Slack soit posée quand même.

### Garde-fous

- **Plafond** : au-delà de 30 approbations dans une même passe, l'outil **s'arrête**, te notifie (macOS) et l'écrit dans le journal. Il ne continue pas en silence. Change-le avec `--max-approvals N` ou dans la config.
- **Une seule instance** à la fois (un verrou l'empêche de se lancer deux fois).
- **Alerte** après 3 passes en échec d'affilée.
- **Aucune écriture** en mode `--dry-run`.

---

## Configuration (facultative)

Sans fichier, tout est détecté. `approve --init` en écrit un pour toi quand tu branches Slack. Tu n'as normalement **rien à éditer**.

Tout est rangé dans un seul dossier, `~/.mr-approver/` :

| Fichier | Contenu |
|---|---|
| `config.json` | ta configuration (lisible par toi seul) |
| `slack_token` | ton jeton Slack, si tu as choisi l'option B |
| `journal.jsonl` | historique de chaque approbation, réaction et erreur |
| `state/` | état interne (verrou, mémoire des annonces trouvées) |

### Variables d'environnement (toutes facultatives)

| Variable | Rôle |
|---|---|
| `MR_APPROVER_HOME` | autre dossier que `~/.mr-approver` |
| `APPROVE_CONFIG` | chemin direct d'un fichier de config |
| `MR_APPROVER_SLACK_TOKEN` | jeton Slack, sans l'écrire sur le disque |
| `HERMES_HOME` | emplacement de Hermes, s'il n'est pas dans `~/.hermes` |
| `MR_APPROVER_PYTHON` | Python à utiliser (3.11 ou plus) |

### Les réglages de `config.json`

| Clé | Défaut | Rôle |
|---|---|---|
| `gitlab_host` | *(détecté)* | serveur GitLab |
| `me.gitlab_id` | *(détecté)* | ton identifiant GitLab |
| `me.slack_id` | *(déduit de Slack)* | ton identifiant de membre Slack (facultatif) |
| `slack_backend` | `auto` | `auto` (jeton s'il existe, sinon Hermes), `token` ou `hermes` |
| `emoji` | `lgtm` | réaction posée (sans les `:`) |
| `exclude_channels` | `[]` | canaux Slack où ne **jamais** réagir (nom ou identifiant) |
| `interval_seconds` | `300` | pause entre deux passes |
| `max_approvals_per_pass` | `30` | plafond d'approbations par passe |
| `rediscovery_minutes` | `60` | fréquence de re-vérification des annonces déjà trouvées |
| `merged_grace_hours` | `48` | durée pendant laquelle on suit une MR mergée pour sa réaction |
| `reverify_minutes` | `30` | une MR dont le code n'a pas changé n'est pas revérifiée avant ce délai |
| `people` | `{}` | raccourcis facultatifs (voir ci-dessous) |

**Raccourcis (`people`)** : facultatif. Pour taper `approve al` au lieu de `approve alice.martin`, ajoute :

```json
"people": { "al": { "gitlab_id": 1001, "require_announcement": true } }
```

---

## Mettre à jour

```bash
approve --update
```

La commande récupère les nouveautés de `main` et les liste. Elle **refuse de continuer** si tu as des modifications locales, si tu n'es pas sur la branche `main` ou si ta copie a divergé : elle ne touche alors à rien. Ta config, ta liste et ton historique (`~/.mr-approver/`) ne sont jamais modifiés.

Une instance déjà lancée garde l'ancien code : relance-la après la mise à jour (`approve --stop`, puis `approve`). `approve --doctor` vérifie ensuite que tout est en ordre.

Équivalent à la main : `git -C <dossier du dépôt> pull`.

---

## Rapidité

Une passe complète prend **quelques secondes** (4 à 5 s pour une soixantaine de MR). Pour y arriver :

- **un seul balayage** des messages Slack récents par passe, au lieu d'une recherche par MR (chaque MR est quand même cherchée une fois à sa première apparition, puis environ toutes les heures par sécurité) ;
- une MR dont le dernier commit n'a pas changé n'est pas revérifiée pendant `reverify_minutes` ;
- les MR et les personnes sont traitées **en parallèle** ;
- les MR mergées sans annonce ne sont plus surveillées pour rien.

La toute première passe, ou après un effacement de `~/.mr-approver/state/`, est plus longue (une recherche par MR).

---

## Dépannage

Commence toujours par **`approve --doctor`** : il teste chaque maillon et indique la correction.

| Symptôme | Cause probable | Solution |
|---|---|---|
| « Sans accès Slack… » | tu as tapé `approve alice` sans Slack branché | `approve alice:all`, ou `approve --init` pour brancher Slack |
| « le serveur GitLab n'a pas pu être deviné » | `glab` est connecté à plusieurs serveurs, ou à aucun | ajoute `--host ton-serveur`, ou `glab auth login` |
| « Ce jeton ne fonctionne pas » / `invalid_auth` | jeton faux, révoqué, ou copié avec un espace | recrée-le et relance `approve --init` |
| « Il manque : reactions:write… » | l'application Slack n'a pas tous les droits | ajoute-les dans **OAuth & Permissions > User Token Scopes**, réinstalle l'app, recopie le jeton |
| Erreur de certificat HTTPS (réseau d'entreprise) | un proxy d'inspection TLS | définis `SSL_CERT_FILE` vers le certificat racine de ton entreprise |
| « approve tourne déjà (verrou pris) » | une autre instance tourne | `approve --status` puis `approve --stop` |
| Aucune réaction posée | pas d'annonce trouvée, message groupé avec une MR non approuvée, ou canal exclu | `--dry-run --once` pour voir ; vérifie `exclude_channels` |
| Une MR n'est pas approuvée | brouillon, « DO NOT MERGE », tu n'es pas approbateur autorisé, ou pas d'annonce Slack | **`approve --why <nom>`** donne la raison exacte de chaque MR |
| Ctrl-C ne semble rien faire | l'instance tourne dans un autre terminal | `approve --stop` |
| « plafond d'approbations atteint » | plus de 30 approbations en une passe | vérifie le journal, puis relance avec `--max-approvals N` |
| Passes en échec côté Slack | jeton révoqué, ou autorisation Hermes expirée | `approve --doctor` indique lequel |

Pour voir ce qui s'est passé : `approve --status`, ou ouvre `~/.mr-approver/journal.jsonl`.

---

## À propos de Hermes

Hermes est un assistant installé sur certaines machines. S'il est présent et autorisé sur Slack, l'outil peut **réutiliser son autorisation** au lieu de demander un jeton (option A). C'est entièrement facultatif : sans Hermes, utilise un jeton (option B) ou le mode GitLab seul.

`bin/approve` choisit seul le bon Python : le Python 3.11+ du système, puis bascule sur celui de Hermes uniquement si c'est la voie Slack retenue.

---

## Développement

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

L'outil n'a **aucune dépendance** hors la bibliothèque standard de Python. Les tests utilisent de faux clients GitLab et un faux serveur Slack local : aucun appel réseau externe, aucune écriture. La CI GitHub Actions les lance sur Python 3.11 et 3.14 à chaque pull request.

Les modifications de `main` passent par une pull request.
