"""Test de plomberie : memoire + outils simules, sans IA."""
import os, tempfile
import memory as M
from tools.mock import MockLinkedIn, MockCalendar, MockCrm

db = os.path.join(tempfile.gettempdir(), "_sdr_test.db")
if os.path.exists(db):
    os.remove(db)

mem = M.Memory(db)
li, cal, crm = MockLinkedIn(), MockCalendar(), MockCrm()

# 1) recherche + insertion
found = li.search_prospects({"role_keywords": ["admission", "career", "promotion"],
                             "min_students": 500, "max_students": 5000}, limit=10)
for p in found:
    mem.upsert_prospect(p)
print("recherche ->", len(found), "prospects ;", [p["full_name"] for p in found])

# 2) base clients
print("ESSCA est client ?", crm.check_existing("ESSCA")["is_client"])
print("ISG est client ?", crm.check_existing("ISG Programme Business Network")["is_client"])

# 3) cycle d'echange avec Claire sur plusieurs ticks
tick = 0
li.set_tick(tick)
li.send_invitation("li_claire", "Bonjour Claire ...")
mem.add_message("li_claire", "out", "invite", "Bonjour Claire ...")
mem.update_prospect("li_claire", state=M.CONTACTED, awaiting_reply=1)

for _ in range(3):
    tick += 1
    li.set_tick(tick)
    replies = li.fetch_new_replies()
    for r in replies:
        if r["prospect_id"] == "li_claire":
            mem.add_message("li_claire", "in", "message", r["text"])
            print(f"tick {tick} : reponse Claire ->", r["text"][:60])
            # on repond pour declencher la suite
            n = li.outbound_count.get("li_claire", 0)
            li.send_message("li_claire", f"reponse {n+1}")

# 4) agenda + reservation
slots = cal.get_slots()
print("creneaux:", [s["label"] for s in slots])
bk = cal.book(mem.get_prospect("li_claire"), slots[0]["id"], "claire@example.com")
print("RDV reserve:", bk["link"], "->", bk["label"])

# 5) trace + comptage
mem.log_decision(1, "li_claire", "obs", "rationale test", "send_invitation",
                 {"note": "x"}, "ok")
print("conversation Claire:", len(mem.get_conversation("li_claire")), "messages")
print("etats:", mem.counts_by_state())
print("OK plomberie.")
mem.close()
