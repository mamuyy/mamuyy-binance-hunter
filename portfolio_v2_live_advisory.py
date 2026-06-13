"""Phase 3.01B: immutable Portfolio V2 research baseline + fresh SQLite live overlay."""
from __future__ import annotations
import argparse, csv, glob, hashlib, json, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import portfolio_v2_advisory_pipeline as base

REPORT_PATH="logs/portfolio_v2_live_advisory_report.json"
PREVIEW_PATH="logs/portfolio_v2_live_advisory_preview.json"
SEND_RESULT_PATH="logs/portfolio_v2_live_advisory_send_result.json"
STATE_PATH="logs/portfolio_v2_live_advisory_send_state.json"
ALLOW_SEND_ENV="ALLOW_PORTFOLIO_V2_TELEGRAM_SEND"
HB_MAX_AGE=15; SIGNAL_MAX_AGE=180; COOLDOWN=21600
OPEN={"OPEN","ACTIVE","TP1 HIT","TP1_HIT","PARTIAL","PARTIALLY_CLOSED"}
LINEAGE_PATTERNS=("logs/*report*.json","reports/*report*.json")

def utcnow(): return datetime.now(timezone.utc)
def parse_time(v):
    if not v: return None
    try: d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except ValueError: return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
def age(v,now):
    d=parse_time(v); return None if d is None else round(max(0,(now-d).total_seconds()/60),2)
def sha(path):
    if not path or not os.path.isfile(path): return None
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1048576),b""): h.update(chunk)
    return h.hexdigest()
def keys(path):
    if not path:return set()
    p=os.path.normpath(str(path));return {p,os.path.basename(p)}
def reports(patterns:Sequence[str]=LINEAGE_PATTERNS):
    out=[];seen=set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            if path in seen or not os.path.isfile(path):continue
            seen.add(path);payload=base.read_json(path)
            if isinstance(payload,dict) and payload.get("output_csv") and payload.get("source_csv"):out.append((path,payload))
    return out
def cutoff(path):
    if not path or not os.path.isfile(path) or Path(path).suffix.lower()!=".csv":return None
    try:
        with open(path,newline="",encoding="utf-8") as f:
            r=csv.DictReader(f);cols=[c for c in ("close_timestamp","signal_timestamp","updated_at","timestamp") if c in (r.fieldnames or [])];latest=None
            for row in r:
                for c in cols:
                    d=parse_time(row.get(c))
                    if d and (latest is None or d>latest):latest=d
            return latest.replace(microsecond=0).isoformat() if latest else None
    except OSError:return None
def lineage(path):
    rs=reports();nodes=[];seen=set();cur=path
    while cur and len(nodes)<12:
        norm=os.path.normpath(cur)
        if norm in seen:break
        seen.add(norm);producer=next(((p,x) for p,x in rs if keys(cur)&keys(x.get("output_csv"))),None)
        node={"artifact_path":cur,"sha256":sha(cur),"phase":None,"report_path":None,"source_path":None}
        if producer:
            p,x=producer;node.update({"phase":x.get("phase"),"report_path":p,"source_path":str(x.get("source_csv"))});cur=node["source_path"]
        else:cur=None
        nodes.append(node)
    terminal=nodes[-1]["artifact_path"] if nodes else None
    return {"nodes":nodes,"depth":len(nodes),"terminal_source":terminal,"data_cutoff":cutoff(terminal),"fingerprint_sha256":hashlib.sha256(json.dumps(nodes,sort_keys=True).encode()).hexdigest()}
