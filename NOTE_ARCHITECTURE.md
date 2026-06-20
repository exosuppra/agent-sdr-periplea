# Note d'architecture : Agent SDR autonome LinkedIn

Candidat : Quentin Duroy. Poste : Growth Hacker IA-First. Produit pitché : MeetYourSchool.

## 1. Framework

Python et l'API Claude (Anthropic), en boucle d'agent à outils (tool use) : observer l'état, décider une action, l'exécuter via un outil, mémoriser, recommencer. Choix d'une boucle simple et lisible plutôt qu'un framework lourd (LangGraph, CrewAI) ou un no-code (Lindy), car c'est cette lisibilité de la décision qui se défend en démo. Le LLM est le planificateur : à chaque cycle il reçoit tout l'état (pipeline, conversations, actions passées) et choisit une action parmi 11 outils, avec un `rationale` obligatoire (trace de décision). Aucun template : chaque message est rédigé par le LLM.

## 2. Outils (architecture ports/adaptateurs)

Le cerveau ne connaît que des interfaces ; dessous, on branche soit des outils simulés (mode `mock`, démo sans risque), soit les vrais services (mode `live`), sans toucher au cerveau.

- LinkedIn : Unipile (session authentifiée du compte, pas de scraping cloud ; choix justifié juste en dessous).
- Agenda : Cal.com API v2 (gratuit), relié à Google Agenda pour l'exercice : les RDV pris apparaissent directement dans l'agenda.
- CRM : HubSpot, application privée (gratuit) : contacts, notes, deals.
- Cerveau et mémoire : API Claude (Anthropic) + SQLite.

Pourquoi Unipile plutôt que PhantomBuster ou un outil cloud du même type : Unipile est une API d'infrastructure qui relaie l'action dans la session authentifiée du compte, avec un proxy dédié par compte. On évite ainsi l'IP datacenter partagée entre des centaines de comptes (le signal exact qui a fait bannir HeyReach en mars 2026) et l'empreinte synthétique d'un PhantomBuster qui rejoue des scripts depuis son propre cloud. Surtout, c'est une API et non un produit clé en main : on garde la maîtrise de la logique de l'agent (notre code, nos règles), au lieu de dépendre d'automatisations préfabriquées. En prime, Unipile est multi-canal (LinkedIn, email, WhatsApp) avec la même interface, ce qui ouvre l'omnicanal sans réécrire le cerveau.

## 3. Mémoire

SQLite, un seul fichier, trois tables : `prospects` (machine à états NEW vers QUALIFIED vers CONTACTED vers IN_CONVERSATION vers MEETING_PROPOSED vers MEETING_BOOKED, plus DISQUALIFIED et DEAD), `messages` (conversations) et `decisions` (la trace : observation, pourquoi, action, résultat). L'agent reconstruit son état à chaque cycle : il sait toujours où en est chaque prospect, quand relancer, et ne se répète jamais, même après un arrêt puis une reprise.

## 4. Conformité LinkedIn (tenir dans la durée)

Aucun outil d'automatisation n'est conforme aux CGU de LinkedIn (art. 8.2). La posture honnête est la minimisation du risque, pas la conformité.

- Sourcing découplé du compte : via Apify, sans login ni cookie LinkedIn, donc aucun compte exposé. En deux temps : une recherche Google (`site:linkedin.com/in`) pour identifier les profils, puis un enrichissement qui lit chaque fiche publique (acteur harvestapi) pour en tirer l'entreprise, le poste et l'email professionnel vérifié (déliverabilité contrôlée). Le risque est reporté sur les CGU de Google et LinkedIn (porté par Apify), pas éliminé.
- Envoi via Unipile (choix détaillé en section 2) : seul l'envoi touche réellement le compte. Dès la 1re invitation le compte est exposé : on réduit la fréquence, on ne supprime pas le risque.
- Cadence prudente persistée en base : de l'ordre de 100 invitations par semaine, espacées aléatoirement, warm-up 30 jours pour un compte neuf. Prochaine étape : une boucle de rétroaction sur le taux d'acceptation avec kill-switch.
- Messages tous générés par le LLM : pas de template identique, principal déclencheur de bannissement.
- Dégradation propre : panne d'un outil ou du LLM gérée sans planter (file de reprise CRM, arrêt après 3 échecs consécutifs, filet de sécurité par cycle, coupe-circuit anti-boucle) ; `chaos.json` coupe un outil en direct pour le démontrer.
- RGPD (B2B, intérêt légitime, opt-out) : droit d'opposition persistant (`dnc.py`), source tracée par prospect, email deviné marqué « estimé ». À cadrer pour un envoi réel : mention d'information art. 14 dans le 1er message, LIA documentée et registre.

## 5. Économie unitaire

- Sourcing (Apify) : plan gratuit de 5 $ de crédit par mois. Recherche d'environ 0,001 à 0,002 $ par page (10 profils), puis enrichissement de profil d'environ 0,01 $ par prospect (entreprise, poste et email pro vérifié). Pour 400 prospects par mois, de l'ordre de 4 à 5 $.
- LLM (Claude) : environ 30 appels Sonnet pour le parcours de démo, soit de l'ordre de 0,10 à 0,15 € par RDV (estimation). Levier : Haiku sur les étapes routinières, Sonnet sur la rédaction.
- Outils : Unipile à partir de 49 €/mois (jusqu'à 10 comptes connectés, +5 €/compte au-delà, requêtes illimitées) ; HubSpot et Cal.com gratuits.
- Coût par RDV dominé par Unipile, de l'ordre de 1 € par RDV à 50 RDV/mois, décroissant quand le volume monte.

## 6. Scaling

Le goulot n'est pas l'agent (le code ne change pas) mais le plafond LinkedIn par compte : de l'ordre de 100 invitations par semaine pour un compte standard (jusqu'à environ 200 pour un compte à forte réputation ; LinkedIn ne publie pas le chiffre exact), soit environ 400 par mois. Atteindre 50 RDV/mois suppose un taux invitation vers RDV de l'ordre de 10 à 15 %, réaliste avec un ICP serré et des messages personnalisés : c'est l'hypothèse à valider en production, pas un chiffre mesuré. Si le taux est plus bas, on ajoute des comptes LinkedIn (Unipile est multi-compte, chacun avec son proxy), le scaling étant alors linéaire.
