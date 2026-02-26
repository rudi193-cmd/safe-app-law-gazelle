"""
gazelle_engine.py -- Core engine for Law Gazelle (SAFE-framework legal assistant)

Full-cycle legal assistant: classify issue, extract facts, look up statutes,
fill document templates, return ready-to-print HTML.
"""
from __future__ import annotations
import hashlib, json, sqlite3, time, urllib.request, urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

_fleet_loaded = False

def _load_fleet():
    global _fleet_loaded
    if _fleet_loaded: return
    import sys, os
    p = os.path.normpath("C:/Users/Sean/Documents/GitHub/Willow/core")
    if p not in sys.path: sys.path.insert(0, p)
    try:
        import llm_router as _r; _r.load_keys_from_json(); _fleet_loaded = True
    except Exception: pass

def _ask_fleet(prompt: str, fallback: str = "") -> str:
    _load_fleet()
    try:
        import llm_router
        r = llm_router.ask(prompt, preferred_tier="free")
        if r and r.content: return r.content.strip()
    except Exception: pass
    return fallback

ISSUE_TYPES = {
    "small_claims":     "Small claims court / money owed",
    "landlord_tenant":  "Landlord-tenant dispute (rent, deposit, eviction)",
    "employment":       "Employment dispute (unpaid wages, wrongful termination)",
    "foia":             "Freedom of Information Act request",
    "cease_desist":     "Cease and desist (harassment, IP, debt)",
    "contract_dispute": "Contract breach / demand",
    "consumer":         "Consumer protection / defective product / fraud",
    "other":            "Other legal matter",
}

ISSUE_TEMPLATES = {
    "small_claims":     ["small_claims_demand"],
    "landlord_tenant":  ["security_deposit_demand"],
    "employment":       ["wage_claim_letter"],
    "foia":             ["foia_request"],
    "cease_desist":     ["cease_desist"],
    "contract_dispute": ["small_claims_demand"],
    "consumer":         ["cease_desist"],
    "other":            [],
}

REQUIRED_FACTS = {
    "small_claims":     ["sender_name","sender_address","recipient_name",
                         "recipient_address","amount_owed","reason","jurisdiction"],
    "landlord_tenant":  ["tenant_name","tenant_address","tenant_current_address",
                         "landlord_name","landlord_address","move_out_date","deposit_amount","state"],
    "employment":       ["employee_name","employee_address","employer_name",
                         "employer_address","wages_owed","employment_period","pay_periods","jurisdiction"],
    "foia":             ["sender_name","sender_address","agency_name",
                         "agency_address","description_of_records"],
    "cease_desist":     ["sender_name","sender_address","recipient_name",
                         "recipient_address","conduct_description","demand_description"],
    "contract_dispute": ["sender_name","sender_address","recipient_name",
                         "recipient_address","amount_owed","reason","jurisdiction"],
    "consumer":         ["sender_name","sender_address","recipient_name",
                         "recipient_address","conduct_description","demand_description"],
    "other":            [],
}

