"""Liste de non-sollicitation (Do Not Contact) PERSISTANTE.

Repond a l'obligation RGPD de droit d'opposition (art. 21) : quand un prospect
demande a ne plus etre contacte, il est ajoute ici. La liste survit a un
`--reset` de la base (fichier separe) et bloque tout futur contact, meme dans
une autre campagne. Cle = identite stable du prospect (URL LinkedIn si dispo,
sinon nom normalise).

Contient des donnees personnelles -> fichier gitignore, jamais commite.
"""
from __future__ import annotations

import json
import os
import re

DNC_FILE = "do_not_contact.json"

# Formulations claires d'opposition (detection deterministe, filet de securite
# au cas ou le LLM passerait a cote).
OPTOUT_PATTERNS = [
    r"ne me contactez plus", r"ne plus me contacter", r"ne me recontact",
    r"ne m'envoyez plus", r"retirez[ -]moi", r"d[eé]sinscri",
    r"ne souhaite plus (?:etre|être) (?:contact|sollicit)",
    r"arr[eê]te[zs]? de me (?:contacter|solliciter|relancer|deranger|d[eé]ranger|ecrire|écrire|harceler)",
    r"supprimez mes (?:donn|coordonn)", r"unsubscribe", r"\bstop\b",
    r"laissez[ -]moi tranquille", r"fou?tez[ -]moi la paix", r"fich(?:ez|e)[ -]moi la paix",
    r"\bdispara(?:is|i|î|isse)", r"je vous bloque", r"c'?est du harc[eè]lement",
    r"ne veux plus (?:de vos messages|etre contact|être contact|vous parler|qu'on me contacte)",
]


def normalize(identity: str) -> str:
    return (identity or "").strip().lower().rstrip("/")


def load() -> dict:
    if os.path.exists(DNC_FILE):
        try:
            return json.load(open(DNC_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def is_blocked(identity: str) -> bool:
    return normalize(identity) in load()


def block(identity: str, full_name: str = "", reason: str = "") -> None:
    key = normalize(identity)
    if not key:
        return
    data = load()
    data[key] = {"full_name": full_name, "reason": reason}
    json.dump(data, open(DNC_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def detect_optout(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in OPTOUT_PATTERNS)


def count() -> int:
    return len(load())
