"""Cockpit de demo de l'agent SDR (tableau de bord web pilotable).

Sert une page web qui lit la base SQLite et affiche, en direct :
- le pipeline des prospects (kanban par etat),
- le flux des decisions de l'agent avec le "pourquoi" (trace de raisonnement),
- la conversation complete de chaque prospect (clic sur une carte).

Et surtout, on PILOTE la demo depuis le navigateur (aucun terminal) :
- saisir un ICP + cliquer "Lancer l'agent",
- couper/retablir un outil (agenda, CRM, LinkedIn) -> grain de sable,
- le prospect piege est deja dans le scenario.

Usage : python dashboard.py   (ou double-clic COCKPIT.bat)  puis http://localhost:8000
Aucune dependance externe (http.server + sqlite stdlib).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from run import DEFAULT_ICP
from tools.piloted import pilot_active, pilot_transcript, pilot_inject, pilot_reset

DB = "sdr_memory.db"
RUN_LOCK = threading.Lock()
STATE = {"running": False}


# ---------- lecture de l'etat ----------
def read_state(db: str) -> dict:
    pilot = {"active": pilot_active(), "transcript": pilot_transcript()}
    base = {"icp": "", "ready": False, "running": STATE["running"],
            "prospects": [], "decisions": [], "counts": {}, "pilot": pilot}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.OperationalError:
        return base
    con.row_factory = sqlite3.Row
    try:
        icp_row = con.execute("SELECT value FROM meta WHERE key='icp'").fetchone()
        icp = json.loads(icp_row["value"]) if icp_row else ""
        prospects = []
        for r in con.execute("SELECT * FROM prospects ORDER BY created_at"):
            conv = [dict(m) for m in con.execute(
                "SELECT direction, text, ts FROM messages WHERE prospect_id=? ORDER BY id",
                (r["id"],))]
            prospects.append({
                "id": r["id"], "name": r["full_name"], "headline": r["headline"],
                "company": r["company"], "state": r["state"],
                "follow_up_count": r["follow_up_count"],
                "qualification": json.loads(r["qualification"] or "null"),
                "attributes": json.loads(r["attributes"] or "{}"),
                "conversation": conv})
        names = {p["id"]: p["name"] for p in prospects}
        decisions = []
        for d in con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 300"):
            decisions.append({
                "cycle": d["cycle"], "prospect": names.get(d["prospect_id"], "-"),
                "prospect_id": d["prospect_id"],
                "action": d["action"], "rationale": d["rationale"],
                "args": json.loads(d["args"] or "{}"), "result": d["result"]})
        counts = {r["state"]: r["n"] for r in
                  con.execute("SELECT state, COUNT(*) n FROM prospects GROUP BY state")}
        return {"icp": icp, "ready": True, "running": STATE["running"],
                "prospects": prospects, "decisions": decisions, "counts": counts, "pilot": pilot}
    except sqlite3.OperationalError:
        return base
    finally:
        con.close()


# ---------- pilotage ----------
def start_run(db: str, icp: str, target: int, read_only: bool = False,
              live: bool = False, search_real: bool = False,
              pilot: bool = False, pilot_name: str = "", resume: bool = False) -> bool:
    if not RUN_LOCK.acquire(blocking=False):
        return False  # un run est deja en cours
    STATE["running"] = True

    def job():
        try:
            from config import Settings, Campaign
            import memory as M
            from trace import Trace
            from agent import Orchestrator, LLMPlanner
            from run import build_tools
            settings = Settings.load()
            campaign = Campaign()
            if target:
                campaign.meeting_target = target
            rid = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            mem = M.Memory(db)
            if not resume:
                mem.reset()  # repart d'une base vide ; si resume -> on garde la memoire (reprise de campagne)
            tr = Trace(rid)
            linkedin, calendar, crm = build_tools("live" if live else "mock", settings,
                                                  search_real=search_real)
            if pilot:
                from tools.piloted import PilotedLinkedIn
                pilot_reset({"full_name": pilot_name or "Vous (test)",
                             "headline": "Responsable Admissions (prospect test)",
                             "company": "Ecole de commerce (test)"})
                linkedin = PilotedLinkedIn(linkedin)
            else:
                pilot_reset(None)
            orch = Orchestrator(settings, campaign, mem, linkedin, calendar, crm,
                                tr, LLMPlanner(settings), read_only=read_only)
            orch.run(icp or DEFAULT_ICP)
            tr.close()
            mem.close()
        except Exception as e:
            print("ERREUR run agent :", e)
        finally:
            STATE["running"] = False
            RUN_LOCK.release()

    threading.Thread(target=job, daemon=True).start()
    return True


def build_chat_calendar(settings):
    """Agenda pour le mini-chatbot : Cal.com REEL si les cles sont presentes, sinon mock.
    Permet au chatbot de verifier la vraie dispo et de reserver pour de vrai."""
    if settings.calcom_api_key and settings.calcom_event_type_id:
        try:
            from tools.calcom import CalComCalendar
            return CalComCalendar(settings)
        except Exception:
            pass
    from tools.mock import MockCalendar
    return MockCalendar()


def build_chat_crm(settings):
    """CRM pour le mini-chatbot : HubSpot REEL si token present, sinon mock.
    Sert a enregistrer un contact recommande quand le prospect redirige."""
    if settings.hubspot_token:
        try:
            from tools.hubspot import HubSpotCrm
            return HubSpotCrm(settings)
        except Exception:
            pass
    from tools.mock import MockCrm
    return MockCrm()


def set_chaos(tool: str, state: str) -> None:
    data = {}
    if os.path.exists("chaos.json"):
        try:
            data = json.load(open("chaos.json", encoding="utf-8"))
        except Exception:
            data = {}
    if tool == "all":
        data = {}  # tout retablir
    else:
        data[tool] = state
    json.dump(data, open("chaos.json", "w", encoding="utf-8"))


PAGE = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Agent SDR - cockpit</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--card:#1e222b;--line:#2a2f3a;--txt:#e7e9ee;--mut:#9aa3b2;}
*{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
body{margin:0;background:var(--bg);color:var(--txt)}
header{padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{font-size:16px;margin:0 0 8px;font-weight:600}
.ctrl{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.ctrl input[type=text]{flex:1;min-width:280px;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px 10px;font-size:13px}
.ctrl input[type=number]{width:64px;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px;font-size:13px}
button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
button.ghost{background:#222734;color:var(--txt);border:1px solid var(--line)}
button.warn{background:#7f1d1d}
.run{font-size:12px;color:var(--mut);margin-left:6px}
.kpis{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:13px}
.kpi b{font-size:18px;display:block}
.icp{color:var(--mut);font-size:12px;margin-top:6px}
.wrap{display:grid;grid-template-columns:1.45fr 1fr;gap:14px;padding:14px 20px;align-items:start}
.board{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px}
.col{flex:1;min-width:150px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px}
.col h2{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:2px 4px 8px;display:flex;justify-content:space-between}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--ac,#3b82f6);border-radius:8px;padding:8px 10px;margin-bottom:8px;cursor:pointer}
.card:hover{border-color:#4b5566}
.card .nm{font-weight:600;font-size:13px}
.card .sub{color:var(--mut);font-size:11px;margin-top:2px}
.card .lnk{display:inline-block;margin-top:5px;font-size:11px;color:#60a5fa;text-decoration:none}
.card .lnk:hover{text-decoration:underline}
.badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:10px;background:#2a2f3a;color:var(--mut);margin-top:6px}
.trace{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;max-height:78vh;overflow:auto}
.trace h2{font-size:13px;margin:2px 4px 10px}
.ev{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px;background:var(--card)}
.ev .top{display:flex;justify-content:space-between;font-size:11px;color:var(--mut)}
.ev .act{display:inline-block;font-size:11px;font-weight:600;color:#cdd3df;background:#222734;border-radius:6px;padding:1px 7px;margin:4px 0}
.ev .why{font-size:12px;line-height:1.45}
.ev .res{font-size:11px;color:var(--mut);margin-top:4px}
.ev .msg{font-size:11px;color:#b9c2d0;background:#11151c;border-radius:6px;padding:6px 8px;margin-top:5px;white-space:pre-wrap}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;max-width:640px;width:92%;max-height:84vh;overflow:auto;padding:18px}
.bubble{border-radius:10px;padding:8px 11px;margin:6px 0;font-size:13px;line-height:1.45;white-space:pre-wrap;max-width:85%}
.out{background:#1d3a5f;margin-left:auto}.in{background:#26303d}
.x{float:right;cursor:pointer;color:var(--mut);font-size:18px}
.hint{color:var(--mut);font-size:13px;padding:30px;text-align:center}
.chatpanel{margin:0 20px 24px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.chatpanel h2{font-size:14px;margin:0 0 4px}
.chatmeta{font-size:12px;color:var(--mut);margin-bottom:10px}
.chatlog{min-height:60px;max-height:320px;overflow:auto;display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
.cbub{border-radius:10px;padding:8px 11px;font-size:13px;line-height:1.45;max-width:80%;white-space:pre-wrap}
.cme{background:#3a1620;border:1px solid #ef4444;color:#ffd7d7;align-self:flex-end}
.cag{background:#15233a;border:1px solid #2563eb;color:#dbe6ff;align-self:flex-start}
.cop{background:#163a2a;border:1px solid #22c55e;color:#d7ffe6;align-self:flex-end}
.chatform{display:flex;gap:8px}
.chatform input{flex:1;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px 10px;font-size:13px}
.facts{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 8px}
.fact{background:#11151c;border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:11px;color:var(--mut)}
.fact b{color:var(--txt);margin-left:5px;font-weight:600}
.verdict{display:inline-block;font-size:11px;font-weight:600;border-radius:6px;padding:2px 8px;margin:2px 0 6px}
.vok{background:#13311f;color:#86efac;border:1px solid #16a34a}
.vko{background:#3a1620;color:#fca5a5;border:1px solid #ef4444}
.jrow{border:1px solid var(--line);border-left:3px solid #8b5cf6;border-radius:8px;padding:8px 10px;margin-bottom:8px;background:var(--card)}
.pilotwhy{display:none;background:#11151c;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:10px}
.pw-title{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin-bottom:5px}
.pw-row{font-size:12px;line-height:1.45;margin:3px 0;color:#c7ced9}
</style></head><body>
<header>
  <h1>Agent SDR autonome - cockpit de démo</h1>
  <div class="ctrl">
    <input type="text" id="icp" placeholder="ICP en langage naturel">
    <span style="font-size:12px;color:var(--mut)">RDV visés</span>
    <input type="number" id="target" value="5" min="1" max="10">
    <button id="go" onclick="launch()">▶ Lancer l'agent</button>
    <label style="font-size:12px;color:var(--mut);display:flex;align-items:center;gap:5px"><input type="checkbox" id="readonly"> Mode observation (ne contacte personne)</label>
    <label style="font-size:12px;color:#f59e0b;display:flex;align-items:center;gap:5px"><input type="checkbox" id="live"> Mode réel (écrit dans Cal.com + HubSpot)</label>
    <label style="font-size:12px;color:#22c55e;display:flex;align-items:center;gap:5px"><input type="checkbox" id="searchreal"> Recherche réelle (vrais profils via Apify)</label>
    <label style="font-size:12px;color:#a855f7;display:flex;align-items:center;gap:5px"><input type="checkbox" id="pilot"> M'inclure comme prospect (je réponds dans le chat ci-dessous)</label>
    <label style="font-size:12px;color:#0ea5e9;display:flex;align-items:center;gap:5px"><input type="checkbox" id="resume"> Reprendre la campagne (mémoire persistante, sans reset)</label>
    <span class="run" id="runstate"></span>
  </div>
  <div class="ctrl" style="margin-top:8px">
    <span style="font-size:12px;color:var(--mut)">Grain de sable :</span>
    <button class="ghost" onclick="chaos('calendar','down')">Couper l'agenda</button>
    <button class="ghost" onclick="chaos('crm','down')">Couper le CRM</button>
    <button class="ghost" onclick="chaos('linkedin','down')">Couper LinkedIn</button>
    <button class="warn" onclick="chaos('all','ok')">Tout rétablir</button>
    <span class="run" id="chaosstate"></span>
  </div>
  <div class="kpis" id="kpis"></div>
  <div class="icp" id="icpshow"></div>
</header>
<div class="wrap">
  <div><div class="board" id="board"></div></div>
  <div class="trace"><h2>Trace de décision (le « pourquoi » de chaque action)</h2><div id="trace"></div></div>
</div>
<div class="chatpanel">
  <h2>Conversation pilotée (testez le chatbot en direct)</h2>
  <div class="chatmeta">C'est l'agent qui vous contacte en premier (c'est lui qui prospecte) : son message d'approche s'affiche tout seul ci-dessous. Répondez-lui comme un prospect (vos messages en rouge) : objection, question piège, « envoyez vos tarifs »... « Démarrer » relance une nouvelle conversation. Pour vous inclure dans une vraie campagne (recherche + agenda + CRM), cochez plutôt « M'inclure comme prospect » en haut avant de lancer un run.</div>
  <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
    <span style="font-size:12px;color:var(--mut);white-space:nowrap">Profil joué (réel) :</span>
    <input id="chatprofile" style="flex:1;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 10px;font-size:12px" placeholder="Colle ici le VRAI profil du prospect que tu joues (poste + école réels). Laisse vide pour un test générique.">
  </div>
  <div id="pilotwhy" class="pilotwhy"></div>
  <div id="chatlog" class="chatlog"></div>
  <div class="chatform">
    <button class="ghost" onclick="startChat()">Démarrer (l'agent vous contacte)</button>
    <input id="chatin" placeholder="Répondez à l'agent... (ex : Envoyez-moi vos tarifs)" onkeydown="if(event.key==='Enter')sendChat()">
    <button onclick="sendChat()">Envoyer</button>
    <button class="ghost" onclick="resetChat()">Réinitialiser</button>
  </div>
</div>
<div class="chatpanel">
  <h2>Console opérateur (interrogez et pilotez l'agent)</h2>
  <div class="chatmeta">Ici vous parlez à l'agent en tant qu'<b>opérateur</b> (pas en tant que prospect). Demandez où il en est (« où en es-tu avec Claire ? »), faites-lui transmettre une info (« écris à Marc que notre webinaire est jeudi 18h »), ou faites-lui déplacer un RDV en cas d'empêchement (« décale le RDV de Claire à mardi 14h, empêchement de dernière minute »).</div>
  <div id="oplog" class="chatlog"></div>
  <div class="chatform">
    <input id="opin" placeholder="Votre consigne à l'agent... (ex : où en es-tu avec Claire ?)" onkeydown="if(event.key==='Enter')sendOperator()">
    <button onclick="sendOperator()">Envoyer</button>
  </div>
</div>
<div id="modal"></div>
<script>
const DEFAULT_ICP=`__ICP__`;
const COLS=[["NEW","Identifiés","#6b7280"],["QUALIFIED","Qualifiés","#0ea5e9"],
["CONTACTED","Contactés","#3b82f6"],["IN_CONVERSATION","En conversation","#8b5cf6"],
["MEETING_PROPOSED","RDV proposé","#f59e0b"],["MEETING_BOOKED","RDV pris","#22c55e"],
["DISQUALIFIED","Disqualifiés","#64748b"],["DEAD","Abandonnés","#ef4444"]];
let SEL=null, DATA={prospects:[]}, lastPilot={active:false,transcript:[]}, lastModalSig='';
document.getElementById('icp').value=DEFAULT_ICP;
function esc(s){return (s||"").replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function msgText(a){return a.note||a.text||a.message||a.reasons||a.reason||"";}
async function launch(){
  const icp=document.getElementById('icp').value, target=+document.getElementById('target').value;
  const read_only=document.getElementById('readonly').checked;
  const live=document.getElementById('live').checked;
  const search_real=document.getElementById('searchreal').checked;
  const pilot=document.getElementById('pilot').checked;
  const resume=document.getElementById('resume').checked;
  if(live && !confirm("Mode réel : l'agent va écrire de vraies données dans Cal.com et HubSpot (contacts, notes, vrais rendez-vous). Utilise un objectif bas (1 ou 2). Continuer ?")) return;
  if(pilot) document.getElementById('chatin').focus();
  const r=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({icp,target,read_only,live,search_real,pilot,resume})})).json();
  if(!r.started){document.getElementById('runstate').textContent="un run est déjà en cours…"; return;}
  if(!resume){
    // reset visuel IMMEDIAT : on repart d'une base vide cote serveur
    SEL=null; close_();
    DATA={prospects:[],decisions:[],counts:{},running:true,ready:true};
    document.getElementById('board').innerHTML='';
    document.getElementById('kpis').innerHTML='';
    document.getElementById('trace').innerHTML='<div class="hint">Démarrage de l\\'agent, le pipeline va se remplir...</div>';
  }
  document.getElementById('runstate').textContent = resume ? "⏳ reprise de la campagne…" : "⏳ agent en cours…";
}
async function chaos(tool,state){
  await fetch('/api/chaos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tool,state})});
  const el=document.getElementById('chaosstate');
  el.textContent = tool==='all' ? "tous les outils rétablis ✅" : (tool+" coupé 🔌 (l'agent va s'adapter)");
}
function kpi(l,v,c){return `<div class="kpi"><b style="color:${c}">${v}</b>${l}</div>`;}
function render(s){
  if(s.ready===false && DATA.prospects && DATA.prospects.length && !s.running){return;}
  DATA=s;
  document.getElementById('go').disabled=s.running;
  document.getElementById('runstate').textContent=s.running?"⏳ agent en cours…":"";
  document.getElementById('icpshow').textContent=s.icp?("ICP courant : "+s.icp):"";
  const c=s.counts, booked=c.MEETING_BOOKED||0, disq=c.DISQUALIFIED||0, dead=c.DEAD||0, tot=s.prospects.length;
  document.getElementById('kpis').innerHTML=
    kpi("RDV pris",booked,"#22c55e")+kpi("En cours",tot-booked-disq-dead,"#3b82f6")
    +kpi("Disqualifiés",disq,"#64748b")+kpi("Abandonnés",dead,"#ef4444")+kpi("Prospects",tot,"#9aa3b2");
  document.getElementById('board').innerHTML=COLS.map(col=>{
    const ps=s.prospects.filter(p=>p.state===col[0]);
    return `<div class="col"><h2><span>${col[1]}</span><span>${ps.length}</span></h2>`+
      ps.map(p=>`<div class="card" style="--ac:${col[2]}" onclick="open_('${p.id}')">
        <div class="nm">${esc(p.name)}</div><div class="sub">${esc(p.company)}</div>
        <div class="sub">${esc(p.headline)}</div>
        ${(p.attributes&&p.attributes.profile_url)?`<a class="lnk" href="${esc(p.attributes.profile_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Profil LinkedIn ↗</a>`:''}
        ${p.follow_up_count?`<span class="badge">${p.follow_up_count} relance(s)</span>`:''}</div>`).join('')+`</div>`;
  }).join('');
  document.getElementById('trace').innerHTML=s.decisions.map(d=>{
    const m=msgText(d.args);
    return `<div class="ev"><div class="top"><span>cycle ${d.cycle} · ${esc(d.prospect)}</span></div>
      <span class="act">${esc(d.action)}</span><div class="why">${esc(d.rationale)}</div>
      ${m?`<div class="msg">${esc(m)}</div>`:''}<div class="res">→ ${esc(d.result)}</div></div>`;
  }).join('') || '<div class="hint">Saisis un ICP et clique « Lancer l\\'agent ».</div>';
  lastPilot=s.pilot||{active:false,transcript:[]};
  renderChat();
  updatePilotWhy();
  // L'agent ouvre la conversation de lui-meme des l'arrivee (c'est lui qui prospecte),
  // sauf si un run pilote est deja en cours (la, c'est le run qui contacte).
  if(!chatAutoStarted && !lastPilot.active && !s.running && chatHistory.length===0){
    chatAutoStarted=true; startChat();
  }
  if(SEL){const sig=modalSig(); if(sig!==lastModalSig){lastModalSig=sig; drawModal();}}
}
function modalSig(){
  const p=DATA.prospects.find(x=>x.id===SEL); if(!p)return '';
  const nd=(DATA.decisions||[]).filter(d=>d.prospect_id===SEL).length;
  return [SEL,p.state,p.conversation.length,nd,p.qualification?1:0].join('|');
}
function open_(id){SEL=id;drawModal();lastModalSig=modalSig();}
function close_(){SEL=null;lastModalSig='';document.getElementById('modal').innerHTML='';}
function whyBlock(p){
  const a=p.attributes||{}, rows=[];
  const fact=(label,val)=>rows.push(`<div class="fact">${label}<b>${esc(String(val))}</b></div>`);
  if(a.school_type) fact("Type", a.school_type==='privee'?'école privée (cible)':a.school_type);
  if(a.student_count) fact("Taille", a.student_count+" étudiants");
  if(a.region) fact("Région", a.region);
  if(a.client_check && typeof a.client_check.is_client!=='undefined') fact("Déjà client", a.client_check.is_client?'oui (à écarter)':'non');
  if(a.source) fact("Source", a.source);
  const facts=rows.length?`<div class="facts">${rows.join('')}</div>`:'';
  const q=p.qualification; let verdict;
  if(q){
    verdict=`<div class="verdict ${q.eligible?'vok':'vko'}">${q.eligible?'✓ Jugé dans la cible':'✗ Écarté'}</div>`
      +(q.reasons?`<div class="why"><b>Raison :</b> ${esc(q.reasons)}</div>`:'')
      +(q.buying_signals?`<div class="why"><b>Signaux d'achat :</b> ${esc(q.buying_signals)}</div>`:'');
  } else {
    verdict=`<div class="why" style="color:var(--mut)">Qualification pas encore tranchée (l'agent doit d'abord vérifier l'école et la base clients).</div>`;
  }
  return `<div class="ev"><span class="act">Pourquoi ce prospect (avant le dialogue)</span>${facts}${verdict}</div>`;
}
function journalBlock(p){
  const ds=(DATA.decisions||[]).filter(d=>d.prospect_id===p.id).slice().reverse();
  if(!ds.length) return '';
  const items=ds.map(d=>{
    const m=msgText(d.args);
    return `<div class="jrow"><span class="act">${esc(d.action)}</span>
      <div class="why">${esc(d.rationale||'')}</div>
      ${m?`<div class="msg">${esc(m)}</div>`:''}
      <div class="res">→ ${esc(d.result||'')}</div></div>`;
  }).join('');
  return `<h2 style="font-size:13px;margin:16px 0 6px">Journal des décisions de l'agent (${ds.length}) : le « pourquoi » de chaque choix</h2>${items}`;
}
function drawModal(){
  const p=DATA.prospects.find(x=>x.id===SEL); if(!p){close_();return;}
  const conv=p.conversation.map(m=>`<div class="bubble ${m.direction==='out'?'out':'in'}">${esc(m.text)}</div>`).join('')||'<div class="hint">Pas encore de message (l\\'agent ne l\\'a pas encore contacté).</div>';
  document.getElementById('modal').innerHTML=`<div class="modal" onclick="if(event.target===this)close_()">
    <div class="box"><span class="x" onclick="close_()">×</span>
    <h1>${esc(p.name)}</h1><div class="icp">${esc(p.headline)} · ${esc(p.company)} · état ${esc(p.state)}</div>
    ${whyBlock(p)}
    <h2 style="font-size:13px;margin:16px 0 4px">Conversation</h2>${conv}
    ${journalBlock(p)}</div></div>`;
}
let chatHistory=[], chatAutoStarted=false;
// Anti-rebond : un prospect peut envoyer 2, 3, 4 messages a la suite. On attend
// 10 s (le delai se rearme a chaque nouveau message) avant de TOUT compiler et de
// repondre une seule fois, de facon pertinente.
let pendingMsgs=[], chatTimer=null, chatBusy=false, chatWaiting=false;
const CHAT_DEBOUNCE_MS=10000;
function chatProfile(){const e=document.getElementById('chatprofile');return e?e.value.trim():'';}
function renderChat(){
  const l=document.getElementById('chatlog'); if(!l)return;
  let items = lastPilot.active
    ? (lastPilot.transcript||[]).map(m=>({role:(m.who==='agent'?'agent':'prospect'),text:m.text}))
    : chatHistory.slice();
  if(!lastPilot.active && chatWaiting) items=items.concat([{role:'agent',text:'…'}]);
  l.innerHTML=items.map(m=> m.role==='sys'
      ? `<div class="hint" style="padding:8px 6px">${esc(m.text)}</div>`
      : `<div class="cbub ${m.role==='agent'?'cag':'cme'}">${esc(m.text)}</div>`).join('')
    || '<div class="hint">'+(lastPilot.active?"L'agent va vous contacter, patientez quelques secondes...":"L'agent prépare son message d'approche...")+'</div>';
  l.scrollTop=l.scrollHeight;
}
async function sendChat(){
  const inp=document.getElementById('chatin'); const msg=inp.value.trim(); if(!msg)return; inp.value='';
  if(lastPilot.active){
    await fetch('/api/inject_reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:msg})});
    return; // la transcription se met à jour au prochain rafraîchissement
  }
  if(chatHistory.length && chatHistory[chatHistory.length-1].role==='sys') return; // conversation terminée
  // On AFFICHE le message tout de suite, mais on retarde la reponse de l'agent :
  // s'il arrive d'autres messages dans les 10 s, on les compile tous ensemble.
  chatHistory.push({role:'prospect',text:msg}); pendingMsgs.push(msg);
  chatWaiting=true; renderChat();
  if(chatTimer) clearTimeout(chatTimer);
  chatTimer=setTimeout(flushChat, CHAT_DEBOUNCE_MS);
}
async function flushChat(){
  chatTimer=null;
  if(chatBusy){ chatTimer=setTimeout(flushChat, 1500); return; }  // attend l'appel en cours
  const batch=pendingMsgs.slice(); pendingMsgs=[];
  if(!batch.length){ chatWaiting=false; renderChat(); return; }
  chatBusy=true;
  const prior=chatHistory.slice(0, chatHistory.length - batch.length);  // tout AVANT ce lot
  const combined=batch.join("\\n");  // l'agent recoit l'ensemble des messages d'un coup
  try{
    const r=await (await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history:prior,message:combined,profile:chatProfile()})})).json();
    chatWaiting=false;
    if(r.closed){ if(!(chatHistory.length && chatHistory[chatHistory.length-1].role==='sys')) chatHistory.push({role:'sys',text:"Conversation terminée : le prospect a demandé l'arrêt, l'agent ne répond plus."}); }
    else { chatHistory.push({role:'agent',text:r.reply||'(pas de réponse)'}); }
  }catch(e){ chatWaiting=false; chatHistory.push({role:'agent',text:'(erreur réseau)'}); }
  finally{ chatBusy=false; renderChat(); }
}
function updatePilotWhy(){
  const box=document.getElementById('pilotwhy'); if(!box)return;
  if(!lastPilot.active){box.style.display='none';box.innerHTML='';return;}
  const p=(DATA.prospects||[]).find(x=>x.attributes&&x.attributes.piloted);
  if(!p){box.style.display='none';box.innerHTML='';return;}
  const ds=(DATA.decisions||[]).filter(d=>d.prospect_id===p.id).slice(0,4);
  const why=ds.map(d=>`<div class="pw-row"><span class="act">${esc(d.action)}</span> ${esc(d.rationale||'')}</div>`).join('');
  box.style.display='block';
  box.innerHTML=`<div class="pw-title">Pourquoi l'agent vous contacte (raisonnement, le plus récent en premier)</div>`
    +(p.qualification&&p.qualification.reasons?`<div class="pw-row"><b>Qualification :</b> ${esc(p.qualification.reasons)}</div>`:'')
    +(why||'<div class="pw-row" style="color:var(--mut)">L\\'agent analyse votre profil...</div>');
}
function resetChat(){chatHistory=[];renderChat();}
async function startChat(){
  if(lastPilot.active)return;
  chatHistory=[{role:'agent',text:'…'}]; renderChat();
  try{ const r=await (await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history:[],message:'',profile:chatProfile()})})).json();
    chatHistory=[{role:'agent',text:r.reply||'(pas de réponse)'}];
  }catch(e){ chatHistory=[{role:'agent',text:'(erreur réseau)'}]; }
  renderChat(); const ci=document.getElementById('chatin'); if(ci)ci.focus();
}
renderChat();
let operatorHistory=[];
function renderOperator(){
  const l=document.getElementById('oplog'); if(!l)return;
  l.innerHTML=operatorHistory.map(m=>`<div class="cbub ${m.role==='agent'?'cag':'cop'}">${esc(m.text)}</div>`).join('')
    || '<div class="hint">Posez une question ou donnez une consigne a l\\'agent (il connait l\\'etat du pipeline).</div>';
  l.scrollTop=l.scrollHeight;
}
async function sendOperator(){
  const inp=document.getElementById('opin'); const msg=inp.value.trim(); if(!msg)return; inp.value='';
  const prior=operatorHistory.slice();
  operatorHistory.push({role:'operator',text:msg}); operatorHistory.push({role:'agent',text:'…'}); renderOperator();
  try{
    const r=await (await fetch('/api/operator',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history:prior,message:msg})})).json();
    operatorHistory[operatorHistory.length-1]={role:'agent',text:r.reply||'(pas de reponse)'};
  }catch(e){ operatorHistory[operatorHistory.length-1]={role:'agent',text:'(erreur reseau)'}; }
  renderOperator();
}
renderOperator();
async function tick(){try{const s=await (await fetch('/api/state')).json();render(s);}catch(e){}}
setInterval(tick,2000); tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._json(read_state(self.server.db))
        else:
            page = PAGE.replace("__ICP__", DEFAULT_ICP.replace("`", "'"))
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if self.path.startswith("/api/run"):
            started = start_run(self.server.db, body.get("icp", ""), int(body.get("target", 3)),
                                read_only=bool(body.get("read_only", False)),
                                live=bool(body.get("live", False)),
                                search_real=bool(body.get("search_real", False)),
                                pilot=bool(body.get("pilot", False)),
                                pilot_name=body.get("pilot_name", ""),
                                resume=bool(body.get("resume", False)))
            self._json({"started": started, "running": STATE["running"]})
        elif self.path.startswith("/api/chaos"):
            set_chaos(body.get("tool", ""), body.get("state", "ok"))
            self._json({"ok": True})
        elif self.path.startswith("/api/chat"):
            from config import Settings
            from agent import chat_reply
            settings = Settings.load()
            cal = build_chat_calendar(settings)
            crm = build_chat_crm(settings)
            reply = chat_reply(settings,
                               body.get("profile", "directeur des admissions, ecole de commerce privee"),
                               body.get("history", []), body.get("message", ""), calendar=cal, crm=crm)
            if reply == "__CONV_CLOSED__":
                self._json({"reply": "", "closed": True})
            else:
                self._json({"reply": reply})
        elif self.path.startswith("/api/operator"):
            from config import Settings
            from supervisor import supervisor_reply
            settings = Settings.load()
            cal = build_chat_calendar(settings)
            crm = build_chat_crm(settings)
            reply = supervisor_reply(settings, body.get("message", ""), body.get("history", []),
                                     db_path=self.server.db, calendar=cal, crm=crm, linkedin=None)
            self._json({"reply": reply})
        elif self.path.startswith("/api/inject_reply"):
            pilot_inject(body.get("text", ""))
            self._json({"ok": True})
        else:
            self._json({"error": "inconnu"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = None
    for p in range(args.port, args.port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            args.port = p
            break
        except OSError:
            continue
    if srv is None:
        print("Aucun port libre.")
        return
    srv.db = args.db
    url = f"http://localhost:{args.port}"
    print(f"Cockpit de démo : {url}  (Ctrl+C pour arrêter)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
