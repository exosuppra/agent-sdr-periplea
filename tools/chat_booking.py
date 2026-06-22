"""Outils agenda pour le mini-chatbot pilote (panneau 'Conversation pilotee').

Le mini-chatbot devient un vrai agent a outils : il consulte les creneaux
LIBRES de l'agenda REEL (Cal.com), refuse un creneau occupe, et reserve pour
de vrai (le RDV arrive dans l'agenda). Fonctionne aussi avec MockCalendar.

Gestion du fuseau : Cal.com renvoie des heures en UTC (suffixe Z), MockCalendar
renvoie des heures locales naives. On normalise tout en heure de Paris pour
comparer ce que demande le prospect ('le 2 juillet a 14h') aux vrais creneaux.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["", "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
        "aout", "septembre", "octobre", "novembre", "decembre"]

# Battement entre deux RDV : on laisse 30 min LIBRES entre la fin d'un RDV et le
# debut du suivant. Un RDV dure 30 min, donc deux debuts de RDV doivent etre
# espaces d'au moins 60 min (30 de RDV + 30 de battement).
DUREE_RDV_MIN = 30
BATTEMENT_MIN = 30
ECART_MIN_DEBUTS = DUREE_RDV_MIN + BATTEMENT_MIN  # 60


# ---------- fuseau Europe/Paris (sans dependance tzdata) ----------
def _eu_summer_time(dt_utc: datetime) -> bool:
    """Vrai si l'heure d'ete europeenne s'applique (dernier dimanche de mars a
    01h UTC -> dernier dimanche d'octobre a 01h UTC)."""
    y = dt_utc.year
    mar = datetime(y, 3, 31, 1, tzinfo=timezone.utc)
    while mar.weekday() != 6:
        mar -= timedelta(days=1)
    oct_ = datetime(y, 10, 31, 1, tzinfo=timezone.utc)
    while oct_.weekday() != 6:
        oct_ -= timedelta(days=1)
    return mar <= dt_utc < oct_


def _to_paris(dt_utc: datetime) -> datetime:
    off = 2 if _eu_summer_time(dt_utc) else 1
    return (dt_utc + timedelta(hours=off)).replace(tzinfo=None)


def _slot_paris(slot: dict) -> datetime:
    """Heure de Paris (naive) d'un creneau, qu'il vienne de Cal.com (UTC) ou du mock (local)."""
    iso = slot.get("start") or slot.get("id") or ""
    s = iso.strip()
    if s.endswith("Z") or ("+" in s[10:]) or ("-" in s[11:]):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _to_paris(dt.astimezone(timezone.utc))
    return datetime.fromisoformat(s).replace(tzinfo=None, second=0, microsecond=0)


def _label(dt: datetime) -> str:
    return f"{JOURS[dt.weekday()]} {dt.day} {MOIS[dt.month]} a {dt.hour}h{dt.minute:02d}"


def human_label(iso: str) -> str:
    """Libelle lisible en heure de Paris a partir d'un ISO (UTC 'Z' ou avec offset)."""
    try:
        return _label(_slot_paris({"start": iso}))
    except Exception:
        return iso


def list_free_slots(calendar, days: int = 28, limit: int = 300) -> list[dict]:
    raw = calendar.get_slots(days, limit)
    out = []
    for s in raw:
        try:
            dt = _slot_paris(s)
        except Exception:
            continue
        out.append({"id": s["id"], "paris": dt, "label": _label(dt)})
    out.sort(key=lambda x: x["paris"])
    return out


def drop_buffered(raw_slots: list[dict], busy_isos, ecart_min: int = ECART_MIN_DEBUTS) -> list[dict]:
    """Retire les creneaux trop proches d'un RDV deja pris, pour garantir un
    battement libre entre deux RDV. `busy_isos` = heures de debut (ISO) des RDV
    existants. Un creneau est ecarte si son debut est a moins de `ecart_min`
    minutes du debut d'un RDV existant (avant OU apres)."""
    busy = []
    for b in (busy_isos or []):
        try:
            busy.append(_slot_paris({"start": b}))
        except Exception:
            pass
    if not busy:
        return raw_slots
    out = []
    for s in raw_slots:
        try:
            dt = _slot_paris(s)
        except Exception:
            out.append(s)  # on ne sait pas situer ce creneau : on ne le bloque pas
            continue
        if all(abs((dt - b).total_seconds()) >= ecart_min * 60 for b in busy):
            out.append(s)
    return out


