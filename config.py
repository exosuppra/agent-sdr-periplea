"""Configuration centrale de l'agent SDR.

Charge les variables d'environnement depuis un fichier .env et expose
deux objets simples : Settings (cles / mode) et Campaign (regles du jeu).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # python-dotenv absent : on lit quand meme os.environ
    pass


@dataclass
class Settings:
    # IA (cerveau de l'agent)
    anthropic_api_key: str = ""
    model: str = "claude-sonnet-4-6"

    # Mode des outils : "mock" (bac a sable) ou "live" (vrais services)
    tools_mode: str = "mock"

    # Acces LinkedIn (Unipile) - mode live uniquement
    unipile_api_key: str = ""
    unipile_dsn: str = ""
    unipile_account_id: str = ""

    # CRM (HubSpot) - mode live uniquement
    hubspot_token: str = ""

    # Recherche LinkedIn reelle sans risque (Apify) - conversation simulee
    apify_token: str = ""

    # Enrichissement email OPTIONNEL (Hunter.io) - devine l'email pro depuis nom + ecole
    hunter_api_key: str = ""

    # Agenda (Cal.com) - mode live uniquement
    calcom_api_key: str = ""
    calcom_event_type_id: str = ""
    calcom_base: str = "https://api.cal.com/v2"  # instance EU = https://api.cal.eu/v2

    owner_email: str = "quentin.duroy28@gmail.com"

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            model=os.getenv("SDR_MODEL", "claude-sonnet-4-6"),
            tools_mode=os.getenv("TOOLS_MODE", "mock").lower(),
            unipile_api_key=os.getenv("UNIPILE_API_KEY", ""),
            unipile_dsn=os.getenv("UNIPILE_DSN", ""),
            unipile_account_id=os.getenv("UNIPILE_ACCOUNT_ID", ""),
            hubspot_token=os.getenv("HUBSPOT_TOKEN", ""),
            apify_token=os.getenv("APIFY_TOKEN", ""),
            hunter_api_key=os.getenv("HUNTER_API_KEY", ""),
            calcom_api_key=os.getenv("CALCOM_API_KEY", ""),
            calcom_event_type_id=os.getenv("CALCOM_EVENT_TYPE_ID", ""),
            calcom_base=os.getenv("CALCOM_BASE", "https://api.cal.com/v2"),
            owner_email=os.getenv("OWNER_EMAIL", "quentin.duroy28@gmail.com"),
        )


@dataclass
class Campaign:
    """Regles du jeu : objectif, criteres d'arret, securite LinkedIn."""
    # Objectif
    meeting_target: int = 3
    window_days: int = 14

    # Securite anti-spam / anti-bannissement (criteres d'arret)
    max_follow_ups: int = 3            # N relances sans reponse -> on clot
    follow_up_gap_hours: float = 48.0  # delai avant relance (mode live)

    # Plafonds LinkedIn prudents (pour tenir 6 mois sans griller le compte)
    max_invites_per_day: int = 20
    max_invites_per_week: int = 100   # plafond DUR LinkedIn, toutes offres (la vraie contrainte)
    max_messages_per_day: int = 40

    # Mode live : espacement aleatoire entre actions (timing humain anti-detection)
    live_min_action_gap_sec: float = 90.0
    live_max_action_gap_sec: float = 360.0

    # Securite anti-emballement de la boucle
    max_cycles: int = 80

    # --- Specifique au mode mock (compression du temps) ---
    # En bac a sable, 1 cycle = 1 "tick". Une relance devient due apres
    # `mock_follow_up_gap_cycles` cycles de silence ; les reponses simulees
    # arrivent au bout d'un certain nombre de ticks.
    mock_follow_up_gap_cycles: int = 1