def research_baseline(path,now):
    path=path or base.discover_latest(base.ALLOCATION_PATTERNS)
    meta=base.source_metadata(path,now);ln=lineage(path) if path else {"nodes":[],"depth":0,"terminal_source":None,"data_cutoff":None,"fingerprint_sha256":None}
    alloc,errors=base.canonicalize_allocations(base.load_records(path)) if path else ([],[])
    reasons=[]
    if not path or not meta.get("available"):reasons.append("Portfolio V2 research baseline not found")
    if len(alloc)<base.MIN_VALID_SYMBOLS:reasons.append(f"research baseline has only {len(alloc)} valid symbols; minimum {base.MIN_VALID_SYMBOLS}")
    reasons.extend(errors);total=base.allocation_total(alloc)
    if len(alloc)>=base.MIN_ROWS_FOR_TOTAL_SANITY and not (base.TOTAL_ALLOCATION_MIN<=total<=base.TOTAL_ALLOCATION_MAX):reasons.append(f"allocation total {total:.2f}% outside {base.TOTAL_ALLOCATION_MIN:.0f}..{base.TOTAL_ALLOCATION_MAX:.0f}%")
    valid=not reasons
    return {"status":"BASELINE_READY" if valid else "BLOCKED_BASELINE","blocked_reasons":reasons,"path":path,"baseline_sha256":sha(path),"file_modified_at":meta.get("modified_at"),"file_age_minutes":meta.get("age_minutes"),"age_policy":"INFORMATIONAL_ONLY_REBUILD_WHEN_SOURCE_DATA_CHANGES","rows":len(alloc),"allocation_total_pct":round(total,2),"all_allocations":alloc,"portfolio_health":base.derived_health(alloc),"lineage_depth":ln["depth"],"lineage_fingerprint_sha256":ln["fingerprint_sha256"],"terminal_source":ln["terminal_source"],"data_cutoff":ln["data_cutoff"],"lineage":ln["nodes"]}
def table_exists(c,t):return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone() is not None
def latest(c,t):
    if not table_exists(c,t):return None
    r=c.execute(f"SELECT * FROM {t} ORDER BY id DESC LIMIT 1").fetchone();return dict(r) if r else None
def latest_signals(c):
    if not table_exists(c,"signals"):return []
    q="SELECT s.* FROM signals s JOIN (SELECT symbol,MAX(id) max_id FROM signals GROUP BY symbol)x ON x.max_id=s.id WHERE s.symbol IS NOT NULL AND s.symbol!='' ORDER BY s.id DESC"
    return [dict(r) for r in c.execute(q).fetchall()]
def exposures(c):
    table="paper_trades" if table_exists(c,"paper_trades") else ("internal_paper_trades" if table_exists(c,"internal_paper_trades") else None)
    if not table:return {},[]
    out={}
    for r in c.execute(f"SELECT symbol,status FROM {table}"):
        s=base.normalize_symbol(r["symbol"]);st=str(r["status"] or "").strip().upper()
        if s and st in OPEN:out[s]=out.get(s,0)+1
    return out,[table]
def live_overlay(db,now,hb_limit,signal_limit):
    if not os.path.isfile(db):return {"status":"BLOCKED_LIVE_OVERLAY","blocked_reasons":[f"runtime database not found: {db}"],"database_path":db}
    try:
        with sqlite3.connect(db) as c:
            c.row_factory=sqlite3.Row;hb=latest(c,"runtime_heartbeats");reg=latest(c,"regime_logs");rows=latest_signals(c);exp,sources=exposures(c)
    except sqlite3.Error as e:return {"status":"BLOCKED_LIVE_OVERLAY","blocked_reasons":[f"runtime database read failed: {e}"],"database_path":db}
    blocked=[];hb_age=age((hb or {}).get("timestamp"),now)
    if hb_age is None:blocked.append("runtime heartbeat missing or invalid")
    elif hb_age>hb_limit:blocked.append(f"runtime heartbeat stale: {hb_age} minutes; limit {hb_limit}")
    signals={};latest_ts=None
    for row in rows:
        s=base.normalize_symbol(row.get("symbol"));d=parse_time(row.get("timestamp"))
        if not s:continue
        signals[s]=row
        if d and (latest_ts is None or d>latest_ts):latest_ts=d
    sig_age=round(max(0,(now-latest_ts).total_seconds()/60),2) if latest_ts else None
    if sig_age is None:blocked.append("current signal snapshot missing or invalid")
    elif sig_age>signal_limit:blocked.append(f"current signals stale: {sig_age} minutes; limit {signal_limit}")
    if not sources:blocked.append("paper exposure tables missing")
    return {"status":"BLOCKED_LIVE_OVERLAY" if blocked else "LIVE_READY","blocked_reasons":blocked,"database_path":db,"heartbeat":hb,"heartbeat_age_minutes":hb_age,"regime":reg,"signals":signals,"signal_age_minutes":sig_age,"active_exposures":exp,"active_exposure_count":sum(exp.values()),"exposure_sources":sources}
