"""Prospect PILOTE : un humain joue un prospect en direct DANS la campagne.

L'agent traite ce prospect comme n'importe quel autre (il l'invite, discute,
propose un RDV, reserve), SAUF que ses reponses viennent d'un humain en temps
reel (via le cockpit), pas d'un script. Sert a tester les capacites de chatbot
de l'agent face a un interlocuteur imprevisible, jusqu'a la prise de RDV reelle.

Etat partage (thread-safe) entre le thread de la campagne et le serveur web.
"""
from __future__ import annotations

import threading

PILOT_ID = "li_pilot"

_lock = threading.Lock()
_state = {"active": False, "profile": {}, "in_replies": [], "transcript": []}


def pilot_reset(profile: dict | None = None) -> None:
    with _lock:
        _state["active"] = bool(profile)
        _state["profile"] = profile or {}
        _state["in_replies"] = []
        _state["transcript"] = []


def pilot_inject(text: str) -> None:
    """L'humain (prospect) ecrit un message."""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _state["in_replies"].append(text)
        _state["transcript"].append({"who": "prospect", "text": text})


def pilot_active() -> bool:
    with _lock:
        return _state["active"]


def pilot_profile() -> dict:
    with _lock:
        return dict(_state["profile"])


def pilot_transcript() -> list:
    with _lock:
        return list(_state["transcript"])


def pilot_has_reply() -> bool:
    with _lock:
        return len(_state["in_replies"]) > 0


def _record_out(text: str) -> None:
    with _lock:
        _state["transcript"].append({"who": "agent", "text": text})


def _drain() -> list:
    with _lock:
        r = _state["in_replies"]
        _state["in_replies"] = []
        return r


class PilotedLinkedIn:
    """Enrobe un adaptateur LinkedIn : delegue tout au 'base', SAUF le prospect
    pilote (li_pilot) dont la conversation est pilotee par un humain en direct."""

    def __init__(self, base):
        self.base = base
        self.pilot_id = PILOT_ID

    def set_tick(self, t):
        if hasattr(self.base, "set_tick"):
            self.base.set_tick(t)

    def search_prospects(self, criteria, limit=10):
        res = self.base.search_prospects(criteria, max(1, limit - 1))
        p = pilot_profile()
        pilot = {"id": self.pilot_id,
                 "full_name": p.get("full_name", "Vous (test)"),
                 "headline": p.get("headline", "Prospect pilote"),
                 "company": p.get("company", ""), "profile_url": "",
                 "attributes": {"source": "prospect pilote (humain en direct)", "piloted": True}}
        return [pilot] + res

    def get_profile(self, prospect_id):
        if prospect_id == self.pilot_id:
            p = pilot_profile()
            return {"id": prospect_id, "full_name": p.get("full_name", ""),
                    "headline": p.get("headline", ""), "company": p.get("company", ""),
                    "attributes": {"piloted": True}}
        return self.base.get_profile(prospect_id)

    def send_invitation(self, prospect_id, note):
        if prospect_id == self.pilot_id:
            _record_out(note)
            return {"ok": True, "channel": "invite", "piloted": True}
        return self.base.send_invitation(prospect_id, note)

    def send_message(self, prospect_id, text, is_followup=False):
        if prospect_id == self.pilot_id:
            _record_out(text)
            return {"ok": True, "channel": "message", "piloted": True}
        return self.base.send_message(prospect_id, text, is_followup)

    def fetch_new_replies(self):
        replies = self.base.fetch_new_replies()
        for t in _drain():
            replies.append({"prospect_id": self.pilot_id, "text": t})
        return replies
