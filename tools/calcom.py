"""Adaptateur agenda REEL via Cal.com API v2 (mode live).

- Base URL : https://api.cal.com/v2
- Auth : header `Authorization: Bearer <cal_live_...>` + header obligatoire
  `cal-api-version` (valeur DIFFERENTE par endpoint : slots=2024-09-04,
  bookings=2026-02-25).
- Flux : GET /v2/slots pour lister les creneaux, POST /v2/bookings pour reserver.

NB conformite : pour reserver AU NOM du prospect, Cal.com exige l'email de
l'invite. En live, l'agent demande l'email dans la conversation avant de
booker (ou envoie le lien public de l'event-type et capte le webhook). Ici
`attendee_email` est passe par l'orchestrateur.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from chaos import tool_is_down
from .base import ToolUnavailable, RateLimited, ToolError
from .chat_booking import human_label

BASE = "https://api.cal.com/v2"
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class CalComCalendar:
    def __init__(self, settings):
        if not (settings.calcom_api_key and settings.calcom_event_type_id):
            raise ToolError("Config Cal.com incomplete (CALCOM_API_KEY / CALCOM_EVENT_TYPE_ID).")
        self.key = settings.calcom_api_key
        self.event_type_id = int(settings.calcom_event_type_id)
        self.owner_email = settings.owner_email
        self.base = (settings.calcom_base or BASE).rstrip("/")  # api.cal.com ou api.cal.eu

    def _check(self) -> None:
        if tool_is_down("calendar"):
            raise ToolUnavailable("Agenda (Cal.com) coupe via chaos.json")

    def _headers(self, version: str) -> dict:
        return {"Authorization": f"Bearer {self.key}",
                "cal-api-version": version,
                "Content-Type": "application/json"}

    def _handle(self, r):
        if r.status_code == 429:
            raise RateLimited("Cal.com : limite de debit (429)")
        if r.status_code >= 500:
            raise ToolUnavailable(f"Cal.com indisponible (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ToolError(f"Cal.com HTTP {r.status_code} : {r.text[:200]}")
        return r.json()

    def get_slots(self, within_days: int = 14, limit: int = 5) -> list[dict]:
        self._check()
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=within_days)
        params = {"eventTypeId": self.event_type_id,
                  "start": start.isoformat(), "end": end.isoformat(),
                  "timeZone": "Europe/Paris"}
        try:
            r = requests.get(f"{self.base}/slots", headers=self._headers("2024-09-04"),
                             params=params, timeout=20)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Cal.com : {e}")
        data = self._handle(r).get("data", {})
        slots = []
        for _day, times in sorted(data.items()):
            for slot in times:
                iso = slot.get("start") if isinstance(slot, dict) else slot
                if not iso:
                    continue
                slots.append({"id": iso, "start": iso, "label": human_label(iso)})
                if len(slots) >= limit:
                    return slots
        return slots

    def book(self, prospect: dict, slot_id: str, attendee_email: str) -> dict:
        self._check()
        body = {
            "start": slot_id,  # l'id du creneau EST son heure ISO (UTC)
            "eventTypeId": self.event_type_id,
            "attendee": {"name": prospect.get("full_name", "Prospect"),
                         "email": attendee_email or self.owner_email,
                         "timeZone": "Europe/Paris"},
        }
        try:
            r = requests.post(f"{self.base}/bookings", headers=self._headers("2026-02-25"),
                              json=body, timeout=25)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Cal.com : {e}")
        d = self._handle(r).get("data", {})
        uid = d.get("uid", "")
        return {"booking_id": uid,
                "link": f"https://app.cal.com/booking/{uid}",
                "start": d.get("start", slot_id),
                "label": human_label(slot_id)}

    def find_booking(self, attendee_email: str = "") -> dict:
        """Retrouve le prochain RDV a venir (filtre par email d'invite si fourni)."""
        self._check()
        params = {"status": "upcoming", "sortStart": "asc", "take": 5}
        if attendee_email:
            params["attendeeEmail"] = attendee_email
        try:
            r = requests.get(f"{self.base}/bookings", headers=self._headers("2024-08-13"),
                             params=params, timeout=20)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Cal.com : {e}")
        items = self._handle(r).get("data", []) or []
        if not items:
            return {}
        b = items[0]
        return {"uid": b.get("uid", ""), "start": b.get("start", "")}

    def reschedule(self, booking_uid: str, new_slot_id: str, reason: str = "") -> dict:
        """Deplace un RDV vers un nouveau creneau. Cal.com annule l'ancien automatiquement."""
        self._check()
        body = {"start": new_slot_id}
        if reason:
            body["reschedulingReason"] = reason
        try:
            r = requests.post(f"{self.base}/bookings/{booking_uid}/reschedule",
                              headers=self._headers("2024-08-13"), json=body, timeout=25)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Cal.com : {e}")
        d = self._handle(r).get("data", {})
        uid = d.get("uid", booking_uid)
        return {"booking_id": uid, "link": f"https://app.cal.com/booking/{uid}",
                "start": d.get("start", new_slot_id), "label": human_label(new_slot_id)}

    def cancel(self, booking_uid: str, reason: str = "") -> dict:
        self._check()
        body = {"cancellationReason": reason} if reason else {}
        try:
            r = requests.post(f"{self.base}/bookings/{booking_uid}/cancel",
                              headers=self._headers("2024-08-13"), json=body, timeout=25)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Cal.com : {e}")
        self._handle(r)
        return {"ok": True, "uid": booking_uid}
