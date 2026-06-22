# Note d'architecture : Agent SDR autonome LinkedIn

Candidat : Quentin Duroy. Poste : Growth Hacker IA-First. Produit pitché : MeetYourSchool.

## 1. Framework

Python et l'API Claude (Anthropic) en boucle d'agent à outils (tool use) : observer l'état, décider, exécuter via un outil, mémoriser, recommencer. Une boucle simple et lisible plutôt qu'un framework lourd (LangGraph, CrewAI) ou un no-code (Lindy), car c'est cette lisibilité de la décision qui se défend en démo. Le LLM est le planificateur : à chaque cycle il reçoit tout l'état (pipeline, conversations, actions passées) et choisit une action parmi 11 outils, avec un `rationale` obligatoire (trace de décision). Aucun template : chaque message est rédigé par le LLM.

## 2. Outils (architecture ports/adaptateurs)

Le cerveau ne connaît que des interfaces ; dessous, on branche soit des outils simulés (mode `mock`, démo sans risque), soit les vrais services (mode `live`), sans toucher au cerveau.

- LinkedIn : Unipile. Agenda : Cal.com (relié à Google Agenda pour l'exercice, les RDV pris y apparaissent). CRM : HubSpot (application privée). Cerveau et mémoire : API Claude + SQLite.

Pourquoi Unipile plutôt que PhantomBuster : Unipile est une API d'infrastructure qui relaie l'action dans la session authentifiée du compte, avec un proxy dédié par compte. On évite l'IP datacenter partagée entre des centaines de comptes (le signal qui a fait bannir la page LinkedIn de HeyReach en mars 2026) et l'empreinte d'un cloud qui rejoue des scripts. Surtout, c'est une API, pas un produit clé en main : on garde la maîtrise de la logique (notre code, nos règles). En prime, Unipile est multi-canal (LinkedIn, email, WhatsApp), ce qui ouvre l'omnicanal sans réécrire le cerveau.

## 3. Mémoire

SQLite, un fichier, trois tables : `prospects` (machine à états NEW → QUALIFIED → CONTACTED → IN_CONVERSATION → MEETING_PROPOSED → MEETING_BOOKED, plus DISQUALIFIED et DEAD), `messages` (conversations) et `decisions` (la trace : observation, pourquoi, action, résultat). L'agent reconstruit son état à chaque cycle : il sait toujours où en est chaque prospect, quand relancer, et ne se répète pas, même après un arrêt puis une reprise.

## 4. Conformité LinkedIn (tenir dans la durée)

Aucun outil d'automatisation n'est conforme aux CGU de LinkedIn (art. 8.2). La posture honnête est la minimisation du risque, pas la conformité.

- Sourcing découplé du compte : via Apify, sans login ni cookie LinkedIn. En deux temps, une recherche Google (`site:linkedin.com/in`) pour identifier les profils, puis un enrichissement qui lit la fiche publique (entreprise, poste, email pro vérifié). Aucun compte exposé.
- Envoi via Unipile : seul l'envoi touche réellement le compte, et l'expose dès la 1re invitation. On réduit la fréquence, on ne supprime pas le risque.
- Cadence prudente persistée en base : de l'ordre de 100 invitations par semaine, espacées aléatoirement, warm-up 30 jours pour un compte neuf.
- Messages tous générés par le LLM : pas de template identique, principal déclencheur de bannissement.
- Dégradation propre : panne d'un outil ou du LLM gérée sans planter (file de reprise CRM, arrêt après 3 échecs, coupe-circuit anti-boucle) ; `chaos.json` coupe un outil en direct pour le démontrer.
- RGPD (B2B, intérêt légitime, opt-out) : droit d'opposition persistant (`dnc.py`), source tracée. À cadrer pour un envoi réel : mention art. 14 dans le 1er message, LIA documentée et registre.

## 5. Économie unitaire

- Apify : recherche Google quasi gratuite ; l'enrichissement (entreprise, poste, email pro vérifié) passe par le plan payant Starter (29 $/mois), pour un usage d'environ 0,01 $ par prospect couvert par les crédits inclus. Cet abonnement est mutualisable (plateforme réutilisable pour d'autres scrapers et agents IA), son coût ne s'impute pas qu'à cet agent.
- LLM (Claude) : environ 0,10 à 0,15 € par RDV (estimation). Levier : Haiku sur les étapes routinières, Sonnet sur la rédaction.
- Unipile à partir de 49 €/mois (jusqu'à 10 comptes) ; HubSpot et Cal.com gratuits.
- Coût par RDV dominé par Unipile : de l'ordre de 1 € par RDV à 50 RDV/mois, décroissant avec le volume.

## 6. Scaling

Le goulot n'est pas l'agent (le code ne change pas) mais le plafond LinkedIn par compte : de l'ordre de 100 invitations par semaine (jusqu'à environ 200 pour un compte à forte réputation ; le chiffre exact n'est pas publié), soit environ 400 par mois. Atteindre 50 RDV/mois suppose un taux invitation vers RDV de 10 à 15 %, réaliste avec un ICP serré et des messages personnalisés : hypothèse à valider en production. Si le taux est plus bas, on ajoute des comptes LinkedIn (Unipile est multi-compte, chacun avec son proxy), le scaling étant alors linéaire.
