"""Adaptateur LinkedIn HYBRIDE : recherche REELLE via Apify, conversation SIMULEE.

- search_prospects : appelle un Actor Apify pour ramener de VRAIS profils
  LinkedIn publics correspondant a l'ICP (titre + region + ecole), SANS cookie
  ni compte LinkedIn (donc zero risque pour le compte du user). Essaie d'abord
  harvestapi/linkedin-profile-search (donnees structurees) ; en cas d'echec,
  bascule sur apify/google-search-scraper (requetes 'site:linkedin.com/in ...').
- get_profile : renvoie les attributs deja recuperes.
- send_invitation / send_message : SIMULES (rien n'est envoye a de vraies
  personnes) et programment une reponse simulee.
- fetch_new_replies : delivre les reponses simulees (meme mecanique que le bac
  a sable). => "il cherche de vrais gens, puis simule les conversations".
"""
from __future__ import annotations

import copy
import re
import unicodedata

import requests

from chaos import tool_is_down
from .base import ToolUnavailable, ToolError

HARVEST = "harvestapi~linkedin-profile-search"
HARVEST_PROFILE = "harvestapi~linkedin-profile-scraper"   # scrape d'un profil precis : entreprise + poste + email reels
PROFILE_MODE = "Profile details + email search ($10 per 1k)"   # ~0,01 $ par profil, AVEC l'email pro verifie
GOOGLE = "apify~google-search-scraper"
API = "https://api.apify.com/v2/acts"


def _slug(url: str) -> str:
    """Identifiant public d'un profil LinkedIn (la partie apres /in/)."""
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1).lower().rstrip("/") if m else ""

# Conversations simulees (variees) appliquees aux vrais prospects trouves.
SCRIPTS = [
    [  # chaleureux -> RDV
        {"after_out": 1, "delay": 1, "text": "Bonjour, merci pour votre message. Concretement, qu'est-ce que MeetYourSchool apporterait a notre service ?"},
        {"after_out": 2, "delay": 1, "text": "Interessant. Je veux bien un echange rapide pour en savoir plus."},
        {"after_out": 3, "delay": 1, "text": "Le premier creneau me convient parfaitement."},
    ],
    [  # objection puis -> RDV
        {"after_out": 1, "delay": 1, "text": "On a deja plusieurs outils en place. Qu'apportez-vous de plus ?"},
        {"after_out": 2, "delay": 1, "text": "D'accord, ca vaut le coup d'en parler. Ok pour un point."},
        {"after_out": 3, "delay": 1, "text": "Le deuxieme creneau propose me convient."},
    ],
    [  # repond en retard puis -> RDV
        {"after_out": 1, "delay": 3, "text": "Desole pour le delai. Oui, le sujet nous concerne, je suis preneur."},
        {"after_out": 2, "delay": 1, "text": "Volontiers pour un echange de 30 minutes."},
        {"after_out": 3, "delay": 1, "text": "Le dernier creneau propose me convient."},
    ],
    [],  # ne repond jamais -> relances puis abandon
]

# Conversation d'un contact RECOMMANDE (lead chaud : il repond et prend RDV).
WARM_REFERRAL = [
    {"after_out": 1, "delay": 1, "text": "Bonjour, merci pour votre message. Oui, c'est bien moi qui gere ce sujet ici. De quoi s'agit-il ?"},
    {"after_out": 2, "delay": 1, "text": "Tres bien, ca m'interesse. Un echange de 30 minutes me convient."},
    {"after_out": 3, "delay": 1, "text": "Le premier creneau propose me convient."},
]


def _txt(v) -> str:
    return v if isinstance(v, str) else ""


# Detecte un intitule de poste (a ne pas confondre avec un nom d'ecole).
_ROLE_RE = re.compile(
    r"^(responsable|directeur|directrice|charg|chef|manager|head|consultant|adjoint|"
    r"assistant|coordinat|gestionnaire|conseill|attach|r[eé]f[eé]rent|animat|d[eé]l[eé]gu|"
    r"president|pr[eé]sident|fondat|founder|ceo|cto|cmo|chro|dg\b|drh\b)", re.I)


def _looks_like_role(s: str) -> bool:
    return bool(_ROLE_RE.match((s or "").strip()))


