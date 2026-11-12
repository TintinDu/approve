# approve — instructions pour les assistants IA

Ce fichier est lu par tout assistant IA qui travaille dans ce dépôt (Codex, Claude Code, Cursor, Copilot…). Claude Code le charge via `CLAUDE.md`.

`approve` est un outil en ligne de commande, sans IA, qui approuve les merge requests GitLab de collègues choisis et pose une réaction Slack sur leur annonce. Le code est un seul module Python : `approve.py` (point d'entrée `bin/approve`).

## Installer et configurer l'outil

Pour installer `approve` et ses réglages après avoir cloné le dépôt, suis le skill `setup-approve` :
[`.agents/skills/setup-approve/SKILL.md`](.agents/skills/setup-approve/SKILL.md). Il fonctionne avec n'importe quel assistant : lis-le et suis-le étape par étape. Dans Claude Code, c'est aussi la commande `/setup-approve`.

## Règles qui valent pour tout le dépôt

- **Le dépôt est public : aucune donnée interne, jamais.** Pas de nom de société, de serveur ou de domaine interne, de nom de collègue, d'identifiant (GitLab, Slack), de nom de canal, de numéro de merge request. Cette règle vaut aussi pour les messages de commit et les descriptions de pull request. Les exemples et les tests utilisent des valeurs factices (`alice`, `gitlab.example.com`, `U0ALICE001`).
- **Aucun secret** (jeton, mot de passe) dans un fichier, un commit ou une conversation. Ne commite jamais `approve_config.json`, l'état (`state/`), ni les journaux (`*.jsonl`, `*.log`) : ils sont déjà ignorés.
- **Ne lance pas la boucle réelle** (`approve` sans `--dry-run`) : elle approuve de vraies merge requests. Pour essayer, utilise `--dry-run --once`.
- L'outil approuve **sans relire le code**. Garde ses garde-fous (plafond par passe, brouillons et « DO NOT MERGE » ignorés, règle de l'annonce par l'auteur) : ne les assouplis pas sans demande explicite.

## Développer

- Python 3.11 ou plus, **bibliothèque standard uniquement** : n'ajoute aucune dépendance d'exécution.
- Les messages affichés à l'utilisateur et le README sont en français.
- Tests : les tests utilisent de faux clients GitLab et un faux serveur Slack local, sans réseau ni écriture.

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

- Il n'y a **pas de CI hébergée** : lance toute la suite en local avant chaque pull request.
- Toute modification de `main` passe par une pull request (une review est requise).
- Ajoute ou adapte un test pour chaque changement de comportement. Si tu ajoutes une option en ligne de commande, documente-la dans le README.
