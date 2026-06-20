# Démo jour J : ton mode d'emploi

## D'abord, c'est quoi un « ICP » ?
ICP = juste **la description des gens à cibler**, en une phrase.
Exemple : « directeurs des admissions d'écoles de commerce privées de 500 à 5000 étudiants ».
Un « ICP variant » = l'évaluateur change cette phrase en direct (ex. « pareil mais Career Center, écoles de moins de 1000 étudiants »). Tu n'as rien à reprogrammer : tu colles leur phrase dans une case et tu lances.

## Préparer (2 min avant l'appel)
1. Double-clique **`COCKPIT.bat`**. Le navigateur s'ouvre sur le tableau de bord.
2. C'est tout. Tu pilotes tout depuis cette page.

## Le déroulé (ce que tu fais / ce que tu dis)
1. **Présente l'écran** : « À gauche, le pipeline des prospects. À droite, le raisonnement de l'agent : le pourquoi de chaque décision. »
2. **Run normal** : laisse l'ICP par défaut, objectif 3, clique **« Lancer l'agent »**. Commente pendant que ça tourne (il cherche, qualifie, écarte les non-éligibles, discute, prend des RDV).
3. **Recherche réelle** (optionnel mais impressionnant) : coche **« Recherche réelle (vrais profils via Apify) »**, relance. Montre qu'il trouve de **vraies personnes** et écarte correctement les écoles d'ingénieurs. Dis : « La recherche est réelle, sans toucher à mon compte LinkedIn ; les conversations sont simulées pour ne déranger personne. »

## TEST 1 : l'ICP variant donné à chaud
Quand ils te dictent un nouvel ICP, fais EXACTEMENT ceci :
1. Dans la case **« ICP en langage naturel »** (en haut), efface le texte.
2. Tape (ou colle) **leur phrase, telle quelle**.
3. Choisis **« RDV visés »** (2 ou 3).
4. Clique **« Lancer l'agent »**.

Dis : « Je lui donne votre ICP tel quel, en langage naturel. Il se reconfigure tout seul, sans une ligne de code modifiée. »
*(Exemple de variant pour t'entraîner : « Responsables Career Center, écoles de commerce privées de moins de 1000 étudiants, en région. »)*

## TEST 2 : le « grain de sable » (ils essaient de le déstabiliser)
Trois formes possibles. Ta réponse pour chacune :

- **Ils coupent un outil.** Pendant un run, clique **« Couper l'agenda »** (ou CRM, ou LinkedIn). Montre dans la trace de droite : « outil indisponible, je réessaierai ». L'agent continue sur d'autres prospects. Clique **« Tout rétablir »** : il reprend et réserve. Dis : « Il dégrade proprement, il ne plante pas. »
- **Un prospect hors-script** (« envoyez-moi vos tarifs » ou un profil ambigu). En bac à sable, c'est *Julien Marais* : clique sa carte pour montrer qu'il a recadré (fourchette de prix + propose 15 min) au lieu de réserver bêtement. En recherche réelle, montre qu'il écarte les profils hors-ICP.
- **Une réponse 3 jours plus tard.** C'est *Paul Lentier* : il ne l'oublie pas, le relance avec mesure, et reprend la conversation quand la réponse arrive. Dis : « Mémoire persistante par prospect. »

Règle d'or : reste calme, **raconte** ce que l'agent fait, et **pointe le “pourquoi”** affiché à droite. C'est ça qu'ils notent.

## Les boutons du cockpit (rappel)
| Élément | Effet |
|---|---|
| Case ICP + RDV visés + « Lancer l'agent » | démarre un parcours |
| « Couper l'agenda / CRM / LinkedIn » | coupe un outil (le grain de sable) |
| « Tout rétablir » | remet les outils en marche |
| « Mode observation » | cherche + qualifie, n'envoie rien |
| « Recherche réelle (Apify) » | vrais profils LinkedIn, sans risque |
| « Mode réel » | écrit dans le vrai Cal.com + HubSpot (à montrer 1 fois, objectif 1) |

## Phrases qui font mouche (questions pièges)
- **Autonomie** : « Je lui donne un objectif en langage naturel ; c'est lui qui formule chaque message et décide de l'ordre. Aucun template. »
- **Unipile vs Phantombuster** : « Unipile passe par la session de mon compte ; Phantombuster scrape depuis des serveurs, c'est ce que LinkedIn a sanctionné en 2026. Pour tenir 6 mois, pas de comparaison. »
- **Tenir 6 mois sans griller un compte** : « Plafond d'environ 100 invitations par semaine, timing aléatoire, chaque message rédigé individuellement, warm-up de 30 jours. »
- **Réponse tardive** : « Mémoire persistante : il relance avec espacement et reprend quand la personne répond. »
- **Coût par RDV** : « Quelques centimes de LLM par RDV, plus l'abonnement Unipile ; ça décroît avec le volume. »
- **C'est du sandbox ?** : « Recherche réelle via Apify, agenda et CRM branchés en vrai ; le bac à sable sert à montrer la mécanique sans déranger personne, et c'est ce que le brief demande. »

## À préciser dans le mail de réponse (et en démo)
Deux nuances à dire clairement, car ce qu'on montre est un **bac à sable de démo**, pas la prod :
- **Mémoire persistante = reprise de campagne.** En situation réelle, l'agent **conserve en mémoire chaque conversation** et **reprend une campagne là où il s'était arrêté** (il ne perd jamais le contexte, ne recontacte personne en double, relance au bon moment). C'est une des 6 caractéristiques d'autonomie demandées. **Ici, on remet la base à zéro à chaque lancement uniquement pour la démo**, afin de voir tout le travail se refaire depuis le départ.
- **Les messages partent directement dans LinkedIn.** En production, les invitations et messages sont envoyés **dans LinkedIn** (via la session du compte, Unipile), pas dans le cockpit. Le cockpit est l'outil de **pilotage et d'observation** (pipeline + raisonnement) ; le panneau de conversation sert à **tester le chatbot** sans déranger de vraies personnes pendant la démo.

## Si ça coince
- Rien ne s'affiche : vérifie que le navigateur est bien sur **http://localhost:8000** (le cockpit).
- Un run a l'air figé : c'est normal, le cerveau réfléchit quelques secondes à chaque étape ; regarde la trace se remplir.
- Pour repartir propre : relance simplement « Lancer l'agent » (chaque run repart d'une base vide).
