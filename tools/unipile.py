"""Adaptateur LinkedIn REEL via Unipile (mode live).

Implemente l'interface LinkedInPort avec l'API Unipile v1.
- Auth : header `X-API-KEY` ; base URL = https://{DSN}/api/v1 ; account_id en query.
- Les actions passent par la session LinkedIn authentifiee de TON compte
  (pas de scraping cloud) -> c'est le choix defendable cote conformite.
- L'adaptateur n'a pas acces a la base de l'agent : il garde donc sa propre
  table de correspondance prospect_id <-> provider_id / chat_id dans un petit
  fichier json local (unipile_map.json).

A FINALISER au branchement (1er compte Unipile reel) : les noms de champs
exacts renvoyes par /linkedin/search varient selon le type de compte
(classic vs Sales Navigator) -> ajuster le mapping dans search_prospects.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone

import requests

from chaos import tool_is_down
from .base import ToolUnavailable, RateLimited, ToolError

MAP_FILE = "unipile_map.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnipileLinkedIn:
    def __init__(self, settings):
        if not (settings.unipile_api_key and settings.unipile_dsn and settings.unipile_account_id):
            raise ToolError("Config Unipile incomplete (UNIPILE_API_KEY / UNIPILE_DSN / UNIPILE_ACCOUNT_ID).")
        self.base = f"https://{settings.unipile_dsn}/api/v1"
        self.account_id = settings.unipile_account_id
        self.headers = {"X-API-KEY": settings.unipile_api_key, "accept": "application/json"}
        self.cfg = settings
        self.map = self._load_map()
        self.last_poll: str | None = None

    # --- utilitaires ---
    def _load_map(self) -> dict:
        if os.path.exists(MAP_FILE):
            try:
                return json.load(open(MAP_FILE, encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_map(self) -> None:
        json.dump(self.map, open(MAP_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def _check(self) -> None:
        if tool_is_down("linkedin"):
            raise ToolUnavailable("LinkedIn (Unipile) coupe via chaos.json")

    def _pace(self) -> None:
        # timing humain : on espace les actions d'envoi (anti-detection)
        time.sleep(random.uniform(self.cfg.live_min_action_gap_sec,
                                  self.cfg.live_max_action_gap_sec))

    def _req(self, method: str, path: str, **kw):
        try:
            r = requests.request(method, self.base + path, headers=self.headers, timeout=30, **kw)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Unipile : {e}")
        if r.status_code == 429:
            raise RateLimited("Unipile/LinkedIn : limite de debit (429)")
        if r.status_code >= 500:
            raise ToolUnavailable(f"Unipile indisponible (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ToolError(f"Unipile HTTP {r.status_code} : {r.text[:200]}")
        return r.json() if r.content else {}

    def _provider_id(self, prospect_id: str):
        """provider_id LinkedIn RESOLU par la recherche authentifiee Unipile, ou None.
        On NE devine PAS un id a partir du nom/id interne (sinon risque d'envoyer au
        mauvais homonyme) : pas de provider_id resolu => pas d'envoi (cf. _require_provider_id)."""
        return (self.map.get(prospect_id) or {}).get("provider_id")

    def _require_provider_id(self, prospect_id: str) -> str:
        prov = self._provider_id(prospect_id)
        if not prov:
            raise ToolError(
                "Envoi refuse : aucun provider_id LinkedIn resolu pour ce prospect. "
                "Regle anti-erreur : on n'envoie jamais sur la base d'un nom ou d'un id devine "
                "(risque d'homonyme). Resous d'abord le profil via la recherche authentifiee Unipile.")
        return prov

    def _prospect_for_chat(self, chat_id: str) -> str | None:
        for pid, info in self.map.items():
            if info.get("chat_id") == chat_id:
                return pid
        return None

    # --- interface LinkedInPort ---
    def search_prospects(self, criteria: dict, limit: int = 10) -> list[dict]:
        self._check()
        keywords = " ".join(criteria.get("role_keywords", [])) or criteria.get("focus", "")
        body = {"api": "classic", "category": "people", "keywords": keywords}
        data = self._req("POST", f"/linkedin/search?account_id={self.account_id}", json=body)
        items = data.get("items") or data.get("data") or []
        out = []
        for it in items[:limit]:
            prov = it.get("provider_id") or it.get("id") or it.get("public_identifier")
            internal = "li_" + str(prov)
            url = it.get("public_profile_url") or it.get("profile_url", "")
            self.map.setdefault(internal, {}).update({"provider_id": prov, "profile_url": url})
            name = it.get("name") or (str(it.get("first_name", "")) + " " + str(it.get("last_name", ""))).strip()
            out.append({
                "id": internal, "full_name": name, "headline": it.get("headline", ""),
                "company": it.get("current_company") or it.get("company", ""),
                "profile_url": url,
                "attributes": {"provider_id": prov, "location": it.get("location", "")},
            })
        self._save_map()
        return out

    def get_profile(self, prospect_id: str) -> dict:
        self._check()
        data = self._req("GET", f"/users/{self._require_provider_id(prospect_id)}?account_id={self.account_id}")
        return {
            "id": prospect_id, "full_name": data.get("name", ""),
            "headline": data.get("headline", ""),
            "company": data.get("current_company", ""),
            "attributes": {"location": data.get("location", ""), "raw": data},
        }

    def send_invitation(self, prospect_id: str, note: str) -> dict:
        self._check()
        prov = self._require_provider_id(prospect_id)  # pas de provider_id resolu => pas d'envoi
        self._pace()
        self._req("POST", "/users/invite", json={
            "account_id": self.account_id,
            "provider_id": prov,
            "message": note[:300],
        })
        return {"ok": True, "channel": "invite"}

    def send_message(self, prospect_id: str, text: str, is_followup: bool = False) -> dict:
        self._check()
        info = self.map.setdefault(prospect_id, {})
        chat_id = info.get("chat_id")
        if chat_id:
            self._pace()
            self._req("POST", f"/chats/{chat_id}/messages", data={"text": text})
        else:
            prov = self._require_provider_id(prospect_id)  # nouvelle conversation : id resolu obligatoire
            self._pace()
            res = self._req("POST", "/chats", data={
                "account_id": self.account_id,
                "attendees_ids": prov,
                "text": text,
            })
            info["chat_id"] = res.get("chat_id") or (res.get("data") or {}).get("id")
            self._save_map()
        return {"ok": True, "channel": "message"}

    def fetch_new_replies(self) -> list[dict]:
        self._check()
        path = f"/messages?account_id={self.account_id}"
        if self.last_poll:
            path += f"&after={self.last_poll}"
        data = self._req("GET", path)
        msgs = data.get("items") or data.get("data") or []
        replies = []
        for m in msgs:
            if m.get("is_sender") or m.get("from_me"):
                continue
            pid = self._prospect_for_chat(m.get("chat_id"))
            if pid:
                replies.append({"prospect_id": pid, "text": m.get("text", "")})
        self.last_poll = _now_iso()
        return replies