def _parse_moment(moment: str):
    if not moment:
        return None
    try:
        d = datetime.fromisoformat(moment.replace("Z", "").strip())
        return d.replace(tzinfo=None, second=0, microsecond=0)
    except Exception:
        return None


def _match(slots: list[dict], req: datetime):
    if not req:
        return None
    for s in slots:
        d = s["paris"]
        if (d.year, d.month, d.day, d.hour, d.minute) == (req.year, req.month, req.day, req.hour, req.minute):
            return s
    return None


def _soonest(slots: list[dict], req, n: int = 5) -> list[dict]:
    if req:
        # priorite : meme date exacte, PUIS meme jour de semaine (ex. le prospect veut "un
        # mercredi" -> on remonte d'autres mercredis), PUIS le reste chronologique. Sinon
        # un creneau d'un jour demande passerait inapercu derriere les creneaux les plus proches.
        same_date = [s for s in slots if s["paris"].date() == req.date()]
        same_wday = [s for s in slots if s["paris"].date() != req.date()
                     and s["paris"].weekday() == req.weekday()]
        rest = [s for s in slots if s["paris"].weekday() != req.weekday()]
        ordered = same_date + same_wday + rest
    else:
        ordered = slots
    return ordered[:n]


def _fmt(slots: list[dict]) -> str:
    return "\n".join(f"- {s['label']}" for s in slots) or "(aucun)"


def _fmt_hm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def _hors_horaires(req, slots: list[dict]):
    """Plage horaire de RDV deduite des creneaux libres (min/max heure de la journee).
    Renvoie (hors_plage: bool, (debut_min, fin_min) | None)."""
    mins = [s["paris"].hour * 60 + s["paris"].minute for s in slots]
    if not mins or not req:
        return False, None
    win = (min(mins), max(mins))
    reqmin = req.hour * 60 + req.minute
    return (reqmin < win[0] or reqmin > win[1]), win


# ---------- handlers appeles par le chatbot ----------
def consulter(calendar, moment_souhaite: str = "") -> str:
    try:
        slots = list_free_slots(calendar)
    except Exception as e:
        return f"AGENDA INDISPONIBLE: {e}. Ne confirme aucun creneau, propose de revenir vers le prospect."
    if not slots:
        return "AGENDA: aucun creneau libre dans les prochaines semaines."
    req = _parse_moment(moment_souhaite)
    if moment_souhaite:
        m = _match(slots, req)
        if m:
            return f"DISPONIBLE: le creneau '{m['label']}' est LIBRE. Tu peux le reserver (reserver_rendez_vous avec moment={moment_souhaite})."
        alts = _soonest(slots, req, 5)
        hors, win = _hors_horaires(req, slots)
        if hors and win:
            return (f"HORS HORAIRES: nos rendez-vous sont entre {_fmt_hm(win[0])} et {_fmt_hm(win[1])}. "
                    "Le moment demande est en dehors de cette plage : explique-le poliment au prospect "
                    "et propose un creneau dans nos horaires :\n" + _fmt(alts))
        return ("OCCUPE: ce moment est dans nos horaires mais deja pris. Ne le confirme pas. "
                "Creneaux LIBRES a proposer :\n" + _fmt(alts))
    return "Creneaux LIBRES (prochaines disponibilites) :\n" + _fmt(_soonest(slots, None, 6))


