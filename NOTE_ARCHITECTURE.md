# Note d'architecture : Agent SDR autonome LinkedIn

**Candidat :** Quentin Duroy · **Poste :** Growth Hacker IA-First · **Produit pitché :** MeetYourSchool

## 1. Framework retenu

**Python + Claude (tool use) en boucle d'agent**, plutôt qu'un framework lourd (LangGraph/CrewAI) ou un no-code (Lindy/Relevance). Raison : l'autonomie demandée tient en une boucle simple et lisible (*observer l'état, décider une action, l'exécuter via un outil, mémoriser, recommencer*), et c'est précisément cette lisibilité qui se défend en démo. Le LLM est le **planificateur** : à chaque cycle il reçoit l'état complet (pipeline, conversations, ses dernières actions) et choisit **une** action parmi 11 outils, en justifiant son choix (champ `rationale` obligatoire → trace de décision). Aucun template : chaque message est rédigé par le LLM à partir du profil et de l'historique.

## 2. Outils branchés (architecture ports/adaptateurs)

Le cerveau ne connaît que des **interfaces** ; on branche dessous soit des outils **simulés** (mode `mock`, démo sans risque), soit les **vrais services** (mode `live`), sans toucher au cerveau :

| Brique | Service | Pourquoi |
|---|---|---|
| Accès LinkedIn | **Unipile** (API `X-API-KEY` + DSN) | session authentifiée de ton compte, pas de scraping cloud → défendable (cf. §4) |
| Agenda | **Cal.com** API v2 | gratuit, API propre (`/slots` puis `/bookings`) |
| CRM | **HubSpot** (app privée) | gratuit ; contacts + notes (trace) + deals (RDV) |
| Cerveau / mémoire | Claude + SQLite | raisonnement + état persistant inspectable |

## 3. Mécanisme de mémoire

**SQLite, un seul fichier** (`sdr_memory.db`), trois tables : `prospects` (machine à états : NEW → QUALIFIED → CONTACTED → IN_CONVERSATION → MEETING_PROPOSED → MEETING_BOOKED, plus DISQUALIFIED / DEAD), `messages` (toute la conversation par prospect) et `decisions` (la trace : pour chaque action, l'observation, le *pourquoi*, l'action et le résultat). À chaque cycle l'agent reconstruit son état depuis cette base → il sait toujours où en est chaque prospect, quand relancer, et ne se répète jamais (même après un arrêt/reprise du process).

## 4. Conformité LinkedIn & industrialisation (tenir 6 mois)

Aucun outil d'automatisation n'est « ToS-compliant » : l'art. 8.2 du User Agreement de LinkedIn interdit explicitement bots, scrapers et outils tiers qui automatisent un compte. La posture honnête est donc la **minimisation du risque**, pas la conformité. Nos choix :
- **Recherche découplée du compte (Apify, pas le compte LinkedIn)** : le sourcing passe par Apify (acteur `google-search-scraper`, requêtes `site:linkedin.com/in`), donc **sans login ni cookie LinkedIn**. Intérêt : la phase la plus volumineuse (le sourcing, justement celle qui fait repérer le scraping de Sales Navigator) n'expose **aucun compte LinkedIn**. Limites assumées : données de surface (nom, titre, école « à confirmer »), et le risque est *reporté* sur les ToS de Google (porté par Apify), pas éliminé.
- **Envoi via Unipile (session authentifiée), pas Phantombuster** : seul l'**envoi** touche le compte. Unipile relaie l'action *dans la session LinkedIn authentifiée du compte* (cookies/OAuth, proxy & quota gérés), pattern bien moins détectable que Phantombuster (IP datacenter, empreinte synthétique). À assumer clairement : **dès la 1re invitation, le compte est exposé** ; aucun plafond ne supprime ce risque, il le rend moins fréquent. Signal récent : en mars 2026, LinkedIn a banni la **page entreprise** de HeyReach et les profils de ses dirigeants (l'éditeur n'a pas fermé, mais la cible était bien ce type d'automatisation).
- **Cadence prudente, persistée en base** : plafond **~100 invitations/semaine** (la vraie contrainte dure) + ~20 invitations et ~40 messages par **fenêtre glissante de 24 h**, comptés **en base** (`count_outbound_since`) donc robustes à un redémarrage du process ; envois **espacés aléatoirement** (90 à 360 s) + *warm-up* 30 jours pour un compte neuf. Prochaine étape d'industrialisation : une **boucle de rétroaction sur le taux d'acceptation** (le vrai signal anti-ban) avec kill-switch, plus fiable qu'un seuil fixe.
- **Anti-similarité par construction** : le déclencheur n°1 de ban, ce sont les templates identiques. Ici **chaque message est généré individuellement par le LLM** → empreinte de similarité quasi nulle.
- **Dégrade proprement** : panne d'outil OU du cerveau LLM → l'action échoue sans planter (file de reprise CRM, arrêt propre après 3 échecs IA consécutifs, filet de sécurité par cycle) ; `chaos.json` coupe un outil en direct pour le démontrer.
- **Critères d'arrêt** : RDV pris / disqualifié / N relances sans réponse → pas de spam infini.
- **Coordonnées (email/téléphone)** : captées quand le prospect les **fournit lui-même** en conversation (l'agent demande l'email avant le RDV) → écrites dans HubSpot. En option, un **enrichisseur (Hunter.io)** devine l'email pro à partir du nom + école ; cet email est alors stocké comme **« estimé » (champ dédié + score + source tracée)**, jamais comme email confirmé, et **non utilisé pour de l'envoi de masse** sans vérification (règle RGPD : email deviné = donnée personnelle).
- **RGPD** : prospection B2B sous **intérêt légitime** (régime opt-out). FAIT : **droit d'opposition** = liste de non-sollicitation persistante (`dnc.py`, l'agent ferme et n'recontacte jamais quelqu'un qui demande l'arrêt) ; **traçabilité de la source** par prospect ; email deviné marqué « estimé ». RESTE à cadrer pour un envoi réel : la **mention d'information (art. 14)** dans le 1er message (identité, finalité, **source des données**, lien d'opposition), la LIA documentée et le registre/DPA.

## 5. Économie unitaire

- **Coût LLM** ≈ quelques centimes par RDV (le parcours complet de la démo = ~30 appels Claude Sonnet, soit ~0,5 €/run pour 4 RDV → **~0,10 à 0,15 €/RDV**). Levier d'optimisation : Haiku pour les étapes routinières (qualification), Sonnet/Opus pour la rédaction des messages.
- **Coûts fixes** : Unipile ~75 à 79 €/mois (par compte LinkedIn), HubSpot et Cal.com gratuits.

## 6. Scaling à 50 RDV/mois

Le goulot n'est **pas** l'agent (le code ne change pas) mais le **plafond LinkedIn** : ~100 invitations/semaine ≈ ~400/mois par compte. Avec un ciblage ICP serré (qui protège aussi le compte via un taux d'acceptation élevé), un seul compte couvre l'ordre de grandeur de 50 RDV/mois ; au-delà, on ajoute des comptes LinkedIn (Unipile est multi-compte, chacun avec son proxy résidentiel dédié). Le coût par RDV reste ~1,5 à 2 €, dominé par l'abonnement Unipile, et **décroît** quand le volume monte (LLM marginal, infra quasi fixe).
