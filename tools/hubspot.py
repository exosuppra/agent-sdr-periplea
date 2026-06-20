"""Adaptateur CRM REEL via HubSpot (mode live, tier gratuit).

- Base URL : https://api.hubapi.com
- Auth : header `Authorization: Bearer <token d'app privee>`.
- Contacts : POST /crm/v3/objects/contacts
- Notes (trace conversation) : POST /crm/v3/objects/notes (associationTypeId 202 -> contact)
- Deals (RDV pris) : POST /crm/v3/objects/deals (associationTypeId 3 -> contact)
- Base clients : recherche POST /crm/v3/objects/contacts/search (lifecyclestage=customer)

Garde une correspondance prospect_id -> contactId dans hubspot_map.json.
Scopes requis sur l'app privee : crm.objects.contacts.read/write,
crm.objects.deals.read/write.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

from chaos import tool_is_down
from .base import ToolUnavailable, RateLimited, ToolError

BASE = "https://api.hubapi.com"
MAP_FILE = "hubspot_map.json"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class HubSpotCrm:
    def __init__(self, settings):
        if not settings.hubspot_token:
            raise ToolError("HUBSPOT_TOKEN manquant.")
        self.h = {"Authorization": f"Bearer {settings.hubspot_token}",
                  "Content-Type": "application/json"}
        self.map = json.load(open(MAP_FILE, encoding="utf-8")) if os.path.exists(MAP_FILE) else {}
        # On NE reutilise PAS les ids d'entreprises memorises lors d'une session passee :
        # une fiche entreprise a pu etre archivee ou supprimee depuis (corbeille HubSpot),
        # et on relierait alors un nouveau contact a une fiche archivee. On les re-resout
        # en direct a chaque run (la recherche HubSpot exclut les archivees).
        self.map.pop("_companies", None)
        self.owner_email = getattr(settings, "owner_email", "") or ""
        self._owner = "?"        # "?" = pas encore recupere ; sinon id owner ou None
        self._lead_opts = None   # valeurs valides de hs_lead_status (cache)
        self._props_ready = False
        self._custom_ok = False   # proprietes custom dispo (scope schema accorde)
        self._companies_ok = True  # scope companies accorde (passe a False sur 403)

    def _save(self) -> None:
        json.dump(self.map, open(MAP_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def _check(self) -> None:
        if tool_is_down("crm"):
            raise ToolUnavailable("CRM (HubSpot) coupe via chaos.json")

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = requests.post(BASE + path, headers=self.h, json=body, timeout=20)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau HubSpot : {e}")
        if r.status_code == 429:
            raise RateLimited("HubSpot : limite de debit (429)")
        if r.status_code >= 500:
            raise ToolUnavailable(f"HubSpot indisponible (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ToolError(f"HubSpot HTTP {r.status_code} : {r.text[:200]}")
        return r.json() if r.content else {}

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        """Appel best-effort (GET/PATCH/PUT) qui ne leve pas : renvoie {} en cas d'echec."""
        try:
            r = requests.request(method, BASE + path, headers=self.h, json=body, timeout=20)
            if r.status_code >= 400:
                return {"_error": r.status_code, "_text": r.text[:200]}
            return r.json() if r.content else {}
        except requests.RequestException:
            return {"_error": "network"}

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:
            pass

    # --- enrichissement (tout en best-effort, ne doit jamais casser la prise de RDV) ---
    def _owner_id(self):
        if self._owner != "?":
            return self._owner
        self._owner = None
        data = self._req("GET", "/crm/v3/owners")
        for o in data.get("results", []):
            self._owner = o.get("id")  # 1er owner du portail (le compte du user)
            if (o.get("email") or "").lower() == (self.owner_email or "").lower():
                self._owner = o.get("id")
                break
        return self._owner

    def _lead_status(self, preferred: str):
        """Renvoie une valeur valide de hs_lead_status pour ce portail, ou None."""
        if self._lead_opts is None:
            data = self._req("GET", "/crm/v3/properties/contacts/hs_lead_status")
            self._lead_opts = [o.get("value") for o in data.get("options", [])] or []
        if not self._lead_opts:
            return None
        return preferred if preferred in self._lead_opts else self._lead_opts[0]

    CUSTOM_PROPS = [
        ("mys_type_ecole", "Type d'ecole (MeetYourSchool)", "string", "text"),
        ("mys_taille_ecole", "Taille de l'ecole (etudiants)", "number", "number"),
        ("mys_source_lead", "Source du lead (MeetYourSchool)", "string", "text"),
        ("mys_linkedin", "Profil LinkedIn (MeetYourSchool)", "string", "text"),
        ("mys_email_estime", "Email estime a confirmer (MeetYourSchool)", "string", "text"),
    ]

    def _ensure_custom_props(self) -> None:
        if self._props_ready:
            return
        self._props_ready = True  # on ne tente la creation qu'une fois par run
        self._custom_ok = True
        for name, label, typ, field in self.CUSTOM_PROPS:
            res = self._req("POST", "/crm/v3/properties/contacts",
                            {"name": name, "label": label, "type": typ, "fieldType": field,
                             "groupName": "contactinformation"})
            if isinstance(res, dict) and res.get("_error") == 403:
                self._custom_ok = False  # scope schema absent -> on n'essaiera pas d'ecrire ces champs
                return

    def _ensure_company(self, name: str):
        if not name or not self._companies_ok:
            return None
        companies = self.map.setdefault("_companies", {})
        if name in companies:
            return companies[name]
        found = self._req("POST", "/crm/v3/objects/companies/search",
                          {"filterGroups": [{"filters": [
                              {"propertyName": "name", "operator": "EQ", "value": name}]}],
                           "properties": ["name"], "limit": 1})
        if isinstance(found, dict) and found.get("_error") == 403:
            self._companies_ok = False  # scope companies absent
            return None
        cid = None
        res = found.get("results") if isinstance(found, dict) else None
        if res:
            cid = res[0].get("id")
        if not cid:
            created = self._req("POST", "/crm/v3/objects/companies", {"properties": {"name": name}})
            if isinstance(created, dict) and created.get("_error") == 403:
                self._companies_ok = False
                return None
            cid = created.get("id")
        if cid:
            companies[name] = cid
            self._save()
        return cid

    def _associate_company(self, contact_id: str, company_id: str) -> None:
        if not (contact_id and company_id):
            return
        self._req("PUT", f"/crm/v4/objects/contacts/{contact_id}/associations/default/companies/{company_id}")

    # --- interface CrmPort ---
    def check_existing(self, company: str) -> dict:
        self._check()
        body = {"filterGroups": [{"filters": [
                    {"propertyName": "company", "operator": "EQ", "value": company}]}],
                "properties": ["company", "lifecyclestage"], "limit": 1}
        results = self._post("/crm/v3/objects/contacts/search", body).get("results", [])
        is_client = any(x.get("properties", {}).get("lifecyclestage") == "customer" for x in results)
        return {"is_client": is_client, "found": bool(results)}

    def _find_existing_contact(self, firstname: str, lastname: str, company: str = ""):
        """Cherche un contact DEJA present dans HubSpot (meme prenom + nom, + ecole si connue)
        pour ne pas creer de doublon. Renvoie son id ou None. Scope contacts.read suffit."""
        filters = []
        if firstname:
            filters.append({"propertyName": "firstname", "operator": "EQ", "value": firstname})
        if lastname:
            filters.append({"propertyName": "lastname", "operator": "EQ", "value": lastname})
        if not filters:
            return None
        if company:
            filters.append({"propertyName": "company", "operator": "EQ", "value": company})
        try:
            res = self._post("/crm/v3/objects/contacts/search",
                             {"filterGroups": [{"filters": filters}],
                              "properties": ["firstname", "lastname", "company"], "limit": 1}).get("results", [])
        except Exception:
            return None
        return res[0].get("id") if res else None

    def upsert_contact(self, prospect: dict) -> str:
        self._check()
        if prospect["id"] in self.map and isinstance(self.map[prospect["id"]], str):
            return self.map[prospect["id"]]
        attrs = prospect.get("attributes") or {}
        parts = (prospect.get("full_name") or "").split(" ", 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""
        company = prospect.get("company", "")
        url = attrs.get("profile_url") or prospect.get("profile_url", "")
        # DEDUP HubSpot : si le contact existe DEJA (meme nom + ecole), on le REUTILISE
        # (pas de doublon) et on ne reecrit pas ses champs deja renseignes.
        existing = self._find_existing_contact(firstname, lastname, company)
        if existing:
            self.map[prospect["id"]] = existing
            self._save()
            self._safe(lambda: self.log_note(prospect["id"],
                       "Contact deja present dans HubSpot : reutilise (pas de doublon cree)."))
            return existing
        # 1) Proprietes standard SURES (texte) -> creation du contact, ne doit pas echouer
        props = {"firstname": firstname,
                 "lastname": lastname,
                 "company": company,
                 "jobtitle": prospect.get("headline", ""),
                 "city": attrs.get("region", ""),
                 "email": attrs.get("email", ""),    # si deja capte en conversation
                 "phone": attrs.get("phone", ""),
                 "lifecyclestage": "lead"}
        # NB : on ne met PAS le lien LinkedIn dans `website` -> HubSpot creerait une
        # entreprise parasite a partir du domaine linkedin.com. Le LinkedIn va dans la
        # propriete custom mys_linkedin (ci-dessous) et dans la note.
        props = {k: v for k, v in props.items() if v}
        cid = self._post("/crm/v3/objects/contacts", {"properties": props}).get("id", "")
        self.map[prospect["id"]] = cid
        self._save()
        if not cid:
            return cid
        # 2a) Champs standard (scope contacts) : statut de lead + proprietaire, PATCH isole
        std = {}
        ls = self._lead_status("IN_PROGRESS")
        if ls:
            std["hs_lead_status"] = ls
        owner = self._owner_id()
        if owner:
            std["hubspot_owner_id"] = owner
        if std:
            self._safe(lambda: self._req("PATCH", f"/crm/v3/objects/contacts/{cid}", {"properties": std}))
        # 2b) Champs custom (scope schema) : type/taille ecole, source, LinkedIn, PATCH isole
        self._safe(self._ensure_custom_props)
        if self._custom_ok:
            cust = {}
            if attrs.get("school_type"):
                cust["mys_type_ecole"] = str(attrs["school_type"])
            if attrs.get("student_count"):
                cust["mys_taille_ecole"] = attrs["student_count"]
            if attrs.get("source"):
                cust["mys_source_lead"] = str(attrs["source"])
            if url:
                cust["mys_linkedin"] = url
            if attrs.get("email_estime"):
                cust["mys_email_estime"] = attrs["email_estime"]
            if cust:
                self._safe(lambda: self._req("PATCH", f"/crm/v3/objects/contacts/{cid}", {"properties": cust}))
        # 3) Vraie societe associee -> remplit la colonne "Entreprise principale"
        self._safe(lambda: self._associate_company(cid, self._ensure_company(company)))
        # 4) Note de contexte (URL LinkedIn, intitule, taille/type ecole, source)
        extra = []
        if url:
            extra.append(f"Profil LinkedIn : {url}")
        if prospect.get("headline"):
            extra.append(f"Intitule : {prospect['headline']}")
        if attrs.get("school_type") or attrs.get("student_count"):
            extra.append(f"Ecole : type={attrs.get('school_type','?')}, taille={attrs.get('student_count','?')}")
        if attrs.get("source"):
            extra.append(f"Source : {attrs['source']}")
        if attrs.get("email_estime"):
            extra.append(f"Email ESTIME (non confirme, a verifier avant envoi) : {attrs['email_estime']} "
                         f"(score {attrs.get('email_estime_score', '?')}/100, via Hunter, base legale interet legitime)")
        if extra:
            self._safe(lambda: self.log_note(prospect["id"], " | ".join(extra)))
        return cid

    def set_email(self, prospect_id: str, email: str) -> None:
        """Renseigne l'email du contact quand on le capte en conversation (best-effort)."""
        cid = self.map.get(prospect_id)
        if cid and email:
            self._safe(lambda: self._req("PATCH", f"/crm/v3/objects/contacts/{cid}",
                                         {"properties": {"email": email}}))

    def set_phone(self, prospect_id: str, phone: str) -> None:
        """Renseigne le telephone du contact quand le prospect le partage (best-effort)."""
        cid = self.map.get(prospect_id)
        if cid and phone:
            self._safe(lambda: self._req("PATCH", f"/crm/v3/objects/contacts/{cid}",
                                         {"properties": {"phone": phone}}))

    def log_note(self, prospect_id: str, text: str) -> None:
        self._check()
        cid = self.map.get(prospect_id)
        if not cid:
            return
        body = {"properties": {"hs_note_body": text, "hs_timestamp": _now_ms()},
                "associations": [{"to": {"id": cid},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                             "associationTypeId": 202}]}]}
        self._post("/crm/v3/objects/notes", body)

    def mark_meeting(self, prospect_id: str, details: dict) -> None:
        self._check()
        cid = self.map.get(prospect_id)
        label = (details or {}).get("label", "")
        props = {"dealname": f"RDV MeetYourSchool {label}".strip(),
                 "pipeline": "default", "dealstage": "appointmentscheduled"}
        assoc = ([{"to": {"id": cid},
                   "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]}]
                 if cid else [])
        self._post("/crm/v3/objects/deals", {"properties": props, "associations": assoc})
        # Le contact passe en "opportunite" + statut de lead "RDV pris" (best-effort)
        if cid:
            up = {"lifecyclestage": "opportunity"}
            ls = self._lead_status("OPEN_DEAL")
            if ls:
                up["hs_lead_status"] = ls
            self._safe(lambda: self._req("PATCH", f"/crm/v3/objects/contacts/{cid}", {"properties": up}))