def reserver(calendar, settings, profile: str, moment: str, email_invite: str = "") -> str:
    req = _parse_moment(moment)
    if not req:
        return "Format de date non compris. Redemande le jour et l'heure au prospect."
    try:
        slots = list_free_slots(calendar)
    except Exception as e:
        return f"AGENDA INDISPONIBLE: {e}. Reessaie plus tard, ne confirme pas."
    m = _match(slots, req)
    if not m:
        alts = _soonest(slots, req, 5)
        hors, win = _hors_horaires(req, slots)
        if hors and win:
            return (f"NON RESERVE (HORS HORAIRES): nos rendez-vous sont entre {_fmt_hm(win[0])} et {_fmt_hm(win[1])}. "
                    "Explique-le au prospect et propose dans cette plage :\n" + _fmt(alts))
        return ("NON RESERVE: ce creneau n'est pas (ou plus) libre. Propose plutot :\n" + _fmt(alts))
    email = (email_invite or "").strip() or settings.owner_email
    prospect = {"id": "chat_pilote", "full_name": "Prospect (test chatbot)"}
    try:
        booking = calendar.book(prospect, m["id"], email)
    except Exception as e:
        return f"ECHEC RESERVATION: {e}. Propose un autre creneau au prospect."
    link = booking.get("link", "")
    return (f"RESERVE pour de vrai: {m['label']}. Invitation envoyee a {email}. "
            + (f"Lien: {link}. " if link else "") + "Confirme chaleureusement au prospect.")


def _booking_at(calendar, email: str, moment: str):
    """Retrouve LE RDV existant dont le debut correspond a `moment` (heure de Paris), parmi
    les RDV a venir de l'invite. Indispensable quand plusieurs RDV partagent le meme email
    (ex. tests sous l'email du compte) : sinon on deplacerait le mauvais (le plus proche)."""
    req = _parse_moment(moment)
    lister = getattr(calendar, "upcoming_bookings", None)
    if not (req and callable(lister)):
        return None
    try:
        for b in lister(email) or []:
            start = b.get("start", "")
            try:
                d = _slot_paris({"start": start})
            except Exception:
                continue
            if (d.year, d.month, d.day, d.hour, d.minute) == (req.year, req.month, req.day, req.hour, req.minute):
                return b
    except Exception:
        return None
    return None


def deplacer(calendar, settings, profile: str, nouveau_moment: str, email_invite: str = "",
             ancien_moment: str = "") -> str:
    """Deplace un RDV deja reserve vers un nouveau creneau (l'ancien est annule)."""
    finder = getattr(calendar, "find_booking", None)
    resched = getattr(calendar, "reschedule", None)
    if not (callable(finder) and callable(resched)):
        return "Le deplacement de RDV n'est pas disponible sur cet agenda."
    req = _parse_moment(nouveau_moment)
    if not req:
        return "Nouvel horaire non compris. Redemande le jour et l'heure au prospect."
    email = (email_invite or "").strip() or settings.owner_email
    try:
        # On cible EN PRIORITE le RDV qui est a l'heure actuelle indiquee (ancien_moment) ;
        # a defaut seulement, le prochain RDV de l'invite (comportement historique).
        existing = _booking_at(calendar, email, ancien_moment) or finder(email) or {}
    except Exception as e:
        return f"AGENDA INDISPONIBLE: {e}. Reessaie plus tard."
    if not existing.get("uid"):
        return ("Aucun RDV existant trouve a deplacer pour cet email. Verifie l'email du prospect, "
                "ou propose simplement d'en reserver un nouveau.")
    try:
        slots = list_free_slots(calendar)
    except Exception as e:
        return f"AGENDA INDISPONIBLE: {e}. Reessaie plus tard."
    m = _match(slots, req)
    if not m:
        alts = _soonest(slots, req, 5)
        return ("NON DEPLACE: le nouveau creneau n'est pas libre. Propose plutot :\n" + _fmt(alts))
    try:
        booking = resched(existing["uid"], m["id"], "Demande du prospect")
    except Exception as e:
        return f"ECHEC DEPLACEMENT: {e}. Propose un autre creneau au prospect."
    link = booking.get("link", "")
    return (f"DEPLACE: l'ancien creneau est annule, le RDV est maintenant {m['label']}. "
            + (f"Lien: {link}. " if link else "") + "Confirme le nouvel horaire au prospect.")
