"""Outils simules - mode "bac a sable".

Fait tourner TOUT le cerveau de l'agent sans aucun compte externe :
- faux annuaire LinkedIn (7 prospects scenarises avec reponses simulees),
- faux agenda (creneaux + reservation),
- faux CRM (contacts + base clients + notes).

Inclut l'interrupteur de panne (chaos.py) pour "couper" un outil en
direct et observer la recuperation de l'agent.

Les 7 prospects couvrent expres tous les cas du brief :
- Claire  : eligible, chaleureuse -> RDV
- Marc    : eligible (career center), objection puis -> RDV
- Paul    : eligible, repond 3 jours plus tard -> RDV (teste la memoire)
- Julien  : PIEGE, repond hors-script ("envoyez vos tarifs")
- Ines    : eligible mais ne repond jamais -> N relances puis abandon
- Sophie  : ecole d'ingenieur PUBLIQUE -> a disqualifier
- ESSCA   : deja cliente -> a disqualifier
"""
from __future__ import annotations

from datetime import datetime, timedelta

from chaos import tool_is_down
from .base import ToolUnavailable, ToolError
from .chat_booking import drop_buffered


CLIENTS = {"ESSCA", "Excelia", "OMNES", "Institut Lyfe", "ESCP"}

# Conversation d'un contact RECOMMANDE (lead chaud : il repond et prend RDV).
WARM_REFERRAL_SCRIPT = [
    {"after_out": 1, "delay": 1,
     "text": "Bonjour, merci pour votre message. Oui, c'est bien moi qui gere ce sujet ici. De quoi s'agit-il ?"},
    {"after_out": 2, "delay": 1,
     "text": "Tres bien, ca m'interesse. Un echange de 30 minutes me convient."},
    {"after_out": 3, "delay": 1,
     "text": "Le premier creneau propose me convient."},
]


def _seed() -> list[dict]:
    return [
        {
            "id": "li_claire", "full_name": "Claire Fontaine",
            "headline": "Directrice des Admissions",
            "company": "ISG Programme Business Network",
            "profile_url": "https://www.linkedin.com/in/claire-fontaine-demo",
            "attributes": {"role": "admissions", "school_type": "privee",
                           "student_count": 2200, "region": "Paris"},
            "_script": [
                {"after_out": 1, "delay": 1,
                 "text": "Bonjour, merci pour l'invitation. MeetYourSchool, concretement, ca couvre quoi pour un service admissions ?"},
                {"after_out": 2, "delay": 1,
                 "text": "Interessant. On jongle justement entre 4 outils differents. Je serais curieuse d'en voir plus."},
                {"after_out": 3, "delay": 1,
                 "text": "Le premier creneau me convient tres bien, merci."},
            ],
        },
        {
            "id": "li_marc", "full_name": "Marc Olivier",
            "headline": "Responsable Career Center",
            "company": "PPA Business School",
            "profile_url": "https://www.linkedin.com/in/marc-olivier-demo",
            "attributes": {"role": "career", "school_type": "privee",
                           "student_count": 900, "region": "Paris"},
            "_script": [
                {"after_out": 1, "delay": 1,
                 "text": "Bonjour. On a deja La Growth Machine pour la prospection, en quoi MeetYourSchool est different ?"},
                {"after_out": 2, "delay": 1,
                 "text": "Ah, ce n'est pas du tout le meme sujet alors. Ok, je veux bien un echange rapide."},
                {"after_out": 3, "delay": 1,
                 "text": "Le deuxieme creneau propose me convient."},
            ],
        },
        {
            "id": "li_paul", "full_name": "Paul Lentier",
            "headline": "Directeur du Career Center",
            "company": "Brest Business School",
            "profile_url": "https://www.linkedin.com/in/paul-lentier-demo",
            "attributes": {"role": "career", "school_type": "privee",
                           "student_count": 1200, "region": "Brest"},
            "_script": [
                {"after_out": 1, "delay": 3,
                 "text": "Desole pour le delai, j'etais en deplacement. Oui, le sujet m'interesse."},
                {"after_out": 2, "delay": 1,
                 "text": "Avec plaisir pour un point de 30 min."},
                {"after_out": 3, "delay": 1,
                 "text": "Le troisieme creneau propose me convient."},
            ],
        },
        {
            "id": "li_julien", "full_name": "Julien Marais",
            "headline": "Admissions & Developpement",
            "company": "EICAR",
            "profile_url": "https://www.linkedin.com/in/julien-marais-demo",
            "attributes": {"role": "admissions", "school_type": "privee",
                           "student_count": 1500, "region": "Paris"},
            "_script": [
                {"after_out": 1, "delay": 1,
                 "text": "Pas le temps pour un call. Envoyez-moi directement vos tarifs par message."},
                {"after_out": 2, "delay": 1,
                 "text": "Bon... d'accord, 15 minutes maximum. Quand ?"},
                {"after_out": 3, "delay": 1,
                 "text": "Le dernier creneau propose me convient."},
            ],
        },
        {
            "id": "li_ines", "full_name": "Ines Roche",
            "headline": "Directrice des Admissions",
            "company": "ESCE International Business School",
            "profile_url": "https://www.linkedin.com/in/ines-roche-demo",
            "attributes": {"role": "admissions", "school_type": "privee",
                           "student_count": 1800, "region": "Paris"},
            "_script": [],  # ne repond jamais
        },
        {
            "id": "li_sophie", "full_name": "Sophie Bernard",
            "headline": "Directrice de la Promotion et des Partenariats",
            "company": "ENSIP - Ecole Nationale Superieure d'Ingenieurs",
            "profile_url": "https://www.linkedin.com/in/sophie-bernard-demo",
            "attributes": {"role": "promotion", "school_type": "ingenieur_publique",
                           "student_count": 4000, "region": "Poitiers"},
            "_script": [],  # ne devrait jamais etre contactee (disqualifiee)
        },
        {
            "id": "li_essca", "full_name": "Helene Dubreuil",
            "headline": "Directrice des Admissions",
            "company": "ESSCA",
            "profile_url": "https://www.linkedin.com/in/helene-dubreuil-demo",
            "attributes": {"role": "admissions", "school_type": "privee",
                           "student_count": 3000, "region": "Angers"},
            "_script": [],  # deja cliente -> disqualifiee
        },
    ]


