"""Memoire persistante de l'agent (SQLite, un seul fichier).

Pour chaque prospect on garde : son etat, sa qualification, l'historique
de conversation complet, et la trace de decision de l'agent.
Un seul fichier .db => facile a inspecter et a livrer comme preuve.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

# --- Etats possibles d'un prospect (machine a etats) ---
NEW = "NEW"                          # identifie, pas encore qualifie
QUALIFIED = "QUALIFIED"              # eligible, pret a etre contacte
DISQUALIFIED = "DISQUALIFIED"        # ecarte (ecole non eligible, deja client...)
CONTACTED = "CONTACTED"             # invitation / 1er message envoye, en attente
IN_CONVERSATION = "IN_CONVERSATION"  # le prospect a repondu, echange en cours
MEETING_PROPOSED = "MEETING_PROPOSED"
MEETING_BOOKED = "MEETING_BOOKED"    # objectif atteint
DEAD = "DEAD"                        # N relances sans reponse -> stop

OPEN_STATES = [NEW, QUALIFIED, CONTACTED, IN_CONVERSATION, MEETING_PROPOSED]
TERMINAL_STATES = [DISQUALIFIED, MEETING_BOOKED, DEAD]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    def __init__(self, path: str = "sdr_memory.db"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS prospects (
                id TEXT PRIMARY KEY,
                full_name TEXT,
                headline TEXT,
                company TEXT,
                profile_url TEXT,
                attributes TEXT,
                state TEXT,
                qualification TEXT,
                follow_up_count INTEGER DEFAULT 0,
                awaiting_reply INTEGER DEFAULT 0,
                next_action_at REAL,
                offered_slots TEXT,
                last_inbound_at TEXT,
                last_outbound_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prospect_id TEXT,
                direction TEXT,   -- 'out' (nous) ou 'in' (le prospect)
                channel TEXT,     -- 'invite' | 'message' | 'inmail'
                text TEXT,
                ts TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle INTEGER,
                prospect_id TEXT,
                observation TEXT,
                rationale TEXT,
                action TEXT,
                args TEXT,
                result TEXT,
                ts TEXT
            );
            """
        )
        self.conn.commit()

    # ---------- meta / compteurs de campagne ----------
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ---------- prospects ----------
    def upsert_prospect(self, p: dict) -> str:
        existing = self.get_prospect(p["id"])
        now = _now_iso()
        if existing:
            self.conn.execute(
                "UPDATE prospects SET full_name=?, headline=?, company=?, "
                "profile_url=?, attributes=?, updated_at=? WHERE id=?",
                (
                    p.get("full_name", existing["full_name"]),
                    p.get("headline", existing["headline"]),
                    p.get("company", existing["company"]),
                    p.get("profile_url", existing["profile_url"]),
                    json.dumps(p.get("attributes", existing["attributes"])),
                    now,
                    p["id"],
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO prospects(id, full_name, headline, company, profile_url, "
                "attributes, state, qualification, follow_up_count, awaiting_reply, "
                "next_action_at, offered_slots, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,0,?,?,?,?)",
                (
                    p["id"], p.get("full_name", ""), p.get("headline", ""),
                    p.get("company", ""), p.get("profile_url", ""),
                    json.dumps(p.get("attributes", {})),
                    NEW, json.dumps(None), None, json.dumps(None), now, now,
                ),
            )
        self.conn.commit()
        return p["id"]

    def get_prospect(self, pid: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        return self._row_to_prospect(row) if row else None

    def list_prospects(self, states: Optional[list[str]] = None) -> list[dict]:
        if states:
            placeholders = ",".join("?" * len(states))
            rows = self.conn.execute(
                f"SELECT * FROM prospects WHERE state IN ({placeholders})", states
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM prospects").fetchall()
        return [self._row_to_prospect(r) for r in rows]

    def update_prospect(self, pid: str, **fields) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("qualification", "offered_slots", "attributes"):
                v = json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("updated_at=?")
        vals.append(_now_iso())
        vals.append(pid)
        self.conn.execute(f"UPDATE prospects SET {', '.join(cols)} WHERE id=?", vals)
        self.conn.commit()

    def _row_to_prospect(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["attributes"] = json.loads(d.get("attributes") or "{}")
        d["qualification"] = json.loads(d.get("qualification") or "null")
        d["offered_slots"] = json.loads(d.get("offered_slots") or "null")
        return d

    # ---------- messages (conversation) ----------
    def add_message(self, prospect_id: str, direction: str, channel: str,
                    text: str, ts: Optional[str] = None) -> None:
        ts = ts or _now_iso()
        self.conn.execute(
            "INSERT INTO messages(prospect_id, direction, channel, text, ts) VALUES(?,?,?,?,?)",
            (prospect_id, direction, channel, text, ts),
        )
        if direction == "out":
            self.conn.execute("UPDATE prospects SET last_outbound_at=? WHERE id=?", (ts, prospect_id))
        else:
            self.conn.execute("UPDATE prospects SET last_inbound_at=? WHERE id=?", (ts, prospect_id))
        self.conn.commit()

    def get_conversation(self, prospect_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT direction, channel, text, ts FROM messages WHERE prospect_id=? ORDER BY id",
            (prospect_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- trace de decision ----------
    def log_decision(self, cycle: int, prospect_id: Optional[str], observation: str,
                     rationale: str, action: str, args: Any, result: str) -> None:
        self.conn.execute(
            "INSERT INTO decisions(cycle, prospect_id, observation, rationale, action, args, result, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (cycle, prospect_id, observation, rationale, action,
             json.dumps(args, ensure_ascii=False), result, _now_iso()),
        )
        self.conn.commit()

    def get_decisions(self, limit: Optional[int] = None) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        out = [dict(r) for r in rows]
        return out[-limit:] if limit else out

    def counts_by_state(self) -> dict:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) n FROM prospects GROUP BY state"
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def count_outbound_since(self, channel: str, since_iso: str) -> int:
        """Compte les envois sortants d'un canal depuis une date (plafonds LinkedIn)."""
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM messages WHERE direction='out' AND channel=? AND ts>=?",
            (channel, since_iso),
        ).fetchone()
        return row["n"]

    def reset(self) -> None:
        """Vide toutes les donnees de campagne (repart a zero) SANS supprimer le fichier.
        Plus robuste qu'un os.remove sous Windows (pas de souci de fichier verrouille par
        une lecture concurrente). Ne touche PAS aux fichiers externes (do_not_contact.json...)."""
        self.conn.executescript(
            "DELETE FROM decisions; DELETE FROM messages; DELETE FROM prospects; DELETE FROM meta;")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
