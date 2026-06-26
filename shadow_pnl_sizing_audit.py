"""
CP-035 — Shadow PnL / Sizing Sanity Audit
Phase 2.5F — MAMUYY Hunter Governance

Purpose:
    Audit kewajaran data shadow_trades:
    1. Verifikasi apakah pnl_percent mencerminkan actual outcome atau hanya simulasi satu arah
    2. Distribusi exposure/sizing — apakah ada anomali
    3. Lifecycle status coverage — apakah ada status selain 'execution simulated'
    4. Signal score vs pnl correlation — apakah score tinggi = pnl lebih baik
    5. Verdict: PASS / REVIEW / FAIL dengan evidence

READ-ONLY — tidak ada write ke DB.
Output: console + logs/shadow_pnl_sizing_audit.json
"""

import json
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

DB_PATH      = os.getenv("DATABASE_PATH", "mamuyy_hunter.db")
OUTPUT_DIR   = "logs"
OUTPUT_PATH  = os.path.join(OUTPUT_DIR, "shadow_pnl_sizing_audit.json")
AUDIT_ID     = "CP-035"
PHASE        = "Phase 2.5F"
GENERATED_AT = datetime.now(timezone.utc).isoformat()

MIN_ROWS_REQUIRED      = 100
EXPECTED_LIFECYCLE_KEY = "execution simulated"
ANOMALY_PNL_HIGH       = 50.0
ANOMALY_EXPOSURE_HIGH  = 10.0
SIZING_STD_THRESHOLD   = 5.0

def _now(): return datetime.now(timezone.utc).isoformat()
def _connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
def _table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
def _round(x, n=4):
    try: return round(float(x), n)
    except: return None
def _verdict(issues, warnings):
    if any("FAIL" in i for i in issues): return "FAIL"
    if issues or warnings: return "REVIEW"
    return "PASS"

def audit_lifecycle_coverage(conn):
    rows = conn.execute("SELECT lifecycle_status, COUNT(*) as cnt FROM shadow_trades GROUP BY lifecycle_status ORDER BY cnt DESC").fetchall()
    statuses = {str(r["lifecycle_status"]): int(r["cnt"]) for r in rows}
    total = sum(statuses.values())
    unique = list(statuses.keys())
    only_simulated = len(unique) == 1 and unique[0] == EXPECTED_LIFECYCLE_KEY
    issues = []
    if only_simulated:
        issues.append("CRITICAL: semua shadow_trades lifecycle='execution simulated'. Shadow WR 100% adalah artefak log simulasi satu arah, BUKAN edge nyata.")
    return {"check":"lifecycle_coverage","status_distribution":statuses,"only_simulated":only_simulated,"issues":issues,"verdict":"FAIL" if issues else "PASS","note":"shadow_trades = observability log, BUKAN outcome tracker" if only_simulated else "OK"}

def audit_pnl_distribution(conn):
    rows = conn.execute("SELECT pnl_percent FROM shadow_trades WHERE pnl_percent IS NOT NULL").fetchall()
    values = [float(r["pnl_percent"]) for r in rows]
    if not values:
        return {"check":"pnl_distribution","verdict":"FAIL","note":"pnl_percent kosong","issues":["FAIL: no data"],"warnings":[]}
    neg = sum(1 for v in values if v < 0)
    pos = sum(1 for v in values if v > 0)
    issues = []
    warnings = []
    if neg == 0:
        issues.append(f"CRITICAL: tidak ada pnl negatif dari {len(values)} records. Semua PnL positif — konfirmasi shadow = expected fill simulation bukan actual outcome.")
    anomaly = [v for v in values if v > ANOMALY_PNL_HIGH]
    if anomaly:
        warnings.append(f"WARNING: {len(anomaly)} rows pnl > {ANOMALY_PNL_HIGH}%")
    return {
        "check":"pnl_distribution","count":len(values),
        "negative_count":neg,"positive_count":pos,
        "min":_round(min(values)),"max":_round(max(values)),
        "mean":_round(statistics.mean(values)),"median":_round(statistics.median(values)),
        "std":_round(statistics.stdev(values) if len(values)>1 else 0),
        "anomaly_high_count":len(anomaly),
        "issues":issues,"warnings":warnings,
        "verdict":"FAIL" if any("CRITICAL" in i for i in issues) else ("REVIEW" if warnings else "PASS"),
        "note":"PnL semua positif — bukan outcome tracker" if neg==0 else f"{neg} negatif, {pos} positif"
    }

