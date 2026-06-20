"""Interrupteur de panne pour la demo ("grain de sable").

Permet de COUPER un outil en direct, sans toucher au code : il suffit
d'editer le fichier chaos.json a la racine, par ex. :

    {"calendar": "down"}

Les outils (mock comme live) consultent ce fichier avant chaque appel
et levent ToolUnavailable si l'outil est marque "down". On remet "ok"
(ou on supprime le fichier) pour retablir -> on observe l'agent se
remettre tout seul. Cles attendues : "linkedin", "calendar", "crm".
"""
from __future__ import annotations

import json
import os

CHAOS_FILE = os.getenv("CHAOS_FILE", "chaos.json")


def tool_is_down(tool: str) -> bool:
    try:
        with open(CHAOS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get(tool, "ok")).lower() == "down"
    except Exception:
        return False