def score(signal):return base.safe_float(base.first_value(signal or {},("adaptive_confidence_score","calculated_score","score","shadow_score")))
def rank(alloc,live):
    sig=live.get("signals") or {};exp=live.get("active_exposures") or {};order={"TOP_WATCH":0,"WATCH":1,"HOLD_NO_ADD":2,"WAIT_LOW_SCORE":3,"WAIT_NO_LIVE_SIGNAL":4};out=[]
    for item in alloc:
        s=item["symbol"];row=sig.get(s);sc=score(row);n=int(exp.get(s,0))
        action="HOLD_NO_ADD" if n else "WAIT_NO_LIVE_SIGNAL" if row is None else "TOP_WATCH" if sc is not None and sc>=70 else "WATCH" if sc is not None and sc>=55 else "WAIT_LOW_SCORE"
        out.append({**item,"live_score":sc,"live_regime":(row or {}).get("regime_name") or "UNKNOWN","active_exposure_count":n,"advisory_action":action})
    return sorted(out,key=lambda x:(order[x["advisory_action"]],-(x["live_score"] if x["live_score"] is not None else -1),-x["allocation_pct"],x["symbol"]))
def render(r):
    b=r["research_baseline"];l=r["live_overlay"];h=b["portfolio_health"];reg=(l.get("regime") or {}).get("regime_name") or "UNKNOWN"
    lines=["📦 PORTFOLIO ENGINE V2 — LIVE ADVISORY","",f"Status: {r['status']}",f"Generated: {r['generated_at']}",f"Baseline ID: {(b.get('baseline_sha256') or 'NONE')[:12]}",f"Baseline Data Cutoff: {b.get('data_cutoff') or 'UNKNOWN'}",f"Baseline File Age: {b.get('file_age_minutes')} minutes (informational)",f"Lineage Depth: {b.get('lineage_depth')}",f"Allocation Total: {b.get('allocation_total_pct',0):.2f}%","",f"Portfolio Health: {h.get('portfolio_health')}",f"Risk Score: {h.get('risk_score',0):.2f}/100",f"Diversification: {h.get('diversification_score',0):.2f}/100","",f"Live Heartbeat Age: {l.get('heartbeat_age_minutes')} minutes",f"Live Signal Age: {l.get('signal_age_minutes')} minutes",f"Current Regime: {reg}",f"Active Paper Exposures: {l.get('active_exposure_count',0)}"]
    if r["status"]!="READY":lines += ["","⛔ Advisory blocked:"]+[f"- {x}" for x in r["blocked_reasons"]]
    else:
        watch=[x for x in r["overlay_rankings"] if x["advisory_action"] in {"TOP_WATCH","WATCH"}][:10];hold=[x for x in r["overlay_rankings"] if x["advisory_action"]=="HOLD_NO_ADD"][:10]
        lines += ["","🟢 Top Live Watch:"]+([f"{i}. {x['symbol']} — Baseline {x['allocation_pct']:.2f}% | Live Score {x['live_score']:.2f} | {x['advisory_action']}" for i,x in enumerate(watch,1)] or ["- none"])+["","🟡 Hold / Do Not Add:"]+([f"- {x['symbol']} | Active Exposure {x['active_exposure_count']} | Baseline {x['allocation_pct']:.2f}%" for x in hold] or ["- none"])
    return "\n".join(lines+["","Mode: V2_ADVISORY_ONLY","Research Baseline Mutated: NO","Runtime V1 Changed: NO","Broker Routing: NO","Order Attempted: NO",f"Baseline Source: {b.get('path')}",f"Live Source: {l.get('database_path')}"])