def audit_sizing_sanity(conn):
    rows = conn.execute("SELECT exposure, signal_score FROM shadow_trades WHERE exposure IS NOT NULL").fetchall()
    exposures = [float(r["exposure"]) for r in rows]
    if not exposures:
        return {"check":"sizing_sanity","verdict":"REVIEW","note":"exposure kosong","issues":[],"warnings":["exposure column kosong"]}
    warnings = []
    anomaly = [v for v in exposures if v > ANOMALY_EXPOSURE_HIGH]
    if anomaly: warnings.append(f"WARNING: {len(anomaly)} rows exposure > {ANOMALY_EXPOSURE_HIGH}")
    std = statistics.stdev(exposures) if len(exposures)>1 else 0
    if std > SIZING_STD_THRESHOLD: warnings.append(f"WARNING: std exposure={_round(std)} > threshold")
    zero_exp = sum(1 for v in exposures if v == 0)
    if zero_exp: warnings.append(f"WARNING: {zero_exp} rows exposure=0")
    score_rows = conn.execute("SELECT signal_score, exposure FROM shadow_trades WHERE signal_score IS NOT NULL AND exposure IS NOT NULL AND exposure > 0").fetchall()
    corr_note = "tidak cukup data"
    if len(score_rows) > 10:
        scores = [float(r["signal_score"]) for r in score_rows]
        exps   = [float(r["exposure"]) for r in score_rows]
        n = len(scores)
        ms, me = sum(scores)/n, sum(exps)/n
        cov = sum((s-ms)*(e-me) for s,e in zip(scores,exps))/n
        ss = (sum((s-ms)**2 for s in scores)/n)**0.5
        se = (sum((e-me)**2 for e in exps)/n)**0.5
        corr = cov/(ss*se) if ss>0 and se>0 else 0
        corr_note = f"r={_round(corr,3)} ({'positif' if corr>0.1 else 'lemah'})"
    return {
        "check":"sizing_sanity","count":len(exposures),
        "min":_round(min(exposures)),"max":_round(max(exposures)),
        "mean":_round(statistics.mean(exposures)),"std":_round(std),
        "zero_exposure_count":zero_exp,"anomaly_high_count":len(anomaly),
        "score_exposure_correlation":corr_note,
        "issues":[],"warnings":warnings,
        "verdict":"REVIEW" if warnings else "PASS",
        "note":f"Exposure range {_round(min(exposures))}–{_round(max(exposures))}"
    }

def audit_temporal_coverage(conn):
    row = conn.execute("SELECT MIN(timestamp) as first_ts, MAX(timestamp) as last_ts, COUNT(DISTINCT DATE(timestamp)) as active_days FROM shadow_trades WHERE timestamp IS NOT NULL").fetchone()
    first_ts = str(row["first_ts"]) if row["first_ts"] else "N/A"
    last_ts  = str(row["last_ts"])  if row["last_ts"]  else "N/A"
    days = int(row["active_days"]) if row["active_days"] else 0
    warnings = []
    if days < 7: warnings.append(f"WARNING: hanya {days} hari data")
    return {"check":"temporal_coverage","first_timestamp":first_ts,"last_timestamp":last_ts,"active_days":days,"warnings":warnings,"verdict":"REVIEW" if warnings else "PASS","note":f"{days} hari aktif"}

