"""Lance TOUTE la demo en une seule commande.

  python demo.py

Demarre le tableau de bord, ouvre le navigateur, puis lance l'agent.
Pas besoin de deux fenetres.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

import dashboard
import run as runner

DB = "sdr_memory.db"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # base vierge AVANT de servir (evite tout verrou de fichier)
    if os.path.exists(DB):
        os.remove(DB)

    # demarre le tableau de bord sur le premier port libre
    srv = None
    port = None
    for p in range(8000, 8010):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), dashboard.Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print("Impossible d'ouvrir un port pour le tableau de bord (8000-8009).")
        return
    srv.db = DB
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = f"http://localhost:{port}"
    print("=" * 60)
    print(f"  TABLEAU DE BORD : {url}")
    print("  (il s'ouvre dans ton navigateur ; sinon copie-colle l'adresse)")
    print("=" * 60)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    time.sleep(2)

    # lance l'agent (cerveau IA, objectif 5 RDV) ; --reset deja fait plus haut
    sys.argv = ["run.py", "--target", "5"]
    runner.main()

    print("\n" + "=" * 60)
    print("  Demo terminee. Le tableau de bord reste affiche.")
    print("  Ferme cette fenetre (ou Ctrl+C) pour quitter.")
    print("=" * 60)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