def build_report(allocation_path=None,database_path=None,heartbeat_max_age_minutes=HB_MAX_AGE,signal_max_age_minutes=SIGNAL_MAX_AGE,now=None):
    current=now or utcnow();research=research_baseline(allocation_path,current);db=database_path or getattr(base.config,"database_path","mamuyy_hunter.db");live=live_overlay(db,current,heartbeat_max_age_minutes,signal_max_age_minutes);safe,active=base.execution_gates_safe();blocked=list(research.get("blocked_reasons") or [])+list(live.get("blocked_reasons") or [])
    if research["status"]!="BASELINE_READY":status="BLOCKED_BASELINE"
    elif live["status"]!="LIVE_READY":status="BLOCKED_LIVE_OVERLAY"
    elif not safe:status="BLOCKED_EXECUTION_GATES_ACTIVE";blocked.append("execution-related environment gates active: "+", ".join(active))
    else:status="READY"
    r={"generated_at":current.replace(microsecond=0).isoformat(),"phase":"3.01B","status":status,"blocked_reasons":blocked,"research_baseline":research,"live_overlay":live,"overlay_rankings":rank(research.get("all_allocations") or [],live),"execution_gates_safe":safe,"active_execution_gates":active,"mode":"V2_ADVISORY_ONLY","runtime_v1_changed":False,"research_baseline_mutated":False,"broker_routing_enabled":False,"order_attempted":False};r["payload_text"]=render(r);r["payload_sha256"]=hashlib.sha256(r["payload_text"].encode()).hexdigest();return r
def send_or_preview(r,send,dry_run,cooldown,state_path):
    state=base.load_state(state_path);ok,why=base.cooldown_passed(state,r["payload_sha256"],cooldown);status="PREVIEW_ONLY";reason=None;attempted=success=False
    if dry_run:status,reason="BLOCKED_DRY_RUN","--dry-run supplied"
    elif not send:reason="send flag not supplied"
    elif r["status"]!="READY":status,reason="BLOCKED_REPORT","; ".join(r["blocked_reasons"])
    elif os.getenv(ALLOW_SEND_ENV)!="1":status,reason="BLOCKED_MANUAL_GATE",f"{ALLOW_SEND_ENV} must be 1"
    elif not ok:status,reason="BLOCKED_COOLDOWN",why
    else:
        attempted=True;success=base.send_telegram(r["payload_text"]);status="SENT" if success else "ERROR";reason=None if success else "Telegram request failed"
        if success:base.write_json(state_path,{"last_sent_at":base.utc_now_iso(),"payload_sha256":r["payload_sha256"],"baseline_sha256":r["research_baseline"].get("baseline_sha256")})
    return {"generated_at":base.utc_now_iso(),"status":status,"blocked_reason":reason,"send_attempted":attempted,"send_success":success,"cooldown_passed":ok,"report_status":r["status"],"runtime_v1_changed":False,"broker_routing_enabled":False,"order_attempted":False}
def main():
    p=argparse.ArgumentParser(description="Portfolio V2 research baseline + live overlay advisory");p.add_argument("--allocation-path");p.add_argument("--database-path",default=getattr(base.config,"database_path","mamuyy_hunter.db"));p.add_argument("--heartbeat-max-age-minutes",type=int,default=HB_MAX_AGE);p.add_argument("--signal-max-age-minutes",type=int,default=SIGNAL_MAX_AGE);p.add_argument("--report-path",default=REPORT_PATH);p.add_argument("--preview-path",default=PREVIEW_PATH);p.add_argument("--send-result-path",default=SEND_RESULT_PATH);p.add_argument("--state-path",default=STATE_PATH);p.add_argument("--send",action="store_true");p.add_argument("--dry-run",action="store_true");p.add_argument("--cooldown-seconds",type=int,default=COOLDOWN);a=p.parse_args();r=build_report(a.allocation_path,a.database_path,a.heartbeat_max_age_minutes,a.signal_max_age_minutes);base.write_json(a.report_path,r);base.write_json(a.preview_path,{"generated_at":r["generated_at"],"status":r["status"],"payload_text":r["payload_text"],"payload_sha256":r["payload_sha256"],"baseline_sha256":r["research_baseline"].get("baseline_sha256"),"runtime_v1_changed":False,"broker_execution_enabled":False,"order_attempted":False});s=send_or_preview(r,a.send,a.dry_run,a.cooldown_seconds,a.state_path);base.write_json(a.send_result_path,s);print(r["payload_text"]);print(f"Telegram Result: {s['status']}");return 1 if s["status"]=="ERROR" else 0
if __name__=="__main__":raise SystemExit(main())
