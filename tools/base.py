"""Interfaces (ports) des outils que l'agent pilote.

Le cerveau ne connait QUE ces interfaces. On branche dessous soit des
outils simules (mock.py), soit les vrais services (unipile.py, calcom.py,
hubspot.py) - sans jamais toucher au cerveau.
"""
from __future__ import annotations

from typing import Protocol


class ToolError(Exception):
    """Erreur generique d'un outil."""


class ToolUnavailable(ToolError):
    """Outil indisponible (panne reelle ou coupure volontaire en demo)."""


class RateLimited(ToolError):
    """Limite de debit atteinte (ex. LinkedIn) -> reessayer plus tard."""


class LinkedInPort(Protocol):
    def search_prospects(self, criteria: dict, limit: int = 10) -> list[dict]: ...
    def get_profile(self, prospect_id: str) -> dict: ...
    def send_invitation(self, prospect_id: str, note: str) -> dict: ...
    def send_message(self, prospect_id: str, text: str) -> dict: ...
    def fetch_new_replies(self) -> list[dict]: ...


class CalendarPort(Protocol):
    def get_slots(self, within_days: int = 14, limit: int = 5) -> list[dict]: ...
    def book(self, prospect: dict, slot_id: str, attendee_email: str) -> dict: ...


class CrmPort(Protocol):
    def upsert_contact(self, prospect: dict) -> str: ...
    def log_note(self, prospect_id: str, text: str) -> None: ...
    def mark_meeting(self, prospect_id: str, details: dict) -> None: ...