# Extraction du nom d'ETABLISSEMENT depuis la description d'un resultat (snippet
# LinkedIn), par ordre de fiabilite : structure "Experience : <Org>", puis "chez/au
# sein de <Org>", puis "a l'<Org>". On ne garde JAMAIS un intitule de poste.
_EXP_RE = re.compile(r"exp.rience\s*:\s*([^·|\n]+)", re.I)
_CHEZ_RE = re.compile(r"\b(?:chez|au sein d[eu’'][^A-Za-z0-9]*l?[’']?)\s*([^,.·|\n]+)", re.I)
_ALECOLE_RE = re.compile(r"\b[aà]\s+l[’']\s*([A-ZÉÈ][^,.·|\n]+)")
# mots de SERVICE/DEPARTEMENT (jamais dans un nom d'ecole) : si le candidat en contient un,
# c'est un intitule de poste/service, pas un etablissement.
_DEPT_RE = re.compile(r"admission|promotion|recrutement|concours|candidat|scolarit|career|d[eé]veloppement", re.I)


def _extract_company(parts, desc: str) -> str:
    desc = desc or ""
    cand = ""
    for rx in (_EXP_RE, _CHEZ_RE, _ALECOLE_RE):
        m = rx.search(desc)
        if m:
            cand = m.group(1).strip()
            break
    if not cand and len(parts) > 1 and not _looks_like_role(parts[-1]):
        cand = parts[-1]  # dernier segment du titre, s'il n'est pas un intitule de poste
    # coupe a la 1re separation (·, |, ellipse, fin de phrase, virgule) ; garde les " - "
    cand = re.split(r"\s*·\s*|\s*\|\s*|…|\.\.\.|\.\s|,\s", cand)[0].strip(" -")
    if _looks_like_role(cand) or _DEPT_RE.search(cand):
        cand = ""  # garde-fou : jamais un intitule de poste/service comme etablissement
    return cand[:80]