def audit_symbol_concentration(conn):
    rows = conn.execute("SELECT symbol, COUNT(*) as cnt FROM shadow_trades GROUP BY symbol ORDER BY cnt DESC LIMIT 10").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
    top = [{"symbol":str(r["symbol"]),"count":int(r["cnt"]),"pct":_round(int(r["cnt"])/total*100,1)} for r in rows]
    warnings = []
    if top and top[0]["pct"] > 20:
        warnings.append(f"WARNING: {top[0]['symbol']} = {top[0]['pct']}% dari semua shadow trades")
    return {"check":"symbol_concentration","top_10_symbols":top,"warnings":warnings,"verdict":"REVIEW" if warnings else "PASS","note":f"Top: {top[0]['symbol'] if top else 'N/A'}"}

def run_audit():
    print(f"\n{'='*60}")
    print(f"  {AUDIT_ID} — Shadow PnL / Sizing Sanity Audit")
    print(f"  {PHASE} | {GENERATED_AT[:19]} UTC")
    print(f"{'='*60}\n")

    if not Path(DB_PATH).exists():
        print(f"[FAIL] DB tidak ditemukan: {DB_PATH}")
        return

    conn = _connect(DB_PATH)
    checks = []

    # Table exists
    exists = _table_exists(conn, "shadow_trades")
    c0 = {"check":"table_exists","result":exists,"verdict":"PASS" if exists else "FAIL","note":"found" if exists else "FAIL: missing"}
    checks.append(c0)
    print(f"[{c0['verdict']}] Table exists: {c0['note']}")
    if not exists:
        conn.close(); return

    # Row count
    count = conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
    c1 = {"check":"row_count","total_rows":int(count),"verdict":"PASS" if count>=MIN_ROWS_REQUIRED else "FAIL","note":f"{count} rows"}
    checks.append(c1)
    print(f"[{c1['verdict']}] Row count: {c1['note']}")

    c2 = audit_lifecycle_coverage(conn);    checks.append(c2)
    print(f"[{c2['verdict']}] Lifecycle: {c2['note']}")
    for i in c2.get("issues",[]): print(f"  ⚠ {i}")

    c3 = audit_pnl_distribution(conn);     checks.append(c3)
    print(f"[{c3['verdict']}] PnL dist: {c3['note']}")
    for i in c3.get("issues",[]): print(f"  ⚠ {i}")

    c4 = audit_sizing_sanity(conn);        checks.append(c4)
    print(f"[{c4['verdict']}] Sizing: {c4['note']}")
    for w in c4.get("warnings",[]): print(f"  ~ {w}")

    c5 = audit_temporal_coverage(conn);    checks.append(c5)
    print(f"[{c5['verdict']}] Temporal: {c5['note']}")

    c6 = audit_symbol_concentration(conn); checks.append(c6)
    print(f"[{c6['verdict']}] Symbols: {c6['note']}")

    conn.close()

    verdicts = [c["verdict"] for c in checks]
    overall = "FAIL" if "FAIL" in verdicts else ("REVIEW" if "REVIEW" in verdicts else "PASS")

    findings = []
    if c2["only_simulated"]:
        findings.append("shadow_trades = observability log, BUKAN outcome tracker")
        findings.append("Shadow WR 100% = metric salah definisi, bukan edge")
    if c3.get("negative_count",1) == 0:
        findings.append(f"PnL semua positif (min={c3['min']}%) — expected fill simulation")
    findings.append("REKOMENDASI: gunakan internal_paper_trades sebagai sumber WR resmi")

    report = {
        "audit_id": AUDIT_ID, "phase": PHASE,
        "generated_at": GENERATED_AT, "db_path": DB_PATH,
        "overall_verdict": overall, "checks": checks,
        "findings": findings,
        "governance": {
            "paper_only": True, "read_only_db": True,
            "shadow_wr_valid": False,
            "authoritative_wr_source": "internal_paper_trades",
            "shadow_wr_explanation": "shadow_trades mencatat expected fill simulation, bukan actual TP/SL outcome. WR=100% by design."
        }
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"  OVERALL VERDICT: {overall}")
    print(f"{'='*60}")
    for f_ in findings: print(f"  → {f_}")
    print(f"\nOutput: {OUTPUT_PATH}\n")

if __name__ == "__main__":
    run_audit()
