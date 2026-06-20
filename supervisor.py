"""Console operateur : l'humain (l'equipe) interroge et PILOTE l'agent.

Difference avec le chatbot 'prospect' (ou l'humain JOUE un prospect face a l'agent) :
ici l'humain est l'OPERATEUR. Il peut demander a l'agent ou il en est avec un
prospect, lui faire ecrire un message pour transmettre une information, ou lui
faire deplacer un RDV deja pris (empechement de derniere minute).

L'agent lit l'etat reel de la campagne (base SQLite) et agit via deux outils :
ecrire_au_prospect et reprogrammer_rdv.
"""
from __future__ import annotations

import dnc
import memory as M

OPERATOR_TOOLS = [
    {"name": "ecrire_au_prospect",
     "description": "Envoie un message a un prospect (au nom de l'agent) pour lui transmettre une "
                    "information. Le message est ajoute a la conversation et au CRM. Redige-le toi-meme, "
                    "personnalise selon le prospect et l'info a transmettre.",
     "input_schema": {"type": "object", "properties": {
         "prospect": {"type": "string", "description": "nom (ou id) du prospect, tel qu'il apparait dans le pipeline"},
         "message": {"type": "string", "description": "le message a envoyer"}},
         "required": ["prospect", "message"]}},
    {"name": "reprogrammer_rdv",
     "description": "Deplace le RDV deja pris d'un prospect vers un nouveau creneau (cas d'un empechement "
                    "de notre cote). L'ancien creneau est annule automatiquement. L'outil verifie la "
                    "disponibilite et previent le prospect.",
     "input_schema": {"type": "object", "properties": {
         "prospect": {"type": "string"},
         "nouveau_moment": {"type": "string", "description": "nouveau creneau au format YYYY-MM-DDTHH:MM heure de Paris"},
         "raison": {"type": "string", "description": "raison de l'empechement, a mentionner poliment au prospect"}},
         "required": ["prospect", "nouveau_moment"]}},
]


def _find_prospect(mem, q):
    q = (q or "").strip().lower()
    if not q:
        return None
    prospects = mem.list_prospects()
    for p in prospects:
        if p["id"].lower() == q or q == p["full_name"].lower():
            return p
    for p in prospects:
        if q in p["full_name"].lower() or q in (p.get("company") or "").lower():
            return p
    return None


def _state_summary(mem) -> str:
    lines = []
    for p in mem.list_prospects():
        conv = mem.get_conversation(p["id"])
        last = conv[-1]["text"][:90] if conv else ""
        rdv = (p.get("attributes") or {}).get("booking_label", "")
        seg = f"- {p['full_name']} ({p.get('company','')}) | etat={p['state']}"
        if rdv:
            seg += f" | RDV={rdv}"
        if last:
            seg += f" | dernier message: {last}"
        lines.append(seg)
    body = "\n".join(lines) or "(aucun prospect dans la base pour le moment)"
    n_dnc = dnc.count()
    if n_dnc:
        body += f"\n\nListe de non-sollicitation (opposition RGPD) : {n_dnc} personne(s), a ne jamais recontacter."
    return body


def _build_msgs(history, question):
    msgs = []
    for h in history or []:
        role = "assistant" if h.get("role") == "agent" else "user"
        content = h.get("text", "")
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    if (question or "").strip():
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n" + question
        else:
            msgs.append({"role": "user", "content": question})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "Bonjour"})
    return msgs


