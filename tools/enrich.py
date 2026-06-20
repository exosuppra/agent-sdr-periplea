"""Enrichissement email OPTIONNEL : devine l'email pro a partir du nom + ecole.

Fournisseur : Hunter.io (endpoint email-finder, SYNCHRONE, accepte le NOM
d'entreprise donc pas besoin du domaine). Cle dans .env (HUNTER_API_KEY).
SANS cle -> desactive (no-op), aucun credit consomme.

RGPD : l'email DEVINE est une donnee personnelle. Il est marque comme ESTIME
(jamais traite comme un email confirme/fourni), avec son score de confiance et
sa source. A confirmer avant tout envoi de masse. cf. NOTE_ARCHITECTURE.md.
"""
from __future__ import annotations

import requests

HUNTER_URL = "https://api.hunter.io/v2/email-finder"


class EmailFinder:
    def __init__(self, settings):
        self.key = getattr(settings, "hunter_api_key", "") or ""

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def find(self, first_name: str, last_name: str, company: str) -> dict:
        """Renvoie {'email': str, 'score': int} si trouve, sinon {} (best-effort, ne leve jamais)."""
        if not self.key or not company or not (first_name or last_name):
            return {}
        params = {"company": company, "first_name": first_name, "last_name": last_name,
                  "api_key": self.key, "max_duration": 10}
        try:
            r = requests.get(HUNTER_URL, params=params, timeout=20)
            if r.status_code >= 400:
                return {}
            data = (r.json() or {}).get("data") or {}
        except Exception:
            return {}
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> dict:
        email = data.get("email")
        if not email:
            return {}
        try:
            score = int(data.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        return {"email": email, "score": score}