class MockLinkedIn:
    def __init__(self):
        self.directory = {p["id"]: p for p in _seed()}
        self.current_tick = 0
        self.reply_queue: list[dict] = []      # {prospect_id, text, ready_tick}
        # "stage" = nombre de messages SUBSTANTIELS envoyes (les relances ne
        # comptent pas) -> les reponses scenarisees ne se decalent pas.
        self.stage: dict[str, int] = {}
        self.returned_ids: set[str] = set()

    def set_tick(self, t: int) -> None:
        self.current_tick = t

    def _check(self) -> None:
        if tool_is_down("linkedin"):
            raise ToolUnavailable("LinkedIn (Unipile) indisponible")

    # --- recherche ---
    def search_prospects(self, criteria: dict, limit: int = 10) -> list[dict]:
        self._check()
        kw = [k.lower() for k in (criteria.get("role_keywords") or [])]
        min_s = criteria.get("min_students")
        max_s = criteria.get("max_students")
        out = []
        for p in self.directory.values():
            if p["id"] in self.returned_ids:
                continue
            sc = p["attributes"].get("student_count", 0)
            if min_s is not None and sc < min_s:
                continue
            if max_s is not None and sc > max_s:
                continue
            hay = (p["headline"] + " " + p["company"] + " "
                   + p["attributes"].get("role", "")).lower()
            if kw:
                # on tokenise les mots-cles (l'ICP est flou) : on matche si un
                # mot significatif apparait. "directeur admissions" -> matche
                # via "admissions" meme si le titre est "directrice des admissions".
                words = {w for k in kw for w in k.split() if len(w) >= 4}
                if words and not any(w in hay for w in words):
                    continue
            out.append({k: p[k] for k in
                        ("id", "full_name", "headline", "company", "profile_url", "attributes")})
            if len(out) >= limit:
                break
        for p in out:
            self.returned_ids.add(p["id"])
        return out

    def register_referral(self, prospect: dict) -> None:
        """Ajoute un contact recommande a l'annuaire avec une conversation de lead chaud."""
        pid = prospect["id"]
        entry = {k: prospect.get(k, "") for k in
                 ("id", "full_name", "headline", "company", "profile_url")}
        entry["attributes"] = prospect.get("attributes", {}) or {}
        # l'ecole du referent est dans la cible -> on rend le contact recommande qualifiable
        entry["attributes"].setdefault("role", "admissions")
        entry["attributes"].setdefault("school_type", "privee")
        entry["attributes"].setdefault("student_count", 1500)
        entry["_script"] = list(WARM_REFERRAL_SCRIPT)
        self.directory[pid] = entry
        self.returned_ids.add(pid)  # ne pas le re-proposer en recherche

    def get_profile(self, prospect_id: str) -> dict:
        self._check()
        p = self.directory.get(prospect_id, {})
        return {k: p[k] for k in
                ("id", "full_name", "headline", "company", "profile_url", "attributes")
                if k in p}

    # --- envois ---
    def _record_outbound(self, prospect_id: str, substantive: bool = True) -> None:
        # Une relance (message identique sans nouveau contenu) ne fait pas
        # avancer le scenario du prospect : un non-repondant reste muet, un
        # repondant lent repond toujours au message d'origine.
        if not substantive:
            return
        n = self.stage.get(prospect_id, 0) + 1
        self.stage[prospect_id] = n
        for step in self.directory.get(prospect_id, {}).get("_script", []):
            if step["after_out"] == n:
                self.reply_queue.append({
                    "prospect_id": prospect_id,
                    "text": step["text"],
                    "ready_tick": self.current_tick + step["delay"],
                })

    def send_invitation(self, prospect_id: str, note: str) -> dict:
        self._check()
        self._record_outbound(prospect_id, substantive=True)
        return {"ok": True, "channel": "invite"}

    def send_message(self, prospect_id: str, text: str, is_followup: bool = False) -> dict:
        self._check()
        self._record_outbound(prospect_id, substantive=not is_followup)
        return {"ok": True, "channel": "message"}

    # --- reception des reponses (asynchrone) ---
    def fetch_new_replies(self) -> list[dict]:
        self._check()
        ready, kept = [], []
        for r in self.reply_queue:
            if r["ready_tick"] <= self.current_tick:
                ready.append({"prospect_id": r["prospect_id"], "text": r["text"]})
            else:
                kept.append(r)
        self.reply_queue = kept
        return ready