def _run_operator_tool(settings, db_path, calendar, crm, linkedin, name, inp) -> str:
    from tools import chat_booking as CB
    inp = inp or {}
    mem = M.Memory(db_path)
    try:
        p = _find_prospect(mem, inp.get("prospect", ""))
        if not p:
            return f"Prospect introuvable : '{inp.get('prospect','')}'. Utilise un nom present dans le pipeline."
        if name == "ecrire_au_prospect":
            msg = (inp.get("message") or "").strip()
            if not msg:
                return "Message vide : redige le contenu a transmettre."
            try:
                if linkedin:
                    linkedin.send_message(p["id"], msg, is_followup=False)
            except Exception:
                pass
            mem.add_message(p["id"], "out", "message", msg)
            new_state = M.CONTACTED if p["state"] in (M.NEW, M.QUALIFIED) else p["state"]
            mem.update_prospect(p["id"], state=new_state, awaiting_reply=1)
            if crm:
                try:
                    crm.log_note(p["id"], f"NOUS (operateur): {msg}")
                except Exception:
                    pass
            return f"Message envoye a {p['full_name']} : \"{msg[:90]}\""
        if name == "reprogrammer_rdv":
            attrs = dict(p.get("attributes") or {})
            uid = attrs.get("booking_uid")
            resched = getattr(calendar, "reschedule", None) if calendar else None
            if not uid or not callable(resched):
                return f"{p['full_name']} n'a pas de RDV deplacable (aucune reservation enregistree)."
            req = CB._parse_moment(inp.get("nouveau_moment", ""))
            if not req:
                return "Nouvel horaire non compris (format attendu : YYYY-MM-DDTHH:MM)."
            try:
                slots = CB.list_free_slots(calendar)
            except Exception as e:
                return f"Agenda indisponible : {e}."
            m = CB._match(slots, req)
            if not m:
                alts = CB._soonest(slots, req, 5)
                return "Ce creneau n'est pas libre. Creneaux possibles :\n" + CB._fmt(alts)
            try:
                booking = resched(uid, m["id"], inp.get("raison", "Empechement de notre cote"))
            except Exception as e:
                return f"Echec du deplacement : {e}."
            attrs["booking_uid"] = booking.get("booking_id", uid)
            attrs["booking_label"] = m["label"]
            mem.update_prospect(p["id"], attributes=attrs, state=M.MEETING_BOOKED, awaiting_reply=1)
            raison = inp.get("raison", "")
            msg = ("Bonjour, un imprevu de notre cote nous oblige a decaler notre echange"
                   + (f" ({raison})" if raison else "") + f". Je vous propose plutot {m['label']}. "
                   "Est-ce que cela vous convient ?")
            mem.add_message(p["id"], "out", "message", msg)
            try:
                if linkedin:
                    linkedin.send_message(p["id"], msg, is_followup=False)
            except Exception:
                pass
            if crm:
                try:
                    crm.log_note(p["id"], f"RDV DEPLACE (operateur): {m['label']}")
                except Exception:
                    pass
            return f"RDV de {p['full_name']} deplace a {m['label']}, et message d'information envoye."
        return "Outil inconnu."
    finally:
        mem.close()


def supervisor_reply(settings, question: str, history: list,
                     db_path: str = "sdr_memory.db", calendar=None, crm=None, linkedin=None) -> str:
    import anthropic
    from datetime import datetime
    if not settings.anthropic_api_key:
        return "(cle IA manquante : renseigne ANTHROPIC_API_KEY)"
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    mem = M.Memory(db_path)
    try:
        summary = _state_summary(mem)
    finally:
        mem.close()
    today = datetime.now().strftime("%A %d %B %Y")
    system = (
        "Tu es l'agent SDR de Periplea. Ici tu parles a ton OPERATEUR humain (l'equipe Periplea), "
        "PAS a un prospect. Reponds a ses questions sur l'avancement de la prospection, et execute "
        "ses consignes : ecrire a un prospect pour lui transmettre une info, ou deplacer un RDV pris "
        "(empechement). Sois concis et factuel, en francais, sans tiret cadratin. Quand l'operateur "
        "te demande d'agir, utilise l'outil adapte ; quand il demande une information, reponds en texte "
        "en t'appuyant sur l'etat ci-dessous.\n\n"
        f"Nous sommes le {today}.\n\nETAT ACTUEL DU PIPELINE :\n{summary}\n"
    )
    msgs = _build_msgs(history, question)
    try:
        for _ in range(6):
            resp = client.messages.create(model=settings.model, max_tokens=800, system=system,
                                           tools=OPERATOR_TOOLS, messages=msgs)
            if resp.stop_reason == "tool_use":
                msgs.append({"role": "assistant", "content": resp.content})
                results = []
                for b in resp.content:
                    if getattr(b, "type", "") == "tool_use":
                        out = _run_operator_tool(settings, db_path, calendar, crm, linkedin, b.name, b.input)
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                msgs.append({"role": "user", "content": results})
                continue
            txt = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            return (txt or "...").replace("—", "-").replace("–", "-").replace("**", "").replace("__", "")
        return "Demande traitee."
    except Exception as e:
        return f"(erreur IA : {e})"
