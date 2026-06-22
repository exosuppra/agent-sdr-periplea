# Agent SDR autonome sur LinkedIn

Un agent commercial **autonome** : on lui donne un ICP en langage naturel, il
identifie et qualifie des prospects LinkedIn, mène la conversation, décroche un
rendez-vous dans un agenda et logge tout dans un CRM, **seul**, en décidant
lui-même de l'ordre de ses actions.

> Le cerveau (Claude) décide ; les outils (LinkedIn/agenda/CRM) exécutent.
> Voir `NOTE_ARCHITECTURE.md` pour les choix techniques et la conformité LinkedIn.

## Les 6 caractéristiques d'un « agent autonome » : où elles sont

1. **Part d'un ICP, pas d'un script** → `agent.py` (le LLM rédige chaque message).
2. **Planification dynamique** → boucle `Orchestrator.run` : il priorise les prospects chauds, peut chercher, relancer, reprioriser.
3. **Tool use** → 11 outils (`TOOLS` dans `agent.py`) : recherche, qualification, invitation, message, agenda, CRM, mémoire.
4. **Mémoire persistante par prospect** → `memory.py` (SQLite, machine à états + historique).
5. **Gestion d'erreur / friction** → `try/except` + dégradation, file de reprise CRM, `chaos.py` (coupure d'outil), gestion des réponses hors-script par le LLM.
6. **Critères d'arrêt explicites** → RDV pris / disqualifié / N relances sans réponse.

## Installation

```
pip install -r requirements.txt
copy .env.example .env        # puis coller la clé ANTHROPIC_API_KEY
```

Le mode par défaut est `mock` (bac à sable) : **aucun compte externe requis**, juste la clé Claude.

## Lancer la démo

**Le plus simple, le cockpit (tout depuis le navigateur, aucun terminal) :**
double-cliquer **`COCKPIT.bat`**. Le navigateur s'ouvre sur le cockpit ; on saisit
un ICP, on choisit le nombre de RDV visés, on clique **« Lancer l'agent »** et on
regarde le pipeline se remplir + la trace de décision défiler. Clic sur une carte
= toute la conversation. Les logs sont écrits dans `logs/<run>.md` et `.jsonl`.

**Variante auto (lance un parcours tout de suite) :** double-cliquer `DEMO.bat`.

**En ligne de commande :** `python dashboard.py` (cockpit) ou `python run.py --reset --target 5` (terminal seul).

## Exemple de parcours complet (livrable)

`exemple_parcours_complet.md` (et `.jsonl`) contient un **parcours prospect complet** trace de bout en bout : reception de l'ICP, identification, verification base clients, qualification, message d'approche, conversation, puis RDV decroche OU disqualification, avec le raisonnement de l'agent a chaque cycle. Prospects fictifs (mode bac a sable), donc aucune donnee personnelle reelle. Rejouable a tout moment : `python run.py --reset --target 3` (un nouveau log apparait dans `logs/`).

## Les 3 tests de la démo (tout cliquable dans le cockpit)

**1. ICP variant donné à chaud** : taper le nouvel ICP dans la case et cliquer
« Lancer l'agent » (ex. *« Responsables Career Center, écoles privées de moins de
1000 étudiants »*). En CLI : `python run.py --reset --icp "..."`.

**2. Coupure volontaire d'un outil** : pendant que l'agent tourne, cliquer
**« Couper l'agenda »** (ou le CRM / LinkedIn). L'agent ne plante pas : il logge
« outil indisponible », travaille les autres prospects et réessaie. Cliquer
**« Tout rétablir »** → il reprend et booke. (En CLI : éditer `chaos.json`,
ex. `{"calendar":"down"}`.)

**3. Prospect piège** : déjà dans le scénario : *Julien Marais* répond hors-script
(« envoyez-moi vos tarifs »). L'agent ne booke pas bêtement : il cadre une
fourchette de prix, propose 15 min, et convertit. (Autres cas couverts :
objection, réponse 3 jours plus tard, non-répondant → abandon, école d'ingénieur
publique → disqualif, école déjà cliente → disqualif.)

## Mode réel (live)

Mettre `TOOLS_MODE=live` dans `.env` et renseigner les clés Unipile / Cal.com /
HubSpot, puis :
```
python run.py --mode live --reset
```
Les adaptateurs réels sont dans `tools/unipile.py`, `tools/calcom.py`,
`tools/hubspot.py` (mêmes interfaces que les mocks).

## Options utiles

| Option | Effet |
|---|---|
| `--reset` | repart d'une base vide |
| `--target N` | nombre de RDV visés (défaut 3, comme le brief) |
| `--icp "..."` | change le profil cible |
| `--planner scripted` | cerveau de secours déterministe (teste la mécanique sans clé) |
| `--mode mock\|live` | force le mode des outils (live = Cal.com + HubSpot réels) |
| `--read-only` | mode observation : recherche + qualification, aucun contact envoyé |
| `--search-real` | recherche LinkedIn RÉELLE via Apify (conversation simulée, sans compte LinkedIn) |

Dans le cockpit, ces modes sont des cases à cocher (Mode observation, Recherche réelle, Mode réel).

Avant la démo, lire **`DEMO_JOUR_J.md`** : comment saisir un ICP variant à chaud et gérer le « grain de sable ».

## Reprise automatique après panne (cron)

Si un outil tombe (agenda, CRM, LinkedIn), l'agent dégrade proprement et **reprend** le travail quand il revient. Pour automatiser la reprise sans surveiller :

```
python cron_resume.py --loop 900     # une passe de reprise toutes les 15 min
```
ou double-cliquer **`CRON_REPRISE.bat`**. Chaque passe termine, sur la base existante, les tâches restées en plan (prospects qualifiés non contactés, RDV proposés non réservés), **sans repartir de zéro ni chercher de nouveaux prospects**, et de façon idempotente (la mémoire évite les doublons). En production : planifier `python cron_resume.py` toutes les ~15 min via le Planificateur de tâches Windows.

## Structure

```
run.py            point d'entrée (CLI)
agent.py          cerveau : 11 outils + boucle + garde-fous (anti-spam, plafonds LinkedIn)
memory.py         mémoire SQLite (états, messages, trace de décision)
trace.py          journaux lisibles (.md + .jsonl)
dashboard.py      tableau de bord web live (kanban + trace)
chaos.py          interrupteur de panne pour la démo
config.py         configuration (.env) + règles de campagne
tools/  base.py   interfaces (ports)
        mock.py   outils simulés (7 prospects scénarisés)
        unipile.py / calcom.py / hubspot.py   connecteurs réels (mode live)
```
