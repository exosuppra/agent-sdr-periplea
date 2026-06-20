"""Point d'entree : lance l'agent SDR sur un ICP.

Exemples :
  python run.py                       # ICP par defaut, mode du .env, cerveau IA
  python run.py --planner scripted    # teste la mecanique SANS cle API
  python run.py --icp "Responsables Career Center, ecoles privees <1000 etudiants"
  python run.py --reset               # repart d'une base vide
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from config import Settings, Campaign
import memory as M
from trace import Trace

DEFAULT_ICP = (
    "Directeurs ou Responsables Promotion / Admissions / Career Center, dans des ecoles "
    "superieures privees francaises de 500 a 5000 etudiants, hors ecoles d'ingenieur publiques. "
    "Objectif : decrocher 3 rendez-vous qualifies de 30 minutes sur les 2 prochaines semaines, "
    "pour presenter MeetYourSchool."
)


def build_tools(mode: str, settings: Settings, search_real: bool = False):
    """Assemble les outils. mode = mock|live (agenda/CRM) ;
    search_real = recherche LinkedIn REELLE via Apify (conversation simulee)."""
    from tools.mock import MockLinkedIn, MockCalendar, MockCrm

    # --- LinkedIn ---
    if search_real and settings.apify_token:
        from tools.apify_linkedin import ApifyLinkedIn
        linkedin = ApifyLinkedIn(settings)
        print("[search] LinkedIn REEL via Apify (conversation simulee).")
    elif (mode == "live" and settings.unipile_api_key and settings.unipile_dsn
          and settings.unipile_account_id):
        from tools.unipile import UnipileLinkedIn
        linkedin = UnipileLinkedIn(settings)
        print("[live] LinkedIn REEL (Unipile).")
    else:
        linkedin = MockLinkedIn()
        if mode == "live" and not search_real:
            print("[live] LinkedIn simule (Unipile pas connecte).")

    # --- Agenda + CRM ---
    if mode == "live":
        from tools.calcom import CalComCalendar
        from tools.hubspot import HubSpotCrm
        calendar = (CalComCalendar(settings)
                    if settings.calcom_api_key and settings.calcom_event_type_id else MockCalendar())
        crm = HubSpotCrm(settings) if settings.hubspot_token else MockCrm()
    else:
        calendar, crm = MockCalendar(), MockCrm()
    return linkedin, calendar, crm


def build_planner(kind: str, settings: Settings):
    if kind == "scripted":
        from agent import ScriptedPlanner
        return ScriptedPlanner()
    from agent import LLMPlanner
    try:
        return LLMPlanner(settings)
    except Exception as e:
        sys.exit(f"Impossible d'initialiser le cerveau IA : {e}\n"
                 f"-> verifie ANTHROPIC_API_KEY dans .env, ou lance avec --planner scripted.")


def resume_pass(db: str = "sdr_memory.db", mode: str | None = None, planner_kind: str = "llm") -> dict:
    """PASSE DE REPRISE (le 'cron' de recuperation apres une panne d'outil).

    Termine, sur la base EXISTANTE, les taches restees en plan (prospects qualifies
    non contactes, RDV proposes non reserves, reponses a traiter), SANS reset et SANS
    chercher de nouveaux prospects. Si les outils sont revenus -> il finit le travail ;
    s'ils sont encore coupes -> il degrade proprement et la prochaine passe reessaiera.
    Idempotent (la memoire evite les doublons). Renvoie un petit bilan.
    """
    settings = Settings.load()
    if mode:
        settings.tools_mode = mode
    if planner_kind == "llm" and not settings.anthropic_api_key:
        return {"resumed": False, "reason": "cle IA manquante"}
    mem = M.Memory(db)
    pending = len(mem.list_prospects(M.OPEN_STATES))
    icp = mem.get_meta("icp")
    if not pending or not icp:
        mem.close()
        return {"resumed": False, "reason": "rien a reprendre", "pending": pending}
    run_id = "resume_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    trace = Trace(run_id)
    campaign = Campaign()
    campaign.meeting_target = 10 ** 6  # pas d'arret sur objectif : on finit TOUT l'existant
    linkedin, calendar, crm = build_tools(settings.tools_mode, settings, search_real=False)
    from agent import Orchestrator
    orch = Orchestrator(settings, campaign, mem, linkedin, calendar, crm, trace,
                        build_planner(planner_kind, settings), resume_only=True)
    still = pending
    try:
        orch.run(icp)
        still = len(mem.list_prospects(M.OPEN_STATES))
    finally:
        trace.close()
        mem.close()
    return {"resumed": True, "run_id": run_id, "started_pending": pending, "still_pending": still}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # accents propres dans le terminal Windows
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Agent SDR autonome LinkedIn")
    ap.add_argument("--icp", default=DEFAULT_ICP, help="profil cible en langage naturel")
    ap.add_argument("--mode", choices=["mock", "live"], default=None, help="surcharge TOOLS_MODE")
    ap.add_argument("--planner", choices=["llm", "scripted"], default="llm")
    ap.add_argument("--target", type=int, default=None, help="nombre de RDV vises")
    ap.add_argument("--db", default="sdr_memory.db")
    ap.add_argument("--reset", action="store_true", help="repart d'une base vide")
    ap.add_argument("--read-only", action="store_true",
                    help="mode observation : recherche + qualification, aucun contact envoye")
    ap.add_argument("--search-real", action="store_true",
                    help="recherche LinkedIn REELLE via Apify (conversation simulee)")
    args = ap.parse_args()

    settings = Settings.load()
    if args.mode:
        settings.tools_mode = args.mode
    campaign = Campaign()
    if args.target:
        campaign.meeting_target = args.target

    if args.reset:
        import os
        for f in (args.db,):
            if os.path.exists(f):
                os.remove(f)

    run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    mem = M.Memory(args.db)
    trace = Trace(run_id)
    linkedin, calendar, crm = build_tools(settings.tools_mode, settings, search_real=args.search_real)
    planner = build_planner(args.planner, settings)

    print(f"=== Agent SDR | mode={settings.tools_mode} | cerveau={args.planner} | objectif={campaign.meeting_target} RDV ===\n")

    from agent import Orchestrator
    orch = Orchestrator(settings, campaign, mem, linkedin, calendar, crm, trace, planner,
                        read_only=args.read_only)
    try:
        orch.run(args.icp)
    finally:
        trace.close()
        mem.close()

    print(f"\nLogs : logs/{run_id}.md  et  logs/{run_id}.jsonl")
    print(f"Base : {args.db}  (inspectable avec n'importe quel lecteur SQLite)")


if __name__ == "__main__":
    main()