class ApifyLinkedIn:
    def __init__(self, settings):
        if not settings.apify_token:
            raise ToolError("APIFY_TOKEN manquant (recherche reelle Apify).")
        self.token = settings.apify_token
        self.current_tick = 0
        self.reply_queue: list[dict] = []
        self.stage: dict[str, int] = {}
        self.scripts: dict[str, list] = {}
        self.directory: dict[str, dict] = {}
        self.returned: set[str] = set()  # profils deja remontes (evite de reboucler sur les memes)

    def set_tick(self, t: int) -> None:
        self.current_tick = t

    def _check(self) -> None:
        if tool_is_down("linkedin"):
            raise ToolUnavailable("Recherche LinkedIn (Apify) coupe via chaos.json")

    def _run_actor(self, actor: str, body: dict) -> list:
        url = f"{API}/{actor}/run-sync-get-dataset-items?token={self.token}"
        try:
            r = requests.post(url, json=body, timeout=180)
        except requests.RequestException as e:
            raise ToolUnavailable(f"reseau Apify : {e}")
        if r.status_code == 429:
            raise ToolUnavailable("Apify : limite de debit")
        if r.status_code >= 400:
            raise ToolError(f"Apify {actor} HTTP {r.status_code} : {r.text[:200]}")
        return r.json() if r.content else []

    # --- recherche reelle ---
    def search_prospects(self, criteria: dict, limit: int = 10) -> list[dict]:
        self._check()
        kw = criteria.get("role_keywords", []) or []
        locations = criteria.get("locations", []) or []
        focus = criteria.get("focus", "")
        titles = kw[:4] or ["directeur"]
        # 1) Voie principale : Google Search Scraper avec un OU entre les titres
        #    (fiable, plan gratuit ; le ET donne 0 resultat car personne n'a 2 postes).
        or_part = "(" + " OR ".join(f'"{t}"' for t in titles) + ")"
        q = f"site:linkedin.com/in {or_part}"
        if focus:
            q += f" {focus}"
        for loc in locations[:1]:
            q += f" {loc}"
        body = {"queries": q, "maxPagesPerQuery": 2, "resultsPerPage": max(limit, 10),
                "countryCode": "fr", "languageCode": "fr"}
        out = self._parse_google(self._run_actor(GOOGLE, body), limit)
        if out:
            out = self._enrich_profiles(out)  # entreprise + poste REELS lus sur la fiche
            return self._register(out)
        # 2) Fallback : harvestapi avec UN seul titre (donnees structurees)
        try:
            body = {"searchQuery": (titles[0] + " " + focus).strip(),
                    "currentJobTitles": [titles[0]], "locations": locations,
                    "profileScraperMode": "Short", "maxItems": limit}
            out = self._parse_harvest(self._run_actor(HARVEST, body), limit)
        except ToolError:
            out = []
        return self._register(out)

    def _enrich_profiles(self, found: list[dict]) -> list[dict]:
        """Lit la VRAIE fiche de chaque profil (harvestapi, en un seul appel) pour en
        tirer l'entreprise et le poste reels, propres et separes. Best-effort : si le
        scrape echoue, on garde ce que l'extrait de recherche avait donne."""
        urls = [f.get("profile_url") for f in found if f.get("profile_url")]
        if not urls:
            return found
        try:
            items = self._run_actor(HARVEST_PROFILE, {"queries": urls, "profileScraperMode": PROFILE_MODE})
        except Exception:
            return found
        by_slug = {}
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            slug = _txt(it.get("publicIdentifier")).lower() or _slug(_txt(it.get("linkedinUrl")))
            if slug:
                by_slug[slug] = it
        for f in found:
            it = by_slug.get(_slug(f.get("profile_url", "")))
            if not it:
                continue
            cands = []
            cp = it.get("currentPosition")
            if isinstance(cp, dict):
                cands.append(cp)
            exp = it.get("experience")
            if isinstance(exp, list) and exp and isinstance(exp[0], dict):
                cands.append(exp[0])
            company = title = ""
            for c in cands:
                company = company or _txt(c.get("companyName"))
                title = title or _txt(c.get("position"))
            title = title or _txt(it.get("headline"))
            if company:
                f["company"] = company.strip()[:80]
            if title:
                f["headline"] = title.strip()[:120]   # le METIER reel (-> jobtitle HubSpot)
                f["job_title"] = title.strip()[:120]
            # email pro REEL et verifie (mode email search) : on prend le 1er deliverable
            emails = it.get("emails") if isinstance(it.get("emails"), list) else []
            best = next((e for e in emails if isinstance(e, dict) and e.get("deliverable")),
                        emails[0] if emails else None)
            if isinstance(best, dict) and _txt(best.get("email")):
                f["email"] = best["email"].strip()
        return found

    def _parse_harvest(self, items, limit) -> list[dict]:
        out = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            name = (_txt(it.get("name")) or _txt(it.get("fullName"))
                    or (_txt(it.get("firstName")) + " " + _txt(it.get("lastName"))).strip())
            positions = it.get("currentPositions") or it.get("currentPosition") or it.get("positions") or []
            if isinstance(positions, dict):
                positions = [positions]
            pos = positions[0] if positions else {}
            title = _txt(pos.get("title")) or _txt(pos.get("position")) or _txt(pos.get("role"))
            company = (_txt(pos.get("companyName")) or _txt(pos.get("company"))
                       or _txt(pos.get("organisation")) or _txt(it.get("companyName")))
            headline = _txt(it.get("headline")) or title or _txt(it.get("summary"))[:90]
            url = _txt(it.get("linkedinUrl")) or _txt(it.get("profileUrl")) or _txt(it.get("url"))
            loc = it.get("location")
            if isinstance(loc, dict):
                region = (_txt(loc.get("linkedinText")) or _txt(loc.get("text"))
                          or _txt(loc.get("city")) or _txt(loc.get("country")))
            else:
                region = _txt(loc)
            if name.strip():
                out.append({"full_name": name.strip(), "headline": headline,
                            "company": company, "profile_url": url, "region": region})
        return out

    def _parse_google(self, items, limit) -> list[dict]:
        out = []
        for page in items:
            results = page.get("organicResults", []) if isinstance(page, dict) else []
            for r in results:
                url = _txt(r.get("url"))
                if "/in/" not in url:
                    continue
                title = _txt(r.get("title")).replace(" | LinkedIn", "").replace(" - LinkedIn", "")
                parts = [p.strip() for p in re.split(r"\s[-|·]\s", title) if p.strip()]
                if not parts:
                    continue
                name = parts[0]
                # le nom de l'ecole/entreprise est en general le DERNIER segment du titre
                # ("Nom - Poste - Ecole"), pas le 2e (qui est souvent l'intitule de poste).
                desc = _txt(r.get("description"))
                # etablissement = ecole reelle, extraite de la description (jamais l'intitule de poste)
                company = _extract_company(parts, desc)
                headline = desc[:140] or (" - ".join(parts[1:]))
                out.append({"full_name": name, "headline": headline, "company": company,
                            "profile_url": url, "region": "", "summary": desc})
                if len(out) >= limit:
                    return out
        return out

    def _register(self, found: list[dict]) -> list[dict]:
        prospects = []
        for i, f in enumerate(found):
            key = (f.get("profile_url") or f.get("full_name") or "").lower()
            if key and key in self.returned:
                continue  # deja remonte lors d'une recherche precedente
            self.returned.add(key)
            # Id PROPRE et reproductible, fabrique depuis le NOM (sans accents) et non
            # depuis la fin de l'URL : le LLM doit pouvoir le recopier a l'identique.
            # (Ex. li_corinne_seguin, pas li__corinne_seguin_46a28865.)
            raw = f.get("full_name") or str(i)
            ascii_name = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
            base = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_") or str(i)
            pid = "li_" + base
            n = 2
            while pid in self.directory:   # unicite en cas d'homonyme
                pid = f"li_{base}_{n}"
                n += 1
            self.directory[pid] = f
            # Conversation simulee : a la prise de RDV, le prospect confirme son email pro
            # REEL (issu de l'enrichissement) pour recevoir l'invitation. AUCUNE donnee
            # inventee : si l'email n'a pas pu etre trouve, le prospect ne le donne pas.
            script = copy.deepcopy(SCRIPTS[i % len(SCRIPTS)])
            real_email = (f.get("email") or "").strip()
            if script and real_email:
                last = dict(script[-1])
                last["text"] = last["text"].rstrip() + f" Pour l'invitation, vous pouvez m'ecrire a {real_email}."
                script[-1] = last
            self.scripts[pid] = script
            prospects.append({
                "id": pid, "full_name": f["full_name"], "headline": f.get("headline", ""),
                "company": f.get("company", ""), "profile_url": f.get("profile_url", ""),
                "attributes": {"region": f.get("region", ""), "profile_url": f.get("profile_url", ""),
                               "summary": f.get("summary", ""), "job_title": f.get("job_title", ""),
                               "source": "Apify (profil reel)"},
            })
        return prospects

    def get_profile(self, prospect_id: str) -> dict:
        f = self.directory.get(prospect_id, {})
        return {"id": prospect_id, "full_name": f.get("full_name", ""),
                "headline": f.get("headline", ""), "company": f.get("company", ""),
                "attributes": {"region": f.get("region", ""), "source": "Apify (reel)"}}

    def search_by_name(self, name: str, company: str = "") -> dict:
        """Recherche un profil LinkedIn PRECIS par nom (+ ecole). Renvoie {} si rien."""
        if not (name or "").strip():
            return {}
        try:
            self._check()
            q = f'site:linkedin.com/in "{name}"'
            if company:
                q += f' "{company}"'
            body = {"queries": q, "maxPagesPerQuery": 1, "resultsPerPage": 5,
                    "countryCode": "fr", "languageCode": "fr"}
            out = self._parse_google(self._run_actor(GOOGLE, body), 3)
        except (ToolError, ToolUnavailable):
            return {}
        first = name.split()[0].lower() if name.split() else ""
        for o in out:
            if first and first in (o.get("full_name", "").lower()):
                return o
        return out[0] if out else {}

    def register_referral(self, prospect: dict) -> None:
        """Ajoute un contact recommande a l'annuaire avec une conversation de lead chaud."""
        pid = prospect["id"]
        attrs = prospect.get("attributes", {}) or {}
        self.directory[pid] = {"full_name": prospect.get("full_name", ""),
                               "headline": prospect.get("headline", ""),
                               "company": prospect.get("company", ""),
                               "profile_url": prospect.get("profile_url", ""),
                               "region": attrs.get("region", "")}
        self.scripts[pid] = list(WARM_REFERRAL)

    # --- conversation simulee (rien n'est envoye a de vraies personnes) ---
    def _record_outbound(self, prospect_id: str, substantive: bool) -> None:
        if not substantive:
            return
        n = self.stage.get(prospect_id, 0) + 1
        self.stage[prospect_id] = n
        for step in self.scripts.get(prospect_id, []):
            if step["after_out"] == n:
                self.reply_queue.append({"prospect_id": prospect_id, "text": step["text"],
                                         "ready_tick": self.current_tick + step["delay"]})

    def send_invitation(self, prospect_id: str, note: str) -> dict:
        self._record_outbound(prospect_id, substantive=True)
        return {"ok": True, "channel": "invite", "simulated": True}

    def send_message(self, prospect_id: str, text: str, is_followup: bool = False) -> dict:
        self._record_outbound(prospect_id, substantive=not is_followup)
        return {"ok": True, "channel": "message", "simulated": True}

    def fetch_new_replies(self) -> list[dict]:
        ready, kept = [], []
        for r in self.reply_queue:
            (ready if r["ready_tick"] <= self.current_tick else kept).append(r)
        self.reply_queue = [r for r in kept]
        return [{"prospect_id": r["prospect_id"], "text": r["text"]} for r in ready]
