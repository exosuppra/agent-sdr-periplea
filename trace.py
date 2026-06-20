"""Affichage lisible de la trace de raisonnement de l'agent.

Chaque decision est imprimee dans la console ET ecrite dans
logs/<run>.md (lisible humain) + logs/<run>.jsonl (machine), pour
repondre au livrable "logs detailles d'un parcours prospect" et au
critere d'evaluation "lisibilite de la trace".
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


class Trace:
    def __init__(self, run_id: str, log_dir: str = "logs"):
        os.makedirs(log_dir, exist_ok=True)
        self.run_id = run_id
        self.md_path = os.path.join(log_dir, f"{run_id}.md")
        self.jsonl_path = os.path.join(log_dir, f"{run_id}.jsonl")
        self._md = open(self.md_path, "w", encoding="utf-8")
        self._jsonl = open(self.jsonl_path, "w", encoding="utf-8")
        self._md.write(f"# Trace de decision - run {run_id}\n\n")
        self._md.flush()

    def event(self, kind: str, text: str) -> None:
        """Evenement de cadrage (demarrage, fin, panne d'outil, etc.)."""
        print(f"[{kind}] {text}")
        self._md.write(f"**{kind}** : {text}\n\n")
        self._md.flush()

    def decision(self, cycle: int, prospect: dict | None, rationale: str,
                 action: str, args: dict, result: str) -> None:
        name = prospect.get("full_name") if prospect else "(global)"
        state = prospect.get("state") if prospect else "-"
        args_str = json.dumps(args, ensure_ascii=False)

        print(f"\n--- cycle {cycle} | {name} [{state}]")
        print(f"    pourquoi : {rationale}")
        print(f"    action   : {action} {args_str}")
        print(f"    resultat : {result}")

        self._md.write(
            f"\n### Cycle {cycle} - {name} ({state})\n"
            f"- raisonnement : {rationale}\n"
            f"- action : `{action}` {args_str}\n"
            f"- resultat : {result}\n"
        )
        self._md.flush()

        self._jsonl.write(json.dumps({
            "cycle": cycle,
            "prospect": name,
            "state": state,
            "rationale": rationale,
            "action": action,
            "args": args,
            "result": result,
            "ts": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False) + "\n")
        self._jsonl.flush()

    def close(self) -> None:
        self._md.close()
        self._jsonl.close()
