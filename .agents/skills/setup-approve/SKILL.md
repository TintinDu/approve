---
name: setup-approve
description: Installe et configure l'outil approve (approbation automatique de merge requests GitLab et réaction Slack) une fois le dépôt cloné. À utiliser quand on demande d'installer, de configurer, de brancher Slack ou de réparer approve. Use when asked to install, set up, configure or troubleshoot approve after cloning the repository.
---

# Installer et configurer approve

Tu guides l'utilisateur de zéro jusqu'à un premier essai à blanc réussi. Les commandes sont les mêmes quelle que soit l'IA qui t'exécute : tu lances les vérifications, l'utilisateur fait lui-même tout ce qui touche à un secret ou à une décision.

## Règles à respecter pendant tout le déroulé

- **Jamais de secret dans le chat ni dans un fichier.** Ne demande ni jeton Slack, ni mot de passe, ni code de vérification. L'étape qui en a besoin (`approve --init`, `glab auth login`) est lancée par l'utilisateur dans son propre terminal.
- **Ne lance jamais la boucle réelle** (`approve` sans `--dry-run`). C'est l'utilisateur qui la démarre, une fois convaincu.
- **N'édite pas à la main** le contenu de `~/.mr-approver/`, sauf `config.json` pour un réglage demandé. N'y ajoute aucun secret.
- **N'écris aucune donnée de l'utilisateur dans le dépôt** : ni nom de serveur interne, ni identifiant, ni nom de collègue, ni canal Slack. Le dépôt est public.
- L'outil **approuve des merge requests sans relire le code**. Avant de désigner des personnes, demande à l'utilisateur de confirmer que son équipe est d'accord.

## Déroulé

Travaille depuis la racine du dépôt. À chaque étape, lis le résultat avant de passer à la suivante.

### 1. Prérequis

```bash
python3 --version      # 3.11 ou plus
glab --version
glab auth status       # doit montrer le serveur GitLab de l'utilisateur
```

- Python ou `glab` manquant : donne la commande d'installation adaptée au système (macOS : `brew install python glab` ; sinon la page du paquet) et laisse l'utilisateur l'exécuter.
- `glab` non connecté : l'utilisateur lance `glab auth login --hostname <son-serveur-gitlab>` lui-même. S'il est connecté à plusieurs serveurs, `approve` demandera `--host <serveur>`.

### 2. Installer la commande

```bash
./install.sh
approve --version
```

`install.sh` crée le lien `~/.local/bin/approve`. S'il signale que ce dossier n'est pas dans le `PATH`, donne à l'utilisateur la ligne `export` affichée, à ajouter à son fichier de shell, puis à relire après un nouveau terminal.

### 3. Créer la configuration et brancher Slack

Demande à l'utilisateur quel accès Slack il veut :

- **GitLab seul** : aucune étape Slack. Ni réaction, ni filtre « annoncées sur Slack ».
- **Hermes** : si l'assistant Hermes est installé et autorisé sur Slack, `approve --init` le détecte et réutilise son autorisation.
- **Jeton Slack à lui** : `approve --init --token`. Le jeton vient d'une application créée avec `slack-app-manifest.json` (détail dans le README, section « Brancher Slack »).

Puis dis-lui de lancer `approve --init` **dans son terminal** (dans Claude Code : `! approve --init`) et de te prévenir une fois terminé. Si une configuration existe déjà, `--force` la remplace : ne l'utilise qu'avec son accord explicite.

### 4. Diagnostiquer

```bash
approve --doctor
```

Corrige chaque ligne en échec d'après le message affiché, puis relance jusqu'à « Tout est prêt ». Pour une erreur que tu ne reconnais pas, consulte la section « Dépannage » du README avant de deviner.

### 5. Choisir les personnes

Demande les usernames GitLab des collègues à approuver. Pour chacun, le mode :

- **par défaut** : seulement les MR dont l'**auteur** a annoncé le lien sur Slack (mode prudent) ;
- **`:all`** : toutes ses MR ouvertes. Réservé aux personnes dont l'utilisateur approuve tout, et seulement s'il le confirme explicitement.

```bash
approve add alice bob:all
approve list
```

`approve add` vérifie chaque nom tout de suite : une faute de frappe est refusée sur place.

### 6. Essai à blanc

```bash
approve --dry-run --once
approve --why alice bob
```

`--dry-run` n'écrit rien. `--why` explique, pour chaque MR ouverte, pourquoi elle serait ou non approuvée. Résume le résultat à l'utilisateur et vérifie avec lui que les décisions sont celles qu'il attend.

### 7. Réglages facultatifs

Ils sont dans `~/.mr-approver/config.json` (liste complète dans le README, section « Les réglages de config.json ») : `emoji`, `exclude_channels`, `interval_seconds`, `max_approvals_per_pass`. Ne change que ce que l'utilisateur demande.

### 8. Passer la main

Dis à l'utilisateur de démarrer lui-même, quand il est prêt :

```bash
approve            # lance la boucle sur la liste mémorisée
approve --status   # état et bilan des dernières 24 h
approve --stop     # arrêt propre
approve --update   # mise à jour
```