class MockCalendar:
    def __init__(self, work_start: int = 11, work_end: int = 19):
        # horaires de travail : dernier creneau de 30 min = work_end - 30 min (donc 18h30)
        self.work_start = work_start
        self.work_end = work_end
        self.booked: set[str] = set()  # creneaux deja reserves (pour la verif de dispo)
        self._by_uid: dict[str, str] = {}   # uid de reservation -> slot_id
        self._last_booking: dict = {}       # dernier RDV (pour find_booking)
        self._slots = self._gen_slots()

    def _gen_slots(self) -> list[dict]:
        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        cand = [(self.work_start, 0), (14, 30), (16, 0), (self.work_end - 1, 0), (self.work_end - 1, 30)]
        last = self.work_end * 60 - 30  # dernier depart possible (18h30 si fin 19h)
        times = [(h, m) for (h, m) in cand if self.work_start * 60 <= h * 60 + m <= last]
        slots = []
        day = datetime.now()
        i = 0
        while len(slots) < 5 and times:
            day = day + timedelta(days=1)
            if day.weekday() >= 5:  # week-end
                continue
            h, m = times[i % len(times)]
            i += 1
            start = day.replace(hour=h, minute=m, second=0, microsecond=0)
            hm = f"{h}h" if m == 0 else f"{h}h{m:02d}"
            slots.append({
                "id": f"slot{len(slots)+1}",
                "start": start.isoformat(),
                "label": f"{jours[start.weekday()]} {start.strftime('%d/%m')} a {hm}",
            })
        return slots

    def _check(self) -> None:
        if tool_is_down("calendar"):
            raise ToolUnavailable("Agenda (Cal.com) indisponible")

    def get_slots(self, within_days: int = 14, limit: int = 5) -> list[dict]:
        self._check()
        avail = [s for s in self._slots if s["id"] not in self.booked]  # exclut les creneaux pris
        # battement : ecarte les creneaux trop proches d'un RDV deja pris
        busy = [s["start"] for s in self._slots if s["id"] in self.booked]
        avail = drop_buffered(avail, busy)
        return avail[:limit]

    def book(self, prospect: dict, slot_id: str, attendee_email: str) -> dict:
        self._check()
        slot = next((s for s in self._slots if s["id"] == slot_id), None)
        if not slot:
            raise ValueError(f"creneau inconnu : {slot_id}")
        if slot_id in self.booked:
            raise ToolError(f"creneau {slot_id} deja reserve")
        self.booked.add(slot_id)
        uid = f"bk_{prospect['id']}_{slot_id}"
        self._by_uid[uid] = slot_id
        self._last_booking = {"uid": uid, "slot_id": slot_id,
                              "email": attendee_email, "start": slot["start"]}
        return {
            "booking_id": uid,
            "link": f"https://cal.com/booking/{uid}",
            "start": slot["start"],
            "label": slot["label"],
        }

    def find_booking(self, attendee_email: str = "") -> dict:
        return dict(self._last_booking) if self._last_booking else {}

    def upcoming_bookings(self, attendee_email: str = "") -> list:
        """Tous les RDV en cours : [{uid, start}, ...] (sert a retrouver le bon par son heure)."""
        out = []
        for uid, slot_id in self._by_uid.items():
            slot = next((s for s in self._slots if s["id"] == slot_id), None)
            if slot:
                out.append({"uid": uid, "start": slot["start"]})
        return out

    def reschedule(self, booking_uid: str, new_slot_id: str, reason: str = "") -> dict:
        self._check()
        slot = next((s for s in self._slots if s["id"] == new_slot_id), None)
        if not slot:
            raise ValueError(f"creneau inconnu : {new_slot_id}")
        if new_slot_id in self.booked:
            raise ToolError(f"creneau {new_slot_id} deja reserve")
        old_slot = self._by_uid.get(booking_uid)
        if old_slot:
            self.booked.discard(old_slot)   # libere l'ancien creneau automatiquement
        self.booked.add(new_slot_id)
        new_uid = f"bk_resched_{new_slot_id}"
        self._by_uid.pop(booking_uid, None)
        self._by_uid[new_uid] = new_slot_id
        self._last_booking = {"uid": new_uid, "slot_id": new_slot_id, "start": slot["start"]}
        return {"booking_id": new_uid, "link": f"https://cal.com/booking/{new_uid}",
                "start": slot["start"], "label": slot["label"]}

    def cancel(self, booking_uid: str, reason: str = "") -> dict:
        old_slot = self._by_uid.pop(booking_uid, None)
        if old_slot:
            self.booked.discard(old_slot)
        return {"ok": True, "uid": booking_uid}


class MockCrm:
    def __init__(self):
        self.contacts: dict[str, dict] = {}
        self.notes: dict[str, list[str]] = {}
        self.meetings: dict[str, dict] = {}

    def _check(self) -> None:
        if tool_is_down("crm"):
            raise ToolUnavailable("CRM (HubSpot) indisponible")

    def check_existing(self, company: str) -> dict:
        self._check()
        return {"is_client": company.strip() in CLIENTS}

    def upsert_contact(self, prospect: dict) -> str:
        self._check()
        self.contacts[prospect["id"]] = prospect
        return prospect["id"]

    def log_note(self, prospect_id: str, text: str) -> None:
        self._check()
        self.notes.setdefault(prospect_id, []).append(text)

    def mark_meeting(self, prospect_id: str, details: dict) -> None:
        self._check()
        self.meetings[prospect_id] = details

    def set_email(self, prospect_id: str, email: str) -> None:
        c = self.contacts.get(prospect_id)
        if c is not None:
            c.setdefault("attributes", {})["email"] = email

    def set_phone(self, prospect_id: str, phone: str) -> None:
        c = self.contacts.get(prospect_id)
        if c is not None:
            c.setdefault("attributes", {})["phone"] = phone