DB_PATH = Path(__file__).parent / "gazelle.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS gazelle_sessions (
    id TEXT PRIMARY KEY, user_name TEXT, issue_raw TEXT, issue_type TEXT,
    jurisdiction TEXT, facts_json TEXT DEFAULT \'{}\', status TEXT DEFAULT \'intake\',
    consent_given INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS gazelle_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
    content TEXT, metadata_json TEXT DEFAULT \'{}\', timestamp TEXT
);
CREATE TABLE IF NOT EXISTS gazelle_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, doc_type TEXT,
    doc_title TEXT, content TEXT, status TEXT DEFAULT \'draft\', created_at TEXT
);
"""

def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH)); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;"); return c

def _ensure_schema():
    with _get_conn() as c: c.executescript(_SCHEMA)

_ensure_schema()
def _now(): return datetime.now().isoformat() + "Z"
def _make_id(s): return hashlib.sha1(f"{s}:{time.time()}".encode()).hexdigest()[:12]
def _rd(r): return dict(r)

def create_session(user_name: str) -> dict:
    sid = _make_id(user_name); now = _now()
    with _get_conn() as c:
        c.execute("INSERT INTO gazelle_sessions "
                  "(id,user_name,issue_raw,issue_type,jurisdiction,facts_json,status,consent_given,created_at,updated_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (sid,user_name,None,None,None,"{}","intake",0,now,now))
    return get_session(sid)

def get_session(session_id: str) -> Optional[dict]:
    with _get_conn() as c:
        row = c.execute("SELECT * FROM gazelle_sessions WHERE id=?",(session_id,)).fetchone()
    if not row: return None
    d = _rd(row)
    try: d["facts"] = json.loads(d.get("facts_json") or "{}")
    except: d["facts"] = {}
    return d

def _upd(session_id: str, **kw):
    if not kw: return
    kw["updated_at"] = _now()
    if "facts" in kw: kw["facts_json"] = json.dumps(kw.pop("facts"))
    sql = "UPDATE gazelle_sessions SET " + ", ".join(f"{k}=?" for k in kw) + " WHERE id=?"
    with _get_conn() as c: c.execute(sql, list(kw.values()) + [session_id])

def delete_session(session_id: str) -> bool:
    if not get_session(session_id): return False
    with _get_conn() as c:
        c.execute("DELETE FROM gazelle_documents WHERE session_id=?",(session_id,))
        c.execute("DELETE FROM gazelle_messages WHERE session_id=?",(session_id,))
        c.execute("DELETE FROM gazelle_sessions WHERE id=?",(session_id,))
    return True

def add_message(session_id: str, role: str, content: str, metadata=None) -> int:
    with _get_conn() as c:
        cur = c.execute("INSERT INTO gazelle_messages (session_id,role,content,metadata_json,timestamp) VALUES (?,?,?,?,?)",
                        (session_id,role,content,json.dumps(metadata or {}),_now()))
        return cur.lastrowid

def get_messages(session_id: str, limit: int = 50) -> list:
    with _get_conn() as c:
        rows = c.execute("SELECT * FROM gazelle_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",(session_id,limit)).fetchall()
    msgs = [_rd(r) for r in reversed(rows)]
    for m in msgs:
        try: m["metadata"] = json.loads(m.get("metadata_json") or "{}")
        except: m["metadata"] = {}
    return msgs

def classify_issue(session_id: str, user_description: str) -> dict:
    il = "\n".join(f"  {k}: {v}" for k,v in ISSUE_TYPES.items())
    prompt = ("You are a legal intake classifier.\n\nSituation:\n---\n" + user_description +
              "\n---\n\nIssue types:\n" + il +
              '\n\nRespond ONLY with JSON: {"issue_type":"<key>","jurisdiction":"<state or federal>",' +
              '"confidence":0.0,"clarifying_questions":["q1","q2","q3"]}')
    raw = _ask_fleet(prompt, "")
    result = {"issue_type":"other","jurisdiction":"federal","confidence":0.5,
              "clarifying_questions":["What state?","Names of all parties?","Desired outcome?"]}
    if raw:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1].strip()
            if clean.startswith("json"): clean = clean[4:].strip()
        try:
            p = json.loads(clean)
            if p.get("issue_type") in ISSUE_TYPES: result["issue_type"] = p["issue_type"]
            result["jurisdiction"] = p.get("jurisdiction","federal") or "federal"
            result["confidence"] = float(p.get("confidence",0.5))
            if isinstance(p.get("clarifying_questions"),list): result["clarifying_questions"] = p["clarifying_questions"][:5]
        except: pass
    _upd(session_id, issue_raw=user_description, issue_type=result["issue_type"],
         jurisdiction=result["jurisdiction"], status="clarifying")
    return result

def extract_facts(session_id: str, conversation_text: str) -> dict:
    s = get_session(session_id)
    if not s: return {"facts":{},"missing_fields":[],"complete":False}
    it = s.get("issue_type") or "other"; req = REQUIRED_FACTS.get(it,[])
    prompt = ("Extract legal facts.\nIssue: " + it + "\nRequired: " + json.dumps(req) +
              "\nConversation:\n---\n" + conversation_text + "\n---\nJSON only. Null for missing.")
    raw = _ask_fleet(prompt,"")
    existing = s.get("facts") or {}; new = {}
    if raw:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1].strip()
            if clean.startswith("json"): clean = clean[4:].strip()
        try:
            p = json.loads(clean)
            if isinstance(p,dict): new = {k:v for k,v in p.items() if v is not None}
        except: pass
    merged = {**existing,**new}; missing = [f for f in req if not merged.get(f)]
    _upd(session_id, facts=merged)
    return {"facts":merged,"missing_fields":missing,"complete":len(missing)==0}

def get_required_templates(issue_type: str) -> list:
    return ISSUE_TEMPLATES.get(issue_type,[])

_DISC = ("This document was prepared with AI assistance. "
         "Review with a qualified attorney before submission.")
_CSS = ('<style>body{font-family:"Times New Roman",serif;font-size:12pt;margin:1in;'
        'color:#000;background:#fff;line-height:1.5}'
        '.hd{text-align:right;margin-bottom:24pt}.pa{margin-bottom:18pt}'
        '.su{font-weight:bold;margin-bottom:18pt}p{margin:0 0 12pt}.sg{margin-top:36pt}'
        '.di{margin-top:48pt;padding-top:12pt;border-top:1px solid #999;'
        'font-size:9pt;color:#666;font-style:italic}'
        '@media print{body{margin:1in}}</style>')

def _w(t, b):
    return ('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>' + t + '</title>'
            + _CSS + '</head><body>' + b
            + '<div class="di">' + _DISC + '</div></body></html>')
def _e(v,df="[Not provided]"):
    if v is None: return df
    return str(v).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
def _a(v): return _e(v).replace(", ","<br>")
def _f(d,k,df="[Not provided]"): return _e(d.get(k) or None,df)

def _small_claims(f):
    t = "Small Claims Demand Letter"; td = datetime.now().strftime("%B %d, %Y")
    dl = _e(f.get("deadline_days","14"))
    b = ("".join([
        '<div class="hd">'+_f(f,"date",td)+'</div>',
        '<div class="pa"><strong>FROM:</strong><br>'+_f(f,"sender_name")+'<br>'+_a(f.get("sender_address",""))+'<br><br>',
        '<strong>TO:</strong><br>'+_f(f,"recipient_name")+'<br>'+_a(f.get("recipient_address",""))+'</div>',
        '<div class="su">RE: Formal Demand for Payment of $'+_f(f,"amount_owed","[Amount]")+'</div>',
        '<p>Dear '+_f(f,"recipient_name")+',</p>',
        '<p>This letter constitutes formal notice that you owe me <strong>$'+_f(f,"amount_owed","[Amount]")+'</strong> for: '+_f(f,"reason","[reason]")+'.</p>',
        '<p>Demand is hereby made for payment in full within <strong>'+dl+' days</strong>. Failure may result in a small claims court filing.</p>',
        '<div class="sg">Sincerely,<br><br><br>____________________________<br>'+_f(f,"sender_name")+'<br>'+_f(f,"date",td)+'</div>',
    ]))
    return t, _w(t,b)

def _foia(f):
    t = "Freedom of Information Act Request"; td = datetime.now().strftime("%B %d, %Y")
    ep = ('<p>Expedited processing requested: '+_e(f.get("expedite_reason",""))+'</p>') if f.get("expedite_reason") else ""
    b = ("".join([
        '<div class="hd">'+_f(f,"date",td)+'</div>',
        '<div class="pa"><strong>FROM:</strong><br>'+_f(f,"sender_name")+'<br>'+_a(f.get("sender_address",""))+'<br><br>',
        '<strong>TO:</strong><br>FOIA Officer<br>'+_f(f,"agency_name")+'<br>'+_a(f.get("agency_address",""))+'</div>',
        '<div class="su">RE: FOIA Request &#8212; 5 U.S.C. &#167; 552</div>',
        '<p>Dear FOIA Officer,</p>',
        '<p>Pursuant to 5 U.S.C. &#167; 552, I request the following records from '+_f(f,"agency_name")+':</p>',
        '<p><em>'+_f(f,"description_of_records","[describe records]")+'</em></p>',ep,
        '<p>Willing to pay reasonable fees up to $25. Notify me if higher. Please respond within 20 business days.</p>',
        '<div class="sg">Sincerely,<br><br><br>____________________________<br>'+_f(f,"sender_name")+'<br>'+_f(f,"date",td)+'</div>',
    ]))
    return t, _w(t,b)

def _deposit(f):
    t = "Security Deposit Return Demand"; td = datetime.now().strftime("%B %d, %Y")
    st = _e(f.get("state","your state")); dl = _e(f.get("deadline_days","14"))
    b = ("".join([
        '<div class="hd">'+_f(f,"date",td)+'</div>',
        '<div class="pa"><strong>FROM:</strong><br>'+_f(f,"tenant_name")+'<br>'+_a(f.get("tenant_current_address",""))+'<br><br>',
        '<strong>TO:</strong><br>'+_f(f,"landlord_name")+'<br>'+_a(f.get("landlord_address",""))+'</div>',
        '<div class="su">RE: Security Deposit Return &#8212; $'+_f(f,"deposit_amount","[Amount]")+'</div>',
        '<p>Dear '+_f(f,"landlord_name")+',</p>',
        '<p>I demand return of my security deposit of <strong>$'+_f(f,"deposit_amount","[Amount]")+'</strong> for '+_f(f,"tenant_address")+'.',
        ' I vacated on '+_f(f,"move_out_date","[date]")+' and left the property in good condition.</p>',
        '<p>Under '+st+' law, landlords must return the deposit within the statutory period. You have not done so.</p>',
        '<p>Please return the full deposit within <strong>'+dl+' days</strong> or I will pursue legal action.</p>',
        '<div class="sg">Sincerely,<br><br><br>____________________________<br>'+_f(f,"tenant_name")+'<br>'+_f(f,"date",td)+'</div>',
    ]))
    return t, _w(t,b)

def _cease(f):
    t = "Cease and Desist Letter"; td = datetime.now().strftime("%B %d, %Y")
    dl = _e(f.get("deadline_days","10"))
    b = ("".join([
        '<div class="hd">'+_f(f,"date",td)+'</div>',
        '<div class="pa"><strong>FROM:</strong><br>'+_f(f,"sender_name")+'<br>'+_a(f.get("sender_address",""))+'<br><br>',
        '<strong>TO:</strong><br>'+_f(f,"recipient_name")+'<br>'+_a(f.get("recipient_address",""))+'</div>',
        '<div class="su">RE: CEASE AND DESIST</div>',
        '<p>Dear '+_f(f,"recipient_name")+',</p>',
        '<p>The following conduct must cease immediately:</p>',
        '<p><strong>'+_f(f,"conduct_description","[conduct]")+'</strong></p>',
        '<p>You are demanded to: '+_f(f,"demand_description","[demand]")+'</p>',
        '<p>Comply within <strong>'+dl+' days</strong> or face civil legal action.</p>',
        '<div class="sg">Sincerely,<br><br><br>____________________________<br>'+_f(f,"sender_name")+'<br>'+_f(f,"date",td)+'</div>',
    ]))
    return t, _w(t,b)

def _wages(f):
    t = "Unpaid Wages Demand Letter"; td = datetime.now().strftime("%B %d, %Y")
    b = ("".join([
        '<div class="hd">'+_f(f,"date",td)+'</div>',
        '<div class="pa"><strong>FROM:</strong><br>'+_f(f,"employee_name")+'<br>'+_a(f.get("employee_address",""))+'<br><br>',
        '<strong>TO:</strong><br>'+_f(f,"employer_name")+'<br>'+_a(f.get("employer_address",""))+'</div>',
        '<div class="su">RE: Demand for Unpaid Wages &#8212; $'+_f(f,"wages_owed","[Amount]")+'</div>',
        '<p>Dear '+_f(f,"employer_name")+',</p>',
        '<p>I demand payment of <strong>$'+_f(f,"wages_owed","[Amount]")+'</strong> for '+_f(f,"employment_period","[period]")+' ('+_f(f,"pay_periods","[pay periods]")+').</p>',
        '<p>Under the FLSA (29 U.S.C. &#167; 201 et seq.) all earned wages must be paid on scheduled dates.</p>',
        '<p>Payment required within <strong>14 days</strong>. Failure may result in DOL complaint and civil action.</p>',
        '<div class="sg">Sincerely,<br><br><br>____________________________<br>'+_f(f,"employee_name")+'<br>'+_f(f,"date",td)+'</div>',
    ]))
    return t, _w(t,b)

_TB = {
    "small_claims_demand":     _small_claims,
    "foia_request":            _foia,
    "security_deposit_demand": _deposit,
    "cease_desist":            _cease,
    "wage_claim_letter":       _wages,
}

def fill_document(session_id: str, template_key: str, facts: dict) -> dict:
    b = _TB.get(template_key)
    if not b: return {"error": f"Unknown template: {template_key}"}
    title, html = b(facts)
    with _get_conn() as c:
        cur = c.execute("INSERT INTO gazelle_documents (session_id,doc_type,doc_title,content,status,created_at) VALUES (?,?,?,?,'draft',?)",
                        (session_id,template_key,title,html,_now()))
        return {"doc_id":cur.lastrowid,"title":title,"content":html,"status":"draft"}

def get_documents(session_id: str) -> list:
    with _get_conn() as c:
        rows = c.execute("SELECT * FROM gazelle_documents WHERE session_id=? ORDER BY id ASC",(session_id,)).fetchall()
    return [_rd(r) for r in rows]

def lookup_statute(query: str, jurisdiction: str = "federal") -> dict:
    results = []
    if jurisdiction in ("federal","us","usa"):
        try:
            enc = urllib.parse.quote(query[:100])
            url = f"https://www.ecfr.gov/api/search/v1/results?query={enc}&per_page=3"
            req = urllib.request.Request(url, headers={"Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode())
                for item in (data.get("results") or [])[:3]:
                    results.append({"title":item.get("label_description",""),"citation":item.get("citation",""),
                                    "url":"https://www.ecfr.gov"+item.get("full_text_excerpt_url",""),
                                    "summary":item.get("full_text_excerpt","")})
        except: pass
    if not results:
        txt = _ask_fleet("Briefly describe the most relevant US federal or "+jurisdiction+" statute(s) for: "+query+". Name, citation, 1-2 sentence summary. Plain text.","")
        if txt: results.append({"title":"Applicable Law (AI Summary)","citation":"","url":"","summary":txt})
    return {"results":results,"query":query,"jurisdiction":jurisdiction}

def explain_law(statute_text: str, issue_context: str) -> str:
    return _ask_fleet("Plain-language explanation.\n\nLegal text:\n"+statute_text[:2000]+
                      "\n\nSituation: "+issue_context+"\n\nExplain in 2-3 simple sentences.",
                      "Explanation unavailable. Please consult an attorney.")

_ML = {
    "sender_name":"your full name","recipient_name":"the other party's full name",
    "sender_address":"your mailing address","recipient_address":"their address",
    "amount_owed":"the exact dollar amount","reason":"why they owe you",
    "jurisdiction":"what state this is in","state":"what state this is in",
    "tenant_name":"your full name (as tenant)","landlord_name":"the landlord's full name",
    "deposit_amount":"the security deposit amount","move_out_date":"your move-out date",
    "tenant_current_address":"your current mailing address","tenant_address":"the rental property address",
    "employee_name":"your full name","employer_name":"the employer's full name",
    "wages_owed":"the amount of unpaid wages","employment_period":"your employment dates",
    "pay_periods":"which pay periods are missing","employer_address":"the employer's address",
    "employee_address":"your mailing address","agency_name":"the agency's name",
    "description_of_records":"what records you want","conduct_description":"what conduct to stop",
    "demand_description":"what you want them to do",
}

def process_message(session_id: str, user_message: str) -> dict:
    s = get_session(session_id)
    if not s: return {"response":"Session not found.","status":"error","documents_ready":False,"documents":[]}
    add_message(session_id,"user",user_message)
    status = s.get("status","intake")

    if status == "intake":
        clf = classify_issue(session_id, user_message)
        it = clf["issue_type"]
        qs = clf.get("clarifying_questions") or ["What state?","Names of parties?","Desired outcome?"]
        qt = "\n".join("\u2022 "+q for q in qs[:4])
        law = lookup_statute(user_message, clf.get("jurisdiction","federal"))
        ln = ""
        if law["results"]:
            top = law["results"][0]; sm=(top.get("summary") or "")[:200]
            ln = "\n\nRelevant law: **"+top["title"]+"**"+((" \u2014 "+sm) if sm else "")
        resp = ("I understand \u2014 it sounds like you have a **"+ISSUE_TYPES.get(it,it)+"** situation."+ln+
                "\n\nTo prepare your documents I need a few details:\n\n"+qt+"\n\nAnswer as many as you can.")
        add_message(session_id,"gazelle",resp)
        return {"response":resp,"status":"clarifying","documents_ready":False,"documents":[]}

    if status == "clarifying":
        msgs = get_messages(session_id,20)
        conv = "\n".join(("User" if m["role"]=="user" else "Gazelle")+": "+m["content"] for m in msgs)
        ext = extract_facts(session_id,conv); missing = ext.get("missing_fields",[])
        if missing and len(missing) > 2:
            ask = [_ML.get(f,f) for f in missing[:3]]
            q = (", ".join(ask[:-1])+" and "+ask[-1]) if len(ask)>1 else ask[0]
            resp = "Thank you \u2014 just a few more details: **"+q+"**."
            add_message(session_id,"gazelle",resp)
            return {"response":resp,"status":"clarifying","documents_ready":False,"documents":[]}
        _upd(session_id,status="drafting")
        s = get_session(session_id); facts = s.get("facts") or {}
        facts["date"] = datetime.now().strftime("%B %d, %Y")
        it = s.get("issue_type") or "other"
        docs = [fill_document(session_id,t,facts) for t in get_required_templates(it)]
        _upd(session_id,status="complete")
        if docs:
            names = ", ".join(d["title"] for d in docs)
            resp = ("Your documents are ready: **"+names+"**.\n\nReview carefully before signing. "
                    "Fields marked [Not provided] need your attention.\n\n"
                    "\u26a0\ufe0f _AI-assisted \u2014 review with an attorney before submitting._")
        else:
            resp = "This situation has no standard template. Contact a local legal aid organization."
        add_message(session_id,"gazelle",resp)
        return {"response":resp,"status":"complete","documents_ready":bool(docs),"documents":docs}

    if status == "complete":
        s = get_session(session_id); msgs = get_messages(session_id,30)
        conv = "\n".join(("User" if m["role"]=="user" else "Gazelle")+": "+m["content"] for m in msgs)
        ext = extract_facts(session_id,conv)
        facts = ext.get("facts") or s.get("facts") or {}
        facts["date"] = datetime.now().strftime("%B %d, %Y")
        it = s.get("issue_type") or "other"; tmps = get_required_templates(it)
        if tmps:
            with _get_conn() as c: c.execute("DELETE FROM gazelle_documents WHERE session_id=?",(session_id,))
            docs = [fill_document(session_id,t,facts) for t in tmps]
        else:
            docs = get_documents(session_id)
        resp = _ask_fleet("You are Gazelle. User said: '"+user_message+"'. Documents updated. Reply in 1-2 warm professional sentences.",
                          "Your documents have been updated. Please review before submitting.")
        add_message(session_id,"gazelle",resp)
        return {"response":resp,"status":"complete","documents_ready":bool(docs),"documents":docs}

    resp = "Tell me about your legal situation and I'll help you prepare documents."
    add_message(session_id,"gazelle",resp)
    return {"response":resp,"status":status,"documents_ready":False,"documents":[]}
