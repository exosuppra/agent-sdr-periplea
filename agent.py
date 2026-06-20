"""Le cerveau de l'agent SDR autonome.

- TOOLS          : les 11 actions que l'IA peut decider d'appeler.
- LLMPlanner     : le vrai cerveau (Claude tool use) qui, a chaque cycle,
                   observe l'etat et choisit UNE action + son raisonnement.
- ScriptedPlanner: cerveau de secours deterministe (sans IA) pour tester
                   la mecanique hors-ligne.
- Orchestrator   : la boucle "observer -> decider -> agir -> memoriser",
                   les garde-fous (anti-spam, plafonds LinkedIn, criteres
                   d'arret) et la gestion de panne d'outil.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import dnc
import memory as M
from tools.base import ToolUnavailable, RateLimited, ToolError
from tools.enrich import EmailFinder

# Detection des coordonnees PARTAGEES par le prospect lui-meme (consenties)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+33|0)\s?[1-9](?:[\s.\-]?\d{2}){4}")
# Score Hunter minimal pour retenir un email ESTIME (sous ce seuil = trop incertain)
MIN_ENRICH_SCORE = 70


def _clean_text(s):
    """Nettoie le texte genere par le LLM pour la messagerie : retire les tirets
    cadratin (proscrits par le user) ET le markdown gras (**, __) qui s'afficherait
    tel quel sur LinkedIn (qui ne rend pas le markdown)."""
    if not isinstance(s, str):
        return s
    return s.replace("—", "-").replace("–", "-").replace("**", "").replace("__", "")


# Marqueurs indiquant que l'agent a DEJA acte l'arret du contact -> conversation close.
_CLOSURE_MARKERS = ("recontacterai plus", "recontacterons plus", "ne plus vous contacter",
                    "ne vous solliciterai plus", "je vous retire", "vous retire de mes",
                    "je disparais", "ne vous recontacte plus", "ne vous ecrirai plus")
CONV_CLOSED = "__CONV_CLOSED__"  # sentinelle : l'agent ne repond plus


def _agent_has_closed(history) -> bool:
    return any(any(m in (h.get("text", "") or "").lower() for m in _CLOSURE_MARKERS)
               for h in (history or []) if h.get("role") == "agent")


# ====================================================================
#  Les outils exposes au planificateur (format Claude tool use)
# ====================================================================
TOOLS = [
    {
        "name": "search_prospects",
        "description": "Cherche de NOUVEAUX prospects sur LinkedIn correspondant a l'ICP. "
                       "A utiliser quand le pipeline manque de prospects a traiter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "object",
                    "properties": {
                        "role_keywords": {"type": "array", "items": {"type": "string"},
                                          "description": "mots-cles de poste, ex. ['admissions','career','promotion']"},
                        "min_students": {"type": "integer"},
                        "max_students": {"type": "integer"},
                        "focus": {"type": "string", "description": "axe particulier de l'ICP, ex. 'career center'"},
                    },
                },
                "limit": {"type": "integer"},
            },
            "required": ["criteria"],
        },
    },
    {
        "name": "get_profile",
        "description": "Recupere le profil detaille d'un prospect (poste exact, ecole, taille, region) pour le qualifier.",
        "input_schema": {"type": "object",
                         "properties": {"prospect_id": {"type": "string"}},
                         "required": ["prospect_id"]},
    },
    {
        "name": "check_client_base",
        "description": "Verifie dans le CRM si l'ecole du prospect est DEJA cliente. A faire AVANT de qualifier.",
        "input_schema": {"type": "object",
                         "properties": {"prospect_id": {"type": "string"}},
                         "required": ["prospect_id"]},
    },
    {
        "name": "record_qualification",
        "description": "Enregistre la decision de qualification d'un prospect. "
                       "eligible=false => le prospect est ecarte proprement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string"},
                "eligible": {"type": "boolean"},
                "reasons": {"type": "string", "description": "justification (ecole eligible ? seniorite ? deja client ?)"},
                "buying_signals": {"type": "string"},
            },
            "required": ["prospect_id", "eligible", "reasons"],
        },
    },
    {
        "name": "send_invitation",
        "description": "Envoie une invitation LinkedIn avec une note PERSONNALISEE (<=300 caracteres). "
                       "Uniquement a un prospect QUALIFIE. Pas de template generique.",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"}, "note": {"type": "string"}},
            "required": ["prospect_id", "note"],
        },
    },
    {
        "name": "send_message",
        "description": "Envoie un message dans la conversation (reponse a une objection, relance, etc.). "
                       "Doit etre personnalise et tenir compte de tout l'historique.",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["prospect_id", "text"],
        },
    },
    {
        "name": "propose_meeting",
        "description": "Propose un RDV : ecris le message d'accroche, les creneaux disponibles seront ajoutes "
                       "automatiquement. A faire quand un signal d'achat est detecte.",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["prospect_id", "message"],
        },
    },
    {
        "name": "book_meeting",
        "description": "Reserve le creneau choisi par le prospect dans l'agenda et logue le RDV au CRM.",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"},
                           "slot_id": {"type": "string", "description": "id du creneau parmi ceux proposes"}},
            "required": ["prospect_id", "slot_id"],
        },
    },
    {
        "name": "disqualify",
        "description": "Ecarte proprement un prospect non pertinent (ecole non eligible, deja client, mauvais poste).",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["prospect_id", "reason"],
        },
    },
    {
        "name": "reschedule_meeting",
        "description": "Deplace le RDV DEJA pris d'un prospect vers un nouveau creneau (l'ancien est annule "
                       "automatiquement, pas de doublon). A utiliser quand un prospect dont le RDV est deja "
                       "reserve signale un empechement. Propose-lui d'abord de nouveaux creneaux "
                       "(propose_meeting) puis appelle reschedule_meeting avec le creneau choisi.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string"},
                "slot_id": {"type": "string", "description": "id du nouveau creneau choisi (parmi les creneaux proposes)"},
            },
            "required": ["prospect_id", "slot_id"],
        },
    },
    {
        "name": "register_referral",
        "description": "Quand un prospect repond qu'il n'est PAS le bon interlocuteur et te renvoie vers "
                       "quelqu'un d'autre, enregistre ce contact recommande. Il est AJOUTE au pipeline "
                       "(et recherche sur LinkedIn par nom + ecole en mode reel), puis qualifie et "
                       "contacte normalement. Le prospect qui t'a redirige est clos proprement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_prospect_id": {"type": "string", "description": "id du prospect qui t'a redirige"},
                "full_name": {"type": "string", "description": "nom complet de la personne recommandee"},
                "role": {"type": "string", "description": "poste de la personne recommandee, si donne"},
                "company": {"type": "string", "description": "ecole/entreprise si differente du referent (sinon laisse vide)"},
            },
            "required": ["from_prospect_id", "full_name"],
        },
    },
    {
        "name": "opt_out",
        "description": "A appeler quand un prospect demande a ne plus etre contacte (opposition RGPD : "
                       "'ne me contactez plus', 'retirez-moi', 'stop'...). Il est clos ET ajoute a la liste "
                       "de non-sollicitation PERSISTANTE : il ne sera jamais recontacte, meme dans une future "
                       "campagne. Ne lui envoie plus aucun message.",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["prospect_id"],
        },
    },
    {
        "name": "mark_no_response",
        "description": "Clot un prospect apres N relances sans reponse (critere d'arret, pas de spam infini).",
        "input_schema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["prospect_id", "reason"],
        },
    },
    {
        "name": "finish",
        "description": "A appeler uniquement quand il n'y a plus aucune action utile maintenant "
                       "(tous les prospects sont soit termines, soit en attente d'une reponse).",
        "input_schema": {"type": "object",
                         "properties": {"reason": {"type": "string"}},
                         "required": ["reason"]},
    },
]

# Chaque action doit etre justifiee : on injecte un champ "rationale" obligatoire
# dans tous les outils -> la trace de decision capture toujours le "pourquoi".
for _t in TOOLS:
    _t["input_schema"]["properties"]["rationale"] = {
        "type": "string",
        "description": "Explique en 1-2 phrases pourquoi tu choisis CETTE action maintenant.",
    }
    _t["input_schema"]["required"] = ["rationale"] + _t["input_schema"].get("required", [])


SYSTEM_TEMPLATE = """Tu es un agent commercial (SDR) AUTONOME pour Periplea, qui vend MeetYourSchool :
un SaaS 9-en-1 pour les ecoles superieures privees francaises (admissions, scolarite,
career center, relation alumni, etc.). Tu prospectes sur LinkedIn de A a Z, seul.

ICP (profil cible) recu en langage naturel :
\"{icp}\"

OBJECTIF : decrocher {target} rendez-vous qualifies de 30 min, sous {window} jours.

A CHAQUE TOUR, tu observes l'etat courant et tu choisis UNE SEULE action (un appel d'outil).
Avant l'appel d'outil, explique en 1-2 phrases POURQUOI tu choisis cette action (ce texte
sert de trace de decision lisible). Puis appelle exactement un outil.

REGLES IMPERATIVES :
- Tu pars de l'ICP, pas d'un script fige. Tu rediges chaque message toi-meme, personnalise
  selon le poste, l'ecole et l'historique de conversation. Jamais de template generique.
