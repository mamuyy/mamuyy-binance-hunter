# Max Concurrent Positions Policy

## Policy Constants

| Parameter | Value | Scope |
|---|---|---|
| `MAX_CONCURRENT_PER_SYMBOL` | **3** | Maximum open + TP1 HIT positions for a single symbol |
| `MAX_CONCURRENT_GLOBAL` | **20** | Maximum open + TP1 HIT positions across all symbols |

**Open statuses counted:** `OPEN`, `TP1 HIT`
**Terminal statuses (not counted):** `CLOSED`, `SL_HIT`, `TP2_HIT`

---

## Rationale

- Prevents capital over-concentration in a single symbol when multiple signals fire in quick succession.
- The global cap bounds total exposure during high-volatility regimes where many symbols may trigger simultaneously.
- TP1 HIT positions are counted because they still carry open exposure on the TP2 leg.

---

## Where to Enforce in Code

The enforcement gate should be inserted in **`internal_paper_engine.py`**, inside the function that processes a new incoming signal and decides whether to insert a new trade row.

**Do not modify `internal_paper_engine.py` based on this document.** This pseudocode is for reference and planning only.

### Pseudocode — Per-Symbol + Global Check

```python
# === POLICY ENFORCEMENT (pseudocode — do NOT copy verbatim) ===

OPEN_STATUSES = {"OPEN", "TP1 HIT"}
MAX_CONCURRENT_PER_SYMBOL = 3
MAX_CONCURRENT_GLOBAL = 20

def _count_active_for_symbol(conn, symbol: str) -> int:
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    cur = conn.execute(
        f"SELECT COUNT(*) FROM internal_paper_trades "
        f"WHERE symbol = ? AND status IN ({placeholders})",
        (symbol, *OPEN_STATUSES),
    )
    return cur.fetchone()[0]

def _count_active_global(conn) -> int:
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    cur = conn.execute(
        f"SELECT COUNT(*) FROM internal_paper_trades "
        f"WHERE status IN ({placeholders})",
        tuple(OPEN_STATUSES),
    )
    return cur.fetchone()[0]

# --- Inside the trade-insertion function, BEFORE INSERT ---

symbol_count = _count_active_for_symbol(conn, signal["symbol"])
if symbol_count >= MAX_CONCURRENT_PER_SYMBOL:
    log.warning(
        "POLICY_BLOCK symbol=%s active=%d limit=%d — skipping new position",
        signal["symbol"], symbol_count, MAX_CONCURRENT_PER_SYMBOL,
    )
    return  # do not insert

global_count = _count_active_global(conn)
if global_count >= MAX_CONCURRENT_GLOBAL:
    log.warning(
        "POLICY_BLOCK global active=%d limit=%d — skipping new position",
        global_count, MAX_CONCURRENT_GLOBAL,
    )
    return  # do not insert

# --- Proceed with INSERT ---
```

### Recommended Location

In `internal_paper_engine.py`, find the function that:
1. Receives a scored signal
2. Decides whether to open a new paper position
3. Calls `conn.execute("INSERT INTO internal_paper_trades ...")`

Insert the guard block **immediately before** that INSERT call, after any existing confidence/regime filters.

---

## Diagnostic Script

Run `scripts/audit_concurrent_positions.py` at any time to see the current state:

```bash
cd /home/ubuntu/mamuyy-binance-hunter
.venv/bin/python scripts/audit_concurrent_positions.py
```

Output report: `reports/concurrent_positions_audit.json`

---

## Stale Trades Companion

Symbols with `STALE_ORPHANED` stale trades (see `scripts/audit_stale_trades.py`) should be investigated before enforcement is enabled — orphaned open positions may inflate the per-symbol count for symbols no longer active in the feed.
