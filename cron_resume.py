"""Cron de REPRISE apres panne.

Relance regulierement la passe de reprise (run.resume_pass) : termine les taches
restees en plan quand un outil etait coupe (agenda/CRM/LinkedIn). Quand l'outil
revient, la passe suivante finit le travail ; tant qu'il est coupe, elle ne fait
rien de dommageable. Idempotent (la memoire evite les doublons).

Usages :
  python cron_resume.py                  # une seule passe (pour le Planificateur Windows)
  python cron_resume.py --loop 900       # boucle : une passe toutes les 900 s (15 min)
  python cron_resume.py --loop 120 --planner scripted   # demo sans cle IA

Production : planifier `python cron_resume.py` toutes les ~15 min via le
Planificateur de taches Windows (ou cron), OU lancer une fois `--loop 900`.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from run import resume_pass


def _once(db: str, mode, planner: str) -> dict:
    res = resume_pass(db=db, mode=mode, planner_kind=planner)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] reprise -> {res}")
    return res


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Cron de reprise des taches en plan apres panne d'outil")
    ap.add_argument("--db", default="sdr_memory.db")
    ap.add_argument("--mode", default=None, help="mock|live (defaut : TOOLS_MODE du .env)")
    ap.add_argument("--planner", default="llm", choices=["llm", "scripted"])
    ap.add_argument("--loop", type=int, default=0, help="intervalle en secondes ; 0 = une seule passe")
    args = ap.parse_args()

    if args.loop <= 0:
        _once(args.db, args.mode, args.planner)
        return

    print(f"Cron de reprise actif : une passe toutes les {args.loop} s. Ctrl+C pour arreter.")
    while True:
        try:
            _once(args.db, args.mode, args.planner)
        except KeyboardInterrupt:
            print("Arret du cron de reprise.")
            return
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] erreur reprise (ignoree) : {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