- PERSONNALISATION (naturel et humain, anti-generique) : le PREMIER message (invitation) doit etre
  CHALEUREUX et donner l'impression d'une vraie personne, pas d'un argumentaire. STRUCTURE : (1) une
  ACCROCHE HUMAINE qui explique comment/pourquoi tu arrives vers cette personne (par ex. "en cherchant
  des responsables admissions en ecoles de commerce, votre profil a retenu mon attention") ; (2)
  ENSUITE seulement, le lien avec ce que MeetYourSchool apporte pour CE role, amene avec TACT (une
  hypothese, pas une affirmation peremptoire de son quotidien) et ancre sur un fait CONCRET et
  verifiable de son profil (intitule exact, ecole, region, detail du "summary") : admissions = suivi
  et relance des candidats, conversion ; career center = placement, relation entreprises, alumni ;
  promotion = remplissage des promos. N'attaque PAS directement par le pitch. Bannis les formules
  vagues ("votre role m'a semble pertinent"). N'invente JAMAIS un fait (chiffre, recompense, actualite)
  que tu n'as pas ; si l'info manque, reste sur l'enjeu metier concret, jamais sur un compliment.
- Qualifie AVANT tout contact : verifie l'eligibilite de l'ecole (privee, 500-5000 etudiants,
  hors ecoles d'ingenieur publiques), la seniorite du poste, ET appelle check_client_base
  pour ecarter une ecole deja cliente.
- Mene la conversation sur 2 a 5 echanges, en t'adaptant aux reponses (objections, questions).
- Detecte le bon moment (signal d'achat) pour proposer un RDV, puis reserve-le.
- Priorise : un prospect chaud (qui vient de repondre) passe avant un nouveau a contacter.
- Ne te repete jamais. Ne spamme pas : respecte le delai entre relances.
- Criteres d'arret par prospect : RDV pris, disqualifie, ou {max_follow_ups} relances sans
  reponse (alors mark_no_response).
- Si un outil est indique INDISPONIBLE, n'insiste pas dessus : travaille sur d'autres
  prospects et il sera reessaye plus tard.
- Ton professionnel, vouvoiement, concis, credible aupres d'un directeur d'ecole. En francais.
- ADRESSE-TOI au prospect par sa civilite et son NOM DE FAMILLE : "Bonjour Madame Seguin", "Bonjour Monsieur Dupont". JAMAIS le prenom seul. Deduis Madame ou Monsieur du prenom ; en cas de doute reel sur le genre, ecris simplement "Bonjour" suivi du prenom et du nom, sans civilite.
- N'utilise JAMAIS le tiret cadratin (—) ni le tiret demi-cadratin (–) : ecris avec des virgules, des deux-points, des parentheses ou des phrases courtes.
- Tes MESSAGES aux prospects sont en TEXTE SIMPLE : aucune mise en forme markdown (pas de gras avec des asterisques, pas de listes a puces) car LinkedIn ne rend pas le markdown.

DEROULE TYPE PAR PROSPECT (tu restes maitre de l'ordre et des priorites) :
- NEW : verifie la base clients (check_client_base) puis qualifie (record_qualification).
  Les resultats de recherche contiennent DEJA le type et la taille de l'ecole :
  n'appelle get_profile QUE s'il manque vraiment une info.
- QUALIFIED : envoie une invitation personnalisee (send_invitation).
- le prospect a repondu : reponds / traite l'objection (send_message), puis propose un
  RDV (propose_meeting) des qu'un signal d'achat apparait.
- des que le prospect accepte un echange ou demande un horaire, NE repose PAS de question
  de decouverte : propose des creneaux ou confirme. Avance toujours vers le RDV, sans revenir
  en arriere.
- COORDONNEES : quand le prospect est interesse et avant de reserver, si tu n'as pas son email,
  demande-lui son email professionnel pour lui envoyer l'invitation (et son telephone s'il prefere).
  Ces coordonnees, qu'il partage de lui-meme, sont enregistrees automatiquement dans le CRM.
- RDV propose et creneau choisi : book_meeting (reserve le creneau choisi, sans nouvelle question).
- DEPLACEMENT : si un prospect dont le RDV est DEJA pris (etat RDV pris) signale un empechement,
  ne reserve pas un 2e RDV. Propose-lui de nouveaux creneaux (propose_meeting), puis deplace
  l'ancien avec reschedule_meeting (le creneau initial est annule automatiquement).
- REDIRECTION : si un prospect repond qu'il n'est pas le bon interlocuteur et te renvoie vers
  quelqu'un d'autre, appelle register_referral (nom de la personne + son poste/ecole si donnes).
  Cette personne sera ajoutee au pipeline et le referent sera clos. Ne tente pas de la chercher
  toi-meme via search_prospects.
- OPPOSITION (RGPD) : si un prospect demande a ne plus etre contacte ("ne me contactez plus",
  "retirez-moi", "stop", "desinscription"), appelle opt_out IMMEDIATEMENT. Ne relance pas, ne
  cherche pas a le convaincre : tu respectes son opposition et il est clos definitivement.
- Quand tu contactes un prospect dont les infos indiquent "source: Recommande par ...", mentionne
  la recommandation dans ton message d'approche (par ex. "X m'a suggere de vous contacter").
Ne repete JAMAIS deux fois de suite la meme action sur le meme prospect : regarde
"TES DERNIERES ACTIONS" et fais avancer le prospect a l'etape suivante.

Tu agis tour par tour. Choisis la meilleure action unique maintenant."""


# ====================================================================
#  Planificateur LLM (le vrai cerveau)
# ====================================================================
class LLMPlanner:
    def __init__(self, settings):
        import anthropic  # importe ici pour ne pas exiger le paquet en mode scripte
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY manquante (voir .env).")
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.model

    def decide(self, system: str, snapshot: dict) -> Optional[tuple[str, str, dict]]:
        user_text = render_snapshot(snapshot)
        last_err = None
        for attempt in range(2):
            try:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=1300,
                    temperature=0.4,
                    system=system,
                    tools=TOOLS,
                    tool_choice={"type": "any"},
                    messages=[{"role": "user", "content": user_text}],
                )
                text_rationale = " ".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                ).strip()
                tool_use = next(
                    (b for b in resp.content if getattr(b, "type", "") == "tool_use"), None
                )
                if not tool_use:
                    return None
                args = dict(tool_use.input)
                rationale = (args.pop("rationale", "") or "").strip() or text_rationale

                rationale = _clean_text(rationale)
                args = {k: _clean_text(v) for k, v in args.items()}
                return (rationale or "(pas d'explication)", tool_use.name, args)
            except Exception as e:  # transitoire : on retente une fois
                last_err = e
                time.sleep(1.5)
        raise RuntimeError(f"Erreur API Anthropic : {last_err}")


# ====================================================================
#  Planificateur scripte (secours sans IA, pour tester la mecanique)
# ====================================================================
class ScriptedPlanner:
    """Politique deterministe minimale. NE remplace PAS l'IA en production :
    sert seulement a valider la boucle sans cle API."""

    def decide(self, system: str, snapshot: dict) -> Optional[tuple[str, str, dict]]:
        actionable = snapshot["actionable"]
        if not actionable:
            if snapshot["can_search"]:
                return ("Pipeline vide, je cherche de nouveaux prospects.",
                        "search_prospects",
                        {"criteria": {"role_keywords": ["admission", "career", "promotion", "recrutement"],
                                      "min_students": 500, "max_students": 5000}, "limit": 10})
            return ("Plus rien a faire.", "finish", {"reason": "pipeline epuise"})

        p = actionable[0]
        attrs = p["attributes"]
        state = p["state"]
        pid = p["id"]

        if state == M.NEW:
            if "client_check" not in attrs:
                return (f"Avant de qualifier {p['full_name']}, je verifie la base clients.",
                        "check_client_base", {"prospect_id": pid})
            is_client = attrs.get("client_check", {}).get("is_client", False)
            sc = attrs.get("student_count", 0)
            stype = attrs.get("school_type", "")
            eligible = (stype == "privee") and (500 <= sc <= 5000) and not is_client
            if is_client:
                reasons = "ecole deja cliente"
            elif stype != "privee":
                reasons = f"type d'ecole non eligible ({stype})"
            elif not (500 <= sc <= 5000):
                reasons = f"taille hors cible ({sc} etudiants)"
            else:
                reasons = "ecole privee dans la cible, poste pertinent"
            return (f"Qualification de {p['full_name']} : {reasons}.",
                    "record_qualification",
                    {"prospect_id": pid, "eligible": eligible, "reasons": reasons})

        if state == M.QUALIFIED:
            note = (f"Bonjour {p['full_name'].split()[0]}, je travaille avec des ecoles comme la votre "
                    f"sur la centralisation de leurs outils (admissions, career center...). "
                    f"Curieux d'echanger sur {p['company']} ?")
            return (f"{p['full_name']} est qualifie, j'envoie une invitation personnalisee.",
                    "send_invitation", {"prospect_id": pid, "note": note[:300]})

        if state in (M.CONTACTED, M.IN_CONVERSATION, M.MEETING_PROPOSED):
            if p["awaiting"] and p["due"]:
                return (f"Pas de reponse de {p['full_name']}, je relance avec un angle different.",
                        "send_message",
                        {"prospect_id": pid, "text": "Petit rappel, votre avis m'interesse. Un echange de 30 min ?"})
            conv = p["conversation"]
            last_in = next((m["text"].lower() for m in reversed(conv) if m["direction"] == "in"), "")
            picks = ["creneau", "convient", "me va", "parfait", "ca ira", "mardi", "jeudi",
                     "lundi", "mercredi", "vendredi", "11h", "14h", "10h", "16h"]
            if p["offered_slots"] and any(k in last_in for k in picks):
                slot_id = _match_slot(last_in, p["offered_slots"])
                return (f"{p['full_name']} a choisi un creneau, je reserve.",
                        "book_meeting", {"prospect_id": pid, "slot_id": slot_id})
            n_out = sum(1 for m in conv if m["direction"] == "out")
            signal = any(k in last_in for k in ["interess", "echange", "call", "d'accord",
                                                "ok", "30 min", "quand", "minutes", "preneur", "volontiers"])
            if state != M.MEETING_PROPOSED and (signal or n_out >= 2):
                return (f"Signal d'achat detecte chez {p['full_name']}, je propose un RDV.",
                        "propose_meeting",
                        {"prospect_id": pid, "message": "Le plus simple est d'en parler 30 min. Voici des creneaux :"})
            return (f"Je reponds a {p['full_name']} en tenant compte de son dernier message.",
                    "send_message",
                    {"prospect_id": pid,
                     "text": "Tres bonne question. MeetYourSchool centralise admissions, scolarite et "
                             "career center en un seul outil, pense pour les ecoles privees. "
                             "Qu'est-ce qui vous prend le plus de temps aujourd'hui ?"})

        return ("Rien de pertinent.", "finish", {"reason": "aucune action"})


def _match_slot(last_in: str, slots: list[dict]) -> str:
    if not slots:
        return "slot1"
    if "dernier" in last_in:
        return slots[-1]["id"]
    ords = [("premier", 0), ("1er", 0), ("second", 1), ("deuxieme", 1), ("2eme", 1),
            ("troisieme", 2), ("3eme", 2), ("quatrieme", 3), ("cinquieme", 4)]
    for kw, i in ords:
        if kw in last_in and i < len(slots):
            return slots[i]["id"]
    for s in slots:
        label = s["label"].lower()
        day = label.split()[0]
        if day in last_in or s["label"].split()[-1] in last_in:
            return s["id"]
    return slots[0]["id"]


# ====================================================================
#  Rendu de l'etat pour le LLM
# ====================================================================
def render_snapshot(s: dict) -> str:
    lines = []
    lines.append(f"OBJECTIF : {s['booked']}/{s['target']} RDV pris pour l'instant.")
    c = s["counts"]
    lines.append("Pipeline : " + ", ".join(f"{k}={v}" for k, v in c.items()) if c else "Pipeline vide.")
    if s["tools_status"]:
        downs = [t for t, st in s["tools_status"].items() if st == "down"]
        if downs:
            lines.append("OUTILS INDISPONIBLES : " + ", ".join(downs) + " (a eviter pour l'instant).")
    if s["can_search"]:
        lines.append("Tu peux appeler search_prospects pour trouver de nouveaux prospects.")
    if s.get("recent_actions"):
        lines.append("")
        lines.append("TES DERNIERES ACTIONS (ne les repete pas inutilement) :")
        for a in s["recent_actions"]:
            lines.append(f"  - cycle {a['cycle']} : {a['action']} [{a['target']}] -> {a['result']}")
    lines.append("")
    if not s["actionable"]:
        lines.append("Aucun prospect n'attend de decision immediate.")
    else:
        lines.append(f"PROSPECTS A TRAITER MAINTENANT ({len(s['actionable'])}) :")
        for p in s["actionable"]:
            lines.append(f"\n- [{p['id']}] {p['full_name']} : {p['headline']} @ {p['company']}")
            lines.append(f"  etat={p['state']} | attente_reponse={p['awaiting']} | relances={p['follow_up_count']}")
            lines.append(f"  infos: {json.dumps(p['attributes'], ensure_ascii=False)}")
            if p["qualification"]:
                lines.append(f"  qualification: {json.dumps(p['qualification'], ensure_ascii=False)}")
            if p["offered_slots"]:
                slot_str = "; ".join(f"{x['id']}={x['label']}" for x in p["offered_slots"])
                lines.append(f"  creneaux proposes: {slot_str}")
            if p["conversation"]:
                lines.append("  conversation:")
                for m in p["conversation"][-6:]:
                    who = "NOUS" if m["direction"] == "out" else "PROSPECT"
                    lines.append(f"    {who}: {m['text']}")
    lines.append("\nChoisis la meilleure action unique maintenant (un seul appel d'outil).")
    return "\n".join(lines)


# ====================================================================
#  Orchestrateur : la boucle de l'agent
# ====================================================================
class Orchestrator:
    def __init__(self, settings, campaign, mem, linkedin, calendar, crm, trace, planner,
                 read_only=False, resume_only=False):
        self.s = settings
        self.cfg = campaign
        self.read_only = read_only  # mode observation : recherche + qualification, AUCUN envoi
        self.resume_only = resume_only  # mode REPRISE : finir l'existant, ne PAS chercher de nouveaux prospects
        self.mem = mem
        self.li = linkedin
        self.cal = calendar
        self.crm = crm
        self.trace = trace
        self.planner = planner
        self.tick = 0
        self.no_more_prospects = False
        self.tool_status: dict[str, str] = {}
        self.pending_crm: list[tuple[str, callable]] = []
        # Les plafonds journaliers sont comptes EN BASE (fenetre glissante 24h),
        # pas en memoire : ils survivent ainsi a un redemarrage du process.
        self.profiled: set[str] = set()        # anti-boucle get_profile
        self.client_checked: set[str] = set()  # anti-boucle check_client_base
        self.finder = EmailFinder(settings)    # enrichissement email optionnel (no-op sans cle)
        # Coupe-circuit anti-boucle : si la MEME action echoue en boucle (ex. un id
        # introuvable que le LLM repete), on met le prospect de cote, et si plus rien
        # n'avance du tout on s'arrete proprement au lieu de tourner sans fin.
        self._fail_sig: str | None = None
        self._fail_count = 0
        self._stuck = 0

    # ---- horloge (mock = ticks ; live = temps reel) ----
    def _now(self) -> float:
        return self.tick if self.s.tools_mode == "mock" else time.time()

    def _gap(self) -> float:
        if self.s.tools_mode == "mock":
            return self.cfg.mock_follow_up_gap_cycles
        return self.cfg.follow_up_gap_hours * 3600

    def _is_due(self, p: dict) -> bool:
        return p["next_action_at"] is None or p["next_action_at"] <= self._now()

    def _sent_recent(self, channel: str, hours: float) -> int:
        """Nombre d'envois reels (en base) sur les `hours` dernieres heures.
        Sert aux plafonds : DB-backed => robuste aux redemarrages du process."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.mem.count_outbound_since(channel, since)

    @staticmethod
    def _identity(p: dict) -> str:
        """Identite STABLE d'un prospect pour la liste de non-sollicitation
        (URL LinkedIn si dispo, sinon nom) : survit a un changement d'id interne."""
        attrs = p.get("attributes") or {}
        url = (p.get("profile_url") or attrs.get("profile_url") or "").strip()
        return url or (p.get("full_name") or "")

    def _enrich_email(self, p) -> None:
        """Devine l'email pro (Hunter) quand on n'en a aucun, et le stocke comme ESTIME
        (jamais comme email confirme). No-op si pas de cle Hunter. Best-effort."""
        if not self.finder.enabled:
            return
        attrs = p.get("attributes") or {}
        if attrs.get("email") or attrs.get("email_estime") or not p.get("company"):
            return
        parts = (p.get("full_name") or "").split(" ", 1)
        try:
            res = self.finder.find(parts[0], parts[1] if len(parts) > 1 else "", p.get("company", ""))
        except Exception:
            return
        email, score = res.get("email"), res.get("score", 0)
        if email and score >= MIN_ENRICH_SCORE:
            new_attrs = dict(attrs)
            new_attrs["email_estime"] = email
            new_attrs["email_estime_score"] = score
            p["attributes"] = new_attrs  # pour l'upsert HubSpot qui suit
            self.mem.update_prospect(p["id"], attributes=new_attrs)
            self.trace.event("EMAIL ESTIME",
                             f"{p['full_name']} : {email} (score {score}/100, via Hunter, a CONFIRMER avant tout envoi).")

    # ---- boucle principale ----
    def run(self, icp: str) -> None:
        self.mem.set_meta("icp", icp)
        self.trace.event("DEMARRAGE", f"ICP recu : {icp}")
        system = SYSTEM_TEMPLATE.format(
            icp=icp, target=self.cfg.meeting_target,
            window=self.cfg.window_days, max_follow_ups=self.cfg.max_follow_ups,
        )
        if self.read_only:
            system += ("\n\nMODE OBSERVATION ACTIF : tu ne dois QUE chercher (search_prospects) et "
                       "qualifier (check_client_base, record_qualification). N'envoie AUCUNE invitation "
                       "ni message, ne propose et ne reserve AUCUN RDV. Quand tous les prospects trouves "
                       "sont qualifies ou disqualifies, appelle finish.")
            self.trace.event("MODE OBSERVATION", "recherche + qualification uniquement, aucun contact ne sera envoye.")

        from tools.piloted import pilot_active, pilot_profile, PILOT_ID
        if pilot_active():
            pp = pilot_profile()
            self.mem.upsert_prospect({"id": PILOT_ID, "full_name": pp.get("full_name", "Vous (test)"),
                                      "headline": pp.get("headline", "Prospect pilote"),
                                      "company": pp.get("company", ""), "profile_url": "",
                                      "attributes": {"piloted": True, "source": "prospect pilote (humain en direct)"}})
            self.mem.update_prospect(PILOT_ID, state=M.QUALIFIED,
                                     qualification={"eligible": True, "reasons": "prospect pilote (test en direct)"})
            self.trace.event("PROSPECT PILOTE", "vous etes inclus comme prospect ; repondez dans le panneau du cockpit.")

        consec_brain_fail = 0
        for cycle in range(1, self.cfg.max_cycles + 1):
            self.tick = cycle
            if hasattr(self.li, "set_tick"):
                self.li.set_tick(self.tick)

            try:
                self._retry_pending_crm()
                self._ingest_replies(cycle)
                self._apply_stop_rules(cycle)

                booked = self.mem.counts_by_state().get(M.MEETING_BOOKED, 0)
                if booked >= self.cfg.meeting_target and not self._pilot_open():
                    self.trace.event("OBJECTIF ATTEINT", f"{booked} RDV pris.")
                    break

                snapshot = self._build_snapshot(icp)
                if not snapshot["actionable"]:
                    # On REMPLIT d'abord le pipeline avec de vrais prospects (recherche)
                    # AVANT de bloquer sur le prospect pilote : sinon l'agent n'attendrait
                    # que vous et ne chercherait jamais. On laisse donc le LLM chercher.
                    if snapshot["can_search"]:
                        pass  # -> tombe dans planner.decide, qui va appeler search_prospects
                    elif self._pilot_inflight():
                        self.trace.event("ATTENTE", "en attente de votre reponse (prospect pilote)...")
                        if not self._wait_pilot():
                            self.trace.event("FIN", "pas de reponse du prospect pilote, on arrete.")
                            break
                        continue  # vous avez repondu : on retraite au prochain cycle
                    elif snapshot["inflight"]:
                        continue  # on laisse le temps avancer pour recevoir les reponses
                    else:
                        self.trace.event("FIN", "plus aucune action a entreprendre.")
                        break

                # Le CERVEAU lui-meme peut tomber (panne API, rate limit IA) : on degrade,
                # on ne plante pas. Apres 3 echecs consecutifs, on s'arrete proprement.
                try:
                    decision = self.planner.decide(system, snapshot)
                    consec_brain_fail = 0
                except Exception as e:
                    consec_brain_fail += 1
                    self.trace.event("CERVEAU INDISPONIBLE",
                                     f"erreur IA gere ({str(e)[:120]}) ; tentative {consec_brain_fail}/3.")
                    if consec_brain_fail >= 3:
                        self.trace.event("FIN", "cerveau IA indisponible de facon persistante, arret propre.")
                        break
                    if self.s.tools_mode != "mock":
                        time.sleep(5)  # petit backoff en live avant de reessayer
                    continue

                if not decision:
                    self.trace.event("FIN", "le planificateur n'a propose aucune action.")
                    break
                rationale, action, args = decision

                pid = args.get("prospect_id")
                p_before = self.mem.get_prospect(pid) if pid else None
                observation = self._observe(p_before, snapshot)

                if action == "finish":
                    self.trace.decision(cycle, None, rationale, action, args, "fin de la boucle")
                    self.mem.log_decision(cycle, None, observation, rationale, action, args, "fin")
                    self.trace.event("FIN", args.get("reason", ""))
                    break

                result = self._dispatch(action, args, cycle)
                pid = args.get("prospect_id")  # _dispatch a pu canoniser l'id
                p_after = self.mem.get_prospect(pid) if pid else None
                self.trace.decision(cycle, p_after or p_before, rationale, action, args, result)
                self.mem.log_decision(cycle, pid, observation, rationale, action, args, result)

                # COUPE-CIRCUIT : empeche toute boucle infinie sur une action qui echoue.
                failed = isinstance(result, str) and result.startswith("ECHEC")
                if not failed:
                    self._fail_sig = None
                    self._fail_count = 0
                    self._stuck = 0
                else:
                    self._stuck += 1
                    sig = f"{action}:{pid}"
                    self._fail_count = self._fail_count + 1 if sig == self._fail_sig else 1
                    self._fail_sig = sig
                    # 3 fois la MEME action en echec sur le MEME prospect : on le met de cote
                    # (avec note) pour ne pas bloquer la campagne, puis on repart.
                    if self._fail_count >= 3 and p_after:
                        self.mem.update_prospect(p_after["id"], state=M.DISQUALIFIED, awaiting_reply=0)
                        self._crm_safe(f"note blocage {p_after['id']}",
                                       lambda pid=p_after["id"], a=action: self.crm.log_note(
                                           pid, f"Mis de cote : l'etape '{a}' a echoue 3 fois (incident technique)."))
                        self.trace.event("INCIDENT REPETE",
                                         f"{p_after['full_name']} : '{action}' echoue 3 fois, prospect mis de cote pour ne pas bloquer.")
                        self._fail_sig = None
                        self._fail_count = 0
                    # blocage global : 6 cycles d'affilee sans la moindre action reussie.
                    if self._stuck >= 6:
                        self.trace.event("FIN", "blocage persistant detecte (aucune action ne reussit), arret propre.")
                        break
            except Exception as e:
                # FILET DE SECURITE : un incident inattendu dans un cycle ne doit JAMAIS
                # faire planter l'agent. On le journalise et on passe au cycle suivant.
                try:
                    self.trace.event("INCIDENT GERE", f"erreur inattendue dans le cycle {cycle} : {str(e)[:150]}")
                except Exception:
                    pass
                continue

        self._summary()

    # ---- reception des reponses ----
    def _ingest_replies(self, cycle: int) -> None:
        try:
            replies = self.li.fetch_new_replies()
        except ToolUnavailable:
            self.tool_status["linkedin"] = "down"
            return
        except Exception:
            return  # reception des reponses non bloquante : on ne plante jamais dessus
        self.tool_status["linkedin"] = "ok"
        for r in replies:
            pid = r["prospect_id"]
            p = self.mem.get_prospect(pid)
            if not p or p["state"] in (M.DEAD, M.DISQUALIFIED):
                continue
            self.mem.add_message(pid, "in", "message", r["text"])
            # OPPOSITION RGPD : detection deterministe (filet de securite si le LLM passe a cote)
            # -> on clot et on ajoute a la liste de non-sollicitation, sans rouvrir le prospect.
            if dnc.detect_optout(r["text"]):
                dnc.block(self._identity(p), p.get("full_name", ""), "opposition detectee dans la reponse")
                self.mem.update_prospect(pid, state=M.DEAD, awaiting_reply=0)
                self._crm_safe(f"optout {pid}",
                               lambda pid=pid: self.crm.log_note(pid, "OPPOSITION (RGPD) detectee : non-sollicitation, ne plus contacter."))
                self.trace.event("OPPOSITION RGPD", f"{p['full_name']} : opposition detectee, clos + liste de non-sollicitation.")
                continue
            # COORDONNEES partagees par le prospect (email / telephone) -> capture + CRM
            em = _EMAIL_RE.search(r["text"])
            ph = _PHONE_RE.search(r["text"])
            if em or ph:
                attrs = dict(p.get("attributes") or {})
                if em and not attrs.get("email"):
                    attrs["email"] = em.group(0)
                    self._crm_safe(f"email {pid}", lambda pid=pid, e=em.group(0): self.crm.set_email(pid, e))
                if ph and not attrs.get("phone"):
                    attrs["phone"] = re.sub(r"[\s.\-]", "", ph.group(0))
                    self._crm_safe(f"phone {pid}", lambda pid=pid, t=attrs.get("phone"): self.crm.set_phone(pid, t))
                self.mem.update_prospect(pid, attributes=attrs)
                self.trace.event("COORDONNEES", f"{p['full_name']} a partage ses coordonnees : enregistrees dans le CRM.")
            # un prospect dont le RDV est DEJA pris peut redemander (empechement) -> on rouvre
            new_state = p["state"] if p["state"] == M.MEETING_PROPOSED else M.IN_CONVERSATION
            self.mem.update_prospect(pid, awaiting_reply=0, next_action_at=None, state=new_state)
            self._crm_safe(f"note reponse {pid}",
                           lambda pid=pid, t=r["text"]: self.crm.log_note(pid, f"REPONSE: {t}"))
            self.trace.event("REPONSE RECUE", f"{p['full_name']} : {r['text'][:70]}")

    # ---- regles d'arret deterministes ----
    def _apply_stop_rules(self, cycle: int) -> None:
        for p in self.mem.list_prospects(M.OPEN_STATES):
            if (p["awaiting_reply"] and self._is_due(p)
                    and p["follow_up_count"] >= self.cfg.max_follow_ups):
                self.mem.update_prospect(p["id"], state=M.DEAD, awaiting_reply=0)
                reason = f"{p['follow_up_count']} relances sans reponse"
                self._crm_safe(f"note dead {p['id']}",
                               lambda pid=p["id"], r=reason: self.crm.log_note(pid, f"CLOTURE: {r}"))
                self.trace.decision(cycle, self.mem.get_prospect(p["id"]),
                                    "Critere d'arret atteint : on ne spamme pas.",
                                    "mark_no_response", {"prospect_id": p["id"], "reason": reason},
                                    "(regle automatique) prospect clos")
                self.mem.log_decision(cycle, p["id"], "max relances", "regle d'arret",
                                      "mark_no_response", {"reason": reason}, "clos")

    # ---- construction de l'etat ----
    def _build_snapshot(self, icp: str) -> dict:
        actionable, inflight = [], False
        for p in self.mem.list_prospects(M.OPEN_STATES):
            due = self._is_due(p)
            if self.read_only:
                act = (p["state"] == M.NEW)  # observation : on s'arrete apres la qualification
            elif p["state"] in (M.NEW, M.QUALIFIED):
                act = True
            elif not p["awaiting_reply"]:
                act = True  # une reponse est arrivee, a traiter
            elif due and p["follow_up_count"] < self.cfg.max_follow_ups:
                act = True  # relance due
            else:
                act = False
                inflight = True
            if act:
                actionable.append({
                    "id": p["id"], "full_name": p["full_name"], "headline": p["headline"],
                    "company": p["company"], "attributes": p["attributes"], "state": p["state"],
                    "qualification": p["qualification"], "awaiting": bool(p["awaiting_reply"]),
                    "due": due, "follow_up_count": p["follow_up_count"],
                    "offered_slots": p["offered_slots"],
                    "conversation": self.mem.get_conversation(p["id"]),
                })
        # priorite : prospects chauds (reponse en attente de traitement) d'abord
        actionable.sort(key=lambda x: (x["state"] == M.NEW, x["awaiting"]))
        recent = []
        for d in self.mem.get_decisions(limit=6):
            rp = self.mem.get_prospect(d["prospect_id"]) if d["prospect_id"] else None
            recent.append({"cycle": d["cycle"], "action": d["action"],
                           "target": rp["full_name"] if rp else "-",
                           "result": (d["result"] or "")[:80]})
        return {
            "icp": icp,
            "target": self.cfg.meeting_target,
            "booked": self.mem.counts_by_state().get(M.MEETING_BOOKED, 0),
            "counts": self.mem.counts_by_state(),
            "actionable": actionable,
            "inflight": inflight,
            "can_search": (not self.no_more_prospects) and (not self.resume_only),
            "tools_status": dict(self.tool_status),
            "recent_actions": recent,
        }

    def _observe(self, p: dict | None, snapshot: dict) -> str:
        if not p:
            return f"booked={snapshot['booked']}/{snapshot['target']}, actionnables={len(snapshot['actionable'])}"
        return f"{p['full_name']} etat={p['state']} relances={p['follow_up_count']} attente={p['awaiting_reply']}"

    # ---- prospect pilote (humain en direct) ----
    def _next_due(self, p) -> float:
        from tools.piloted import PILOT_ID
        if p["id"] == PILOT_ID:
            return self._now() + 10 ** 9  # pas de relance auto : l'humain repond a son rythme
        return self._now() + self._gap()

    def _pilot_inflight(self) -> bool:
        from tools.piloted import pilot_active, PILOT_ID
        if not pilot_active():
            return False
        p = self.mem.get_prospect(PILOT_ID)
        return bool(p and p["state"] in M.OPEN_STATES and p["awaiting_reply"])

    def _pilot_open(self) -> bool:  # ta conversation n'est pas terminee
        from tools.piloted import pilot_active, PILOT_ID
        if not pilot_active():
            return False
        p = self.mem.get_prospect(PILOT_ID)
        return bool(p and p["state"] in M.OPEN_STATES)

    def _wait_pilot(self, max_seconds: float = 900) -> bool:
        from tools.piloted import pilot_has_reply
        waited = 0.0
        while waited < max_seconds:
            if pilot_has_reply():
                return True
            time.sleep(1.5)
            waited += 1.5
        return False

    # ---- resolution tolerante d'un prospect ----
    # Les vrais prospects ont un id avec suffixe de hash (ex. li__corinne_seguin_46a28865)
    # que le LLM ne peut pas recopier de memoire. S'il fournit un id approximatif
    # (ex. "corinne_seguin"), on le rapproche par le nom au lieu de planter.
    def _resolve_prospect(self, pid):
        if not pid:
            return None
        p = self.mem.get_prospect(pid)
        if p:
            return p
        # Le LLM n'arrive pas a recopier un id a suffixe de hash : il ecrit par ex.
        # 'licorinne_seguin_46a28865' (il fusionne 'li' et 'corinne') au lieu de
        # 'li__corinne_seguin_46a28865'. On retrouve donc le prospect par son NOM :
        # tous les mots du nom doivent apparaitre dans l'id fourni, mis a plat.
        flat = re.sub(r"[^a-z0-9]", "", (pid or "").lower())
        if not flat:
            return None
        cands = []
        for q in self.mem.list_prospects():
            toks = [re.sub(r"[^a-z0-9]", "", t) for t in (q["full_name"] or "").lower().split()]
            toks = [t for t in toks if len(t) > 2]
            if toks and all(t in flat for t in toks):
                cands.append(q)
        return cands[0] if len(cands) == 1 else None

    _NEEDS_PROSPECT = {"get_profile", "check_client_base", "record_qualification",
                       "send_invitation", "send_message", "propose_meeting",
                       "book_meeting", "reschedule_meeting", "opt_out"}

    # ---- execution d'une action ----
    def _dispatch(self, action: str, args: dict, cycle: int) -> str:
        pid = args.get("prospect_id")
        p = self._resolve_prospect(pid)
        # id canonique : si le LLM a mal recopie l'id mais qu'on a retrouve le prospect,
        # on remet le vrai id dans args pour que tous les handlers ecrivent au bon endroit.
        if p and pid and pid != p["id"]:
            args["prospect_id"] = p["id"]
        # action qui exige un prospect mais id introuvable : message clair (pas un crash),
        # avec les ids valides pour que le LLM se corrige au lieu de boucler.
        if action in self._NEEDS_PROSPECT and not p:
            valids = "; ".join(f'{q["full_name"]} -> {q["id"]}'
                               for q in self.mem.list_prospects(M.OPEN_STATES)[:8])
            return (f"ECHEC : prospect_id '{pid}' introuvable. Recopie EXACTEMENT un id "
                    f"de la liste actionnable. Ids valides : {valids}")
        tool_of = {"search_prospects": "linkedin", "get_profile": "linkedin",
                   "send_invitation": "linkedin", "send_message": "linkedin",
                   "propose_meeting": "calendar", "book_meeting": "calendar",
                   "reschedule_meeting": "calendar", "check_client_base": "crm"}
        try:
            handler = getattr(self, f"_do_{action}", None)
            if not handler:
                return f"action inconnue : {action}"
            result = handler(p, args, cycle)
            if action in tool_of:
                self.tool_status[tool_of[action]] = "ok"
            return result
        except ToolUnavailable as e:
            if action in tool_of:
                self.tool_status[tool_of[action]] = "down"
            return f"ECHEC (outil indisponible) : {e}. On reessaiera plus tard."
        except RateLimited as e:
            return f"ECHEC (limite de debit) : {e}. On ralentit."
        except Exception as e:
            return f"ECHEC : {e}"

    # ---- handlers d'actions ----
    def _do_opt_out(self, p, args, cycle) -> str:
        if not p:
            return "REFUS : prospect introuvable."
        dnc.block(self._identity(p), p.get("full_name", ""), args.get("reason", "opposition du prospect"))
        self.mem.update_prospect(p["id"], state=M.DEAD, awaiting_reply=0)
        self._crm_safe(f"optout {p['id']}",
                       lambda: self.crm.log_note(p["id"], "OPPOSITION (RGPD) : ajoute a la liste de non-sollicitation, ne plus contacter."))
        self.trace.event("OPPOSITION RGPD", f"{p['full_name']} demande a ne plus etre contacte : clos + liste de non-sollicitation.")
        return f"{p['full_name']} retire et ajoute a la liste de non-sollicitation (ne sera jamais recontacte)."

    def _do_search_prospects(self, p, args, cycle) -> str:
        crit = args.get("criteria", {})
        limit = args.get("limit", 10)
        found = self.li.search_prospects(crit, limit)
        kept, blocked = [], 0
        for fp in found:
            if dnc.is_blocked(self._identity(fp)):
                blocked += 1  # respecte l'opposition : on ne le re-source meme pas
                continue
            self.mem.upsert_prospect(fp)
            kept.append(fp)
        if not kept:
            if not found:
                self.no_more_prospects = True
            return ("aucun nouveau prospect trouve." if not blocked
                    else f"aucun nouveau prospect (dont {blocked} ecarte(s) : liste de non-sollicitation).")
        suffix = f" ({blocked} ecarte(s) : non-sollicitation)" if blocked else ""
        return f"{len(kept)} prospect(s) ajoute(s){suffix} : " + ", ".join(x["full_name"] for x in kept)

    def _do_register_referral(self, p, args, cycle) -> str:
        name = (args.get("full_name") or "").strip()
        if not name:
            return "REFUS : nom du contact recommande manquant."
        referrer = p or (self.mem.get_prospect(args.get("from_prospect_id"))
                         if args.get("from_prospect_id") else None)
        referrer_name = referrer["full_name"] if referrer else "un contact"
        ref_company = (args.get("company") or "").strip() or (referrer["company"] if referrer else "")
        role = (args.get("role") or "").strip()
        # 1) Enrichir via recherche LinkedIn par NOM (mode reel Apify) si dispo
        found = {}
        searcher = getattr(self.li, "search_by_name", None)
        if callable(searcher):
            try:
                found = searcher(name, ref_company) or {}
            except Exception:
                found = {}
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:20] or str(cycle)
        new_id = "ref_" + slug
        company = found.get("company") or ref_company
        profile_url = found.get("profile_url", "")
        attrs = {"source": f"Recommande par {referrer_name}",
                 "referred_by": referrer_name, "profile_url": profile_url}
        # meme ecole que le referent (sauf si une autre est donnee) -> herite des criteres
        if referrer and not (args.get("company") or "").strip():
            rattrs = referrer.get("attributes", {}) or {}
            for k in ("school_type", "student_count", "region"):
                if rattrs.get(k):
                    attrs[k] = rattrs[k]
        if found.get("region"):
            attrs["region"] = found["region"]
        attrs.setdefault("region", "")
        new_prospect = {
            "id": new_id,
            "full_name": found.get("full_name") or name,
            "headline": found.get("headline") or role or "Poste a confirmer",
            "company": company,
            "profile_url": profile_url,
            "attributes": attrs,
        }
        if dnc.is_blocked(self._identity(new_prospect)):
            return (f"{name} figure sur la liste de non-sollicitation : non ajoute, on respecte son opposition.")
        self.mem.upsert_prospect(new_prospect)
        # le contact recommande se comporte comme un vrai prospect (profil, reponses)
        register = getattr(self.li, "register_referral", None)
        if callable(register):
            try:
                register(new_prospect)
            except Exception:
                pass
        # 2) Clore proprement le referent (pas le bon interlocuteur)
        if referrer:
            self.mem.update_prospect(referrer["id"], state=M.DISQUALIFIED, awaiting_reply=0)
            self._crm_safe(f"redir {referrer['id']}",
                           lambda: self.crm.log_note(referrer["id"],
                                    f"N'est pas le bon interlocuteur, redirige vers {name}."))
        via = "trouve sur LinkedIn" if profile_url else "profil a confirmer"
        return (f"Contact recommande ajoute : {name} ({company or 'ecole a confirmer'}, {via}). "
                f"Referent {referrer_name} clos. Etape suivante : qualifier puis contacter {name} "
                f"en mentionnant la recommandation de {referrer_name}.")

    def _do_get_profile(self, p, args, cycle) -> str:
        pid = args["prospect_id"]
        if pid in self.profiled:
            return (f"Profil DEJA recupere : {json.dumps(p['attributes'], ensure_ascii=False)}. "
                    f"Etape suivante : check_client_base puis record_qualification.")
        self.profiled.add(pid)
        prof = self.li.get_profile(pid)
        if prof.get("attributes"):
            merged = dict(p["attributes"]); merged.update(prof["attributes"])
            self.mem.update_prospect(pid, attributes=merged)
        return f"profil : {json.dumps(prof.get('attributes', {}), ensure_ascii=False)}"

    def _do_check_client_base(self, p, args, cycle) -> str:
        pid = args["prospect_id"]
        if pid in self.client_checked:
            res = p["attributes"].get("client_check", {})
            return (f"Base clients DEJA verifiee : deja_client={res.get('is_client')}. "
                    f"Etape suivante : record_qualification.")
        self.client_checked.add(pid)
        res = self.crm.check_existing(p["company"])
        attrs = dict(p["attributes"]); attrs["client_check"] = res
        self.mem.update_prospect(pid, attributes=attrs)
        return f"deja client = {res['is_client']}"

    def _do_record_qualification(self, p, args, cycle) -> str:
        elig = bool(args["eligible"])
        qual = {"eligible": elig, "reasons": args.get("reasons", ""),
                "buying_signals": args.get("buying_signals", "")}
        new_state = M.QUALIFIED if elig else M.DISQUALIFIED
        self.mem.update_prospect(args["prospect_id"], qualification=qual, state=new_state)
        if not elig:
            self._crm_safe(f"note disq {p['id']}",
                           lambda: self.crm.log_note(p["id"], f"DISQUALIFIE: {args.get('reasons','')}"))
        return f"{'QUALIFIE' if elig else 'DISQUALIFIE'} : {args.get('reasons','')}"

    def _do_send_invitation(self, p, args, cycle) -> str:
        if self.read_only:
            return "REFUS (mode observation) : aucune invitation n'est envoyee."
        if dnc.is_blocked(self._identity(p)):
            self.mem.update_prospect(p["id"], state=M.DEAD, awaiting_reply=0)
            return "REFUS (non-sollicitation) : ce prospect s'est oppose a tout contact, on ne l'invite pas."
        if p["state"] != M.QUALIFIED:
            return f"REFUS : on n'envoie une invitation qu'a un prospect qualifie (etat={p['state']})."
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        if self.mem.count_outbound_since("invite", week_ago) >= self.cfg.max_invites_per_week:
            return ("REFUS (plafond LinkedIn ~100 invitations/semaine, la contrainte dure) : "
                    "on temporise pour ne pas griller le compte.")
        if self._sent_recent("invite", 24) >= self.cfg.max_invites_per_day:
            return "REFUS (plafond LinkedIn) : quota d'invitations des 24h atteint."
        note = args["note"][:300]
        self.li.send_invitation(args["prospect_id"], note)
        self.mem.add_message(p["id"], "out", "invite", note)
        self.mem.update_prospect(p["id"], state=M.CONTACTED, awaiting_reply=1,
                                 next_action_at=self._next_due(p))
        self._enrich_email(p)  # devine l'email pro (Hunter) si dispo -> stocke ESTIME
        self._crm_safe(f"upsert {p['id']}", lambda: self.crm.upsert_contact(p))
        return "invitation envoyee."

    def _do_send_message(self, p, args, cycle) -> str:
        if self.read_only:
            return "REFUS (mode observation) : aucun message n'est envoye."
        if p["state"] not in (M.CONTACTED, M.IN_CONVERSATION, M.MEETING_PROPOSED):
            return f"REFUS : pas de conversation active (etat={p['state']})."
        is_followup = bool(p["awaiting_reply"])
        if is_followup and not self._is_due(p):
            return "REFUS (anti-spam) : une reponse est attendue, la relance n'est pas encore due."
        if is_followup and p["follow_up_count"] >= self.cfg.max_follow_ups:
            return "REFUS : nombre max de relances atteint, il faut cloturer (mark_no_response)."
        if self._sent_recent("message", 24) >= self.cfg.max_messages_per_day:
            return "REFUS (plafond LinkedIn) : quota de messages des 24h atteint."
        self.li.send_message(args["prospect_id"], args["text"], is_followup=is_followup)
        self.mem.add_message(p["id"], "out", "message", args["text"])
        fu = p["follow_up_count"] + (1 if is_followup else 0)
        self.mem.update_prospect(p["id"], state=M.IN_CONVERSATION, awaiting_reply=1,
                                 follow_up_count=fu, next_action_at=self._next_due(p))
        self._crm_safe(f"note msg {p['id']}",
                       lambda: self.crm.log_note(p["id"], f"NOUS{' (relance)' if is_followup else ''}: {args['text']}"))
        return "relance envoyee." if is_followup else "message envoye."

    def _do_propose_meeting(self, p, args, cycle) -> str:
        if self.read_only:
            return "REFUS (mode observation) : aucune proposition de RDV n'est envoyee."
        if p["state"] not in (M.CONTACTED, M.IN_CONVERSATION, M.MEETING_PROPOSED):
            return f"REFUS : pas de conversation active (etat={p['state']})."
        slots = self.cal.get_slots(self.cfg.window_days)  # peut lever ToolUnavailable
        slot_txt = "\n".join(f"- {s['label']}" for s in slots)
        text = f"{args['message']}\n\n{slot_txt}\n\nDites-moi le creneau qui vous convient."
        self.li.send_message(args["prospect_id"], text, is_followup=False)
        self.mem.add_message(p["id"], "out", "message", text)
        self.mem.update_prospect(p["id"], state=M.MEETING_PROPOSED, awaiting_reply=1,
                                 offered_slots=slots, next_action_at=self._next_due(p))
        self._crm_safe(f"note propose {p['id']}",
                       lambda: self.crm.log_note(p["id"], "PROPOSITION DE RDV envoyee"))
        return f"{len(slots)} creneaux proposes : " + " ; ".join(s["label"] for s in slots)

    def _do_book_meeting(self, p, args, cycle) -> str:
        if self.read_only:
            return "REFUS (mode observation) : aucun RDV n'est reserve."
        # garde anti-doublon : un prospect deja booke -> on DEPLACE au lieu de re-reserver
        if p and (p.get("attributes") or {}).get("booking_uid") and getattr(self.cal, "reschedule", None):
            return self._do_reschedule_meeting(p, args, cycle)
        if not p["offered_slots"]:
            return "REFUS : aucun creneau n'a encore ete propose a ce prospect."
        requested = args["slot_id"]
        # VERIFICATION DE DISPONIBILITE avant de reserver (evite le double-booking)
        available = self.cal.get_slots(self.cfg.window_days, limit=20)
        avail_ids = {s["id"] for s in available}
        slot_id, note = requested, ""
        if requested not in avail_ids:
            if not available:
                self.mem.update_prospect(p["id"], state=M.MEETING_PROPOSED, awaiting_reply=1,
                                         next_action_at=self._next_due(p))
                return ("INDISPONIBLE : le creneau demande n'est plus libre et il ne reste aucun "
                        "creneau. A reproposer plus tard.")
            slot_id = available[0]["id"]  # le creneau a ete pris entre-temps : on prend le suivant libre
            note = " (creneau initial pris entre-temps, reserve le prochain disponible)"
        attendee = (p.get("attributes") or {}).get("email") or self.s.owner_email
        booking = self.cal.book(p, slot_id, attendee)
        # CRM en best-effort (ne doit pas bloquer la prise de RDV)
        self._crm_safe(f"meeting {p['id']}", lambda: (
            self.crm.upsert_contact(p),
            self.crm.mark_meeting(p["id"], booking),
            self.crm.log_note(p["id"], f"RDV RESERVE: {booking.get('label')} {booking.get('link')}"),
        ))
        if note:
            confirm = (f"Le creneau initialement choisi venait d'etre pris ; je vous ai reserve le "
                       f"prochain disponible : {booking.get('label')}. Lien : {booking.get('link')}. A tres vite !")
        else:
            confirm = f"Parfait, c'est reserve pour {booking.get('label')}. Lien : {booking.get('link')}. A tres vite !"
        try:
            self.li.send_message(p["id"], confirm, is_followup=False)
            self.mem.add_message(p["id"], "out", "message", confirm)
        except ToolUnavailable:
            self.pending_crm.append((f"confirm {p['id']}",
                                     lambda: self.li.send_message(p["id"], confirm, is_followup=False)))
        attrs = dict(p.get("attributes") or {})
        attrs["booking_uid"] = booking.get("booking_id", "")
        attrs["booking_label"] = booking.get("label", "")
        self.mem.update_prospect(p["id"], state=M.MEETING_BOOKED, awaiting_reply=0, attributes=attrs)
        return f"RDV RESERVE{note} : {booking.get('label')} ({booking.get('link')})"

    def _do_reschedule_meeting(self, p, args, cycle) -> str:
        if self.read_only:
            return "REFUS (mode observation) : aucun deplacement de RDV."
        if not p:
            return "REFUS : prospect introuvable."
        uid = (p.get("attributes") or {}).get("booking_uid")
        resched = getattr(self.cal, "reschedule", None)
        if not uid or not callable(resched):
            return "REFUS : pas de RDV existant a deplacer pour ce prospect."
        requested = args.get("slot_id")
        available = self.cal.get_slots(self.cfg.window_days, limit=20)
        avail_ids = {s["id"] for s in available}
        slot_id, note = requested, ""
        if requested not in avail_ids:
            if not available:
                return "INDISPONIBLE : aucun creneau libre pour deplacer le RDV. A reproposer plus tard."
            slot_id = available[0]["id"]
            note = " (creneau demande indisponible, deplace au prochain libre)"
        booking = resched(uid, slot_id, "Empechement / demande du prospect")
        attrs = dict(p.get("attributes") or {})
        attrs["booking_uid"] = booking.get("booking_id", uid)
        attrs["booking_label"] = booking.get("label", "")
        confirm = (f"C'est note, je vous deplace ce rendez-vous{note}. Nouveau creneau : "
                   f"{booking.get('label')}. Lien : {booking.get('link')}. A tres vite !")
        try:
            self.li.send_message(p["id"], confirm, is_followup=False)
            self.mem.add_message(p["id"], "out", "message", confirm)
        except ToolUnavailable:
            self.pending_crm.append((f"resched confirm {p['id']}",
                                     lambda: self.li.send_message(p["id"], confirm, is_followup=False)))
        self.mem.update_prospect(p["id"], state=M.MEETING_BOOKED, awaiting_reply=0, attributes=attrs)
        self._crm_safe(f"resched {p['id']}",
                       lambda: self.crm.log_note(p["id"], f"RDV DEPLACE: {booking.get('label')} {booking.get('link')}"))
        return f"RDV DEPLACE{note} : {booking.get('label')} ({booking.get('link')})"

    def _do_disqualify(self, p, args, cycle) -> str:
        self.mem.update_prospect(args["prospect_id"], state=M.DISQUALIFIED)
        self._crm_safe(f"note disq {p['id']}",
                       lambda: self.crm.log_note(p["id"], f"DISQUALIFIE: {args.get('reason','')}"))
        return f"disqualifie : {args.get('reason','')}"

    def _do_mark_no_response(self, p, args, cycle) -> str:
        self.mem.update_prospect(args["prospect_id"], state=M.DEAD, awaiting_reply=0)
        return f"clos (sans reponse) : {args.get('reason','')}"

    # ---- CRM best-effort + file de reprise ----
    def _crm_safe(self, label: str, fn) -> None:
        try:
            fn()
        except ToolUnavailable:
            self.tool_status["crm"] = "down"
            self.pending_crm.append((label, fn))
            self.trace.event("CRM DIFFERE", f"{label} (CRM indisponible, sera reessaye).")
        except Exception:
            pass

    def _retry_pending_crm(self) -> None:
        if not self.pending_crm:
            return
        still = []
        for label, fn in self.pending_crm:
            try:
                fn()
                self.trace.event("REPRISE", f"{label} rejoue avec succes.")
            except Exception:
                still.append((label, fn))
        if not still and self.pending_crm:
            self.tool_status["crm"] = "ok"
        self.pending_crm = still

    # ---- bilan ----
    def _summary(self) -> None:
        counts = self.mem.counts_by_state()
        booked = counts.get(M.MEETING_BOOKED, 0)
        self.trace.event("BILAN", f"RDV pris={booked} | etats={json.dumps(counts, ensure_ascii=False)}")


# ====================================================================
#  Conversation pilotee : un humain joue le prospect, l'agent repond en direct
#  (pour tester les capacites de chatbot face a un interlocuteur imprevisible)
# ====================================================================
CHAT_TOOLS = [
    {"name": "consulter_disponibilites",
     "description": "Consulte l'agenda REEL. Si le prospect a propose un moment precis, passe-le dans "
                    "moment_souhaite pour verifier s'il est libre ; sinon, renvoie les prochaines "
                    "disponibilites. A appeler AVANT de proposer ou de confirmer toute date. Ne confirme "
                    "jamais un horaire sans l'avoir verifie ici.",
     "input_schema": {"type": "object", "properties": {
         "moment_souhaite": {"type": "string",
                             "description": "Moment demande par le prospect, format YYYY-MM-DDTHH:MM en heure de "
                                            "Paris (ex: 2026-07-02T14:00). Laisse vide pour lister les prochaines dispos."}}}},
    {"name": "reserver_rendez_vous",
     "description": "Reserve REELLEMENT le RDV dans l'agenda. A n'appeler QUE sur un creneau confirme libre. "
                    "L'outil re-verifie la disponibilite : si le creneau n'est pas libre, il renvoie des "
                    "alternatives sans rien reserver.",
     "input_schema": {"type": "object", "properties": {
         "moment": {"type": "string", "description": "Creneau a reserver, format YYYY-MM-DDTHH:MM heure de Paris."},
         "email_invite": {"type": "string", "description": "Email du prospect pour l'invitation (demande-le avant si tu ne l'as pas)."}},
         "required": ["moment"]}},
    {"name": "deplacer_rendez_vous",
     "description": "Quand le prospect veut DEPLACER un RDV deja reserve, deplace-le vers un nouveau creneau. "
                    "L'ancien creneau est ANNULE automatiquement (pas de doublon). L'outil verifie d'abord "
                    "que le nouveau creneau est libre ; sinon il renvoie des alternatives sans rien changer.",
     "input_schema": {"type": "object", "properties": {
         "nouveau_moment": {"type": "string", "description": "Nouveau creneau souhaite, format YYYY-MM-DDTHH:MM heure de Paris."},
         "email_invite": {"type": "string", "description": "Email du prospect (sert a retrouver son RDV existant)."}},
         "required": ["nouveau_moment"]}},
]


REFERRAL_CHAT_TOOL = {
    "name": "noter_contact_recommande",
    "description": "Quand le prospect dit qu'il n'est PAS le bon interlocuteur et te recommande "
                   "quelqu'un d'autre, enregistre ce contact (fiche creee dans le CRM) pour que "
                   "Periplea le contacte. Puis remercie le prospect dans ta reponse.",
    "input_schema": {"type": "object", "properties": {
        "full_name": {"type": "string", "description": "nom de la personne recommandee"},
        "role": {"type": "string", "description": "son poste, si donne"},
        "company": {"type": "string", "description": "son ecole, si donnee"}},
        "required": ["full_name"]},
}


def _chat_register_referral(crm, inp) -> str:
    name = (inp.get("full_name") or "").strip()
    if not name:
        return "Nom du contact manquant : redemande poliment le nom au prospect."
    if crm is None:
        return f"Contact {name} note (pas de CRM connecte). Remercie le prospect et dis que Periplea contactera {name}."
    pid = "chat_ref_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:18] or "x")
    prospect = {"id": pid, "full_name": name,
                "headline": (inp.get("role") or "Poste a confirmer"),
                "company": (inp.get("company") or ""),
                "attributes": {"source": "Recommande (test chatbot)", "profile_url": ""}}
    try:
        crm.upsert_contact(prospect)
        return (f"Contact {name} enregistre dans le CRM (source : recommandation). "
                f"Remercie le prospect et indique que tu vas contacter {name}.")
    except Exception as e:
        return f"Contact {name} note. Remercie le prospect (CRM indisponible : {e})."


def _run_chat_tool(calendar, crm, settings, profile, name, inp) -> str:
    from tools import chat_booking as CB
    inp = inp or {}
    try:
        if name == "consulter_disponibilites":
            return CB.consulter(calendar, inp.get("moment_souhaite", ""))
        if name == "reserver_rendez_vous":
            return CB.reserver(calendar, settings, profile, inp.get("moment", ""), inp.get("email_invite", ""))
        if name == "deplacer_rendez_vous":
            return CB.deplacer(calendar, settings, profile, inp.get("nouveau_moment", ""), inp.get("email_invite", ""))
        if name == "noter_contact_recommande":
            return _chat_register_referral(crm, inp)
    except Exception as e:
        return f"Erreur outil : {e}"
    return "Outil inconnu."


def chat_reply(settings, profile: str, history: list, message: str, calendar=None, crm=None) -> str:
    import anthropic
    from datetime import datetime
    if not settings.anthropic_api_key:
        return "(cle IA manquante : renseigne ANTHROPIC_API_KEY)"
    # CONVERSATION CLOSE : si l'agent a deja acte qu'il arrete le contact, on NE repond plus
    # (inutile de continuer a engager un prospect qui s'oppose / un interlocuteur hostile).
    if _agent_has_closed(history):
        return CONV_CLOSED
    # Premiere opposition (ou hostilite explicite) : on acquitte UNE fois, puis ce sera clos.
    _prospect_msgs = [h.get("text", "") for h in (history or []) if h.get("role") != "agent"]
    if dnc.detect_optout(message or "") or any(dnc.detect_optout(m) for m in _prospect_msgs):
        return "C'est note, je ne vous recontacterai plus. Bonne continuation."
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    today = datetime.now().strftime("%A %d %B %Y")
    system = (
        "Tu es un SDR de Periplea qui vend MeetYourSchool, un SaaS 9-en-1 pour les ecoles superieures "
        "privees francaises (admissions, scolarite, career center, relation alumni). Tu discutes EN DIRECT "
        f"sur LinkedIn avec un prospect : {profile}. Objectif : mener la conversation de facon credible, "
        "repondre aux objections et aux questions, detecter le bon moment et proposer un rendez-vous de "
        "30 minutes (entre 11h et 18h30). Ne donne jamais de prix ferme sans cadrer un "
        "echange. Ton professionnel, vouvoiement, concis (2 a 4 phrases). En francais. "
        "Adresse-toi au prospect par sa civilite et son NOM DE FAMILLE ('Bonjour Madame Seguin', "
        "'Bonjour Monsieur Dupont'), jamais par son prenom seul ; deduis Madame ou Monsieur du prenom, "
        "et en cas de doute reel sur le genre ecris 'Bonjour' suivi du prenom et du nom, sans civilite. "
        "N'utilise jamais le tiret cadratin, ni de champ entre crochets type [Prenom] ou [Ecole] : "
        "si tu ne connais pas un detail, formule sans, de facon naturelle. Ecris en TEXTE SIMPLE, "
        "sans mise en forme markdown (pas de gras avec des asterisques) : LinkedIn ne l'affiche pas. "
        "PERSONNALISATION : evite les compliments vagues ('votre role m'a semble pertinent'). "
        "Accroche-toi sur un element concret du poste/de l'ecole et relie-le a un enjeu precis du "
        "metier (admissions = suivi et relance des candidats ; career center = placement et relation "
        "entreprises ; promotion = remplissage des promos). N'invente jamais de fait que tu n'as pas. "
        "OPPOSITION : si le prospect demande a ne plus etre contacte (stop, retirez-moi, ne me "
        "contactez plus), confirme poliment que tu le retires et ne le recontacteras pas, sans insister."
    )
    is_opener = not history and not (message or "").strip()
    tools = []
    if calendar is not None:
        tools += CHAT_TOOLS
    if crm is not None:
        tools.append(REFERRAL_CHAT_TOOL)
    use_tools = bool(tools) and not is_opener
    if use_tools and calendar is not None:
        system += (
            f"\n\nNous sommes aujourd'hui le {today} (annee 2026). Tu disposes d'outils relies a "
            "l'agenda REEL : consulter_disponibilites et reserver_rendez_vous. Regles imperatives : "
            "(1) ne propose et ne confirme JAMAIS un horaire sans l'avoir verifie via consulter_disponibilites ; "
            "(2) si le prospect propose un creneau qui n'est pas libre, dis-le clairement et propose a la place "
            "les creneaux libres renvoyes par l'outil ; (3) avant de reserver, demande l'email du prospect pour "
            "l'invitation ; (4) reserve uniquement via reserver_rendez_vous, puis confirme l'horaire exact reserve ; "
            "(5) si le prospect veut DEPLACER un RDV deja pris, utilise deplacer_rendez_vous (l'ancien creneau est "
            "annule automatiquement), ne cree pas une seconde reservation. "
            "Convertis les dates parlees (par ex. '2 juillet a 14h') au format YYYY-MM-DDTHH:MM avant d'appeler un outil."
        )
    if use_tools and crm is not None:
        system += (
            "\n\nSi le prospect dit qu'il n'est pas le bon interlocuteur et te recommande quelqu'un d'autre, "
            "appelle noter_contact_recommande (nom + poste/ecole si donnes) pour enregistrer ce contact, "
            "puis remercie le prospect et indique que tu vas contacter la personne recommandee."
        )
    if is_opener:
        msgs = [{"role": "user", "content": "(Tu inities le contact LinkedIn avec ce prospect. Ecris ton tout "
                 "premier message d'approche, court (3 a 4 phrases), NATUREL et CHALEUREUX, comme une vraie "
                 "personne et non un argumentaire commercial. STRUCTURE : (1) commence par une ACCROCHE HUMAINE "
                 "qui explique comment/pourquoi tu arrives vers cette personne, par ex. 'en cherchant des "
                 "responsables admissions en ecoles de commerce, votre profil a retenu mon attention' ou 'je "
                 "cherchais justement quelqu'un qui pilote les admissions a [son ecole]' ; (2) ENSUITE seulement, "
                 "amene avec TACT le lien avec ce que MeetYourSchool apporte pour SON role, comme une hypothese "
                 "(pas en affirmant son quotidien comme un fait), ancre sur un fait reel de son profil ; (3) "
                 "termine par une question ouverte et legere. INTERDIT : attaquer direct par le pitch, les "
                 "formules vagues ('votre role m'a semble pertinent'), et inventer un fait. Ne propose pas encore de creneau.)"}]
    else:
        msgs = []
        for h in history:
            role = "assistant" if h.get("role") == "agent" else "user"
            content = h.get("text", "")
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + content
            else:
                msgs.append({"role": role, "content": content})
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n" + message
        else:
            msgs.append({"role": "user", "content": message})
        if not msgs or msgs[0]["role"] != "user":
            msgs.insert(0, {"role": "user", "content": "Bonjour"})
    try:
        for _ in range(6):
            kwargs = dict(model=settings.model, max_tokens=600, system=system, messages=msgs)
            if use_tools:
                kwargs["tools"] = tools
            resp = client.messages.create(**kwargs)
            if use_tools and resp.stop_reason == "tool_use":
                msgs.append({"role": "assistant", "content": resp.content})
                results = []
                for b in resp.content:
                    if getattr(b, "type", "") == "tool_use":
                        out = _run_chat_tool(calendar, crm, settings, profile, b.name, b.input)
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                msgs.append({"role": "user", "content": results})
                continue
            txt = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            return _clean_text(txt or "...")
        return "Un instant, je verifie mon agenda et je reviens vers vous tres vite."
    except Exception as e:
        return f"(erreur IA : {e})"
