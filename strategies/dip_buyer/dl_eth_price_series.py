"""
Downloads the intra-contract price series for every ETH 15m contract via the Polymarket
CLOB API (prices-history, free).

8 parallel threads -> ~50 min for 16k contracts.
Automatic resume from progress.json.

Requires a local outcome_cache.json mapping each contract slug to its CLOB outcome/token id
(built by a separate discovery step -- not included here, see README).

Produces: data/polymarket_eth/eth_price_series.parquet
Columns:
    candle_start_ts  : candle start (unix s, int32)
    ts               : trade timestamp (unix s, int32)
    price            : contract price (float32)
    bar_10s          : 10s bar index within the candle [0..89] (int8)
"""
import json, time, gc, threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
from datetime import datetime, timezone

CLOB_URL   = "https://clob.polymarket.com/prices-history"
CACHE_F    = Path("data/polymarket_eth/outcome_cache.json")
OUT_PATH   = Path("data/polymarket_eth/eth_price_series.parquet")
PROG_F     = Path("data/polymarket_eth/price_series_progress.json")
CANDLE_SEC = 900
BAR_SEC    = 10
N_THREADS  = 8
WRITE_EVERY = 1000   # flush to disk every N completed requests

SCHEMA = pa.schema([
    pa.field("candle_start_ts", pa.int32()),
    pa.field("ts",              pa.int32()),
    pa.field("price",           pa.float32()),
    pa.field("bar_10s",         pa.int8()),
])

_lock = threading.Lock()

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def load_cache():
    d = json.load(open(CACHE_F))
    out = {}
    for slug, val in d.items():
        if not slug.startswith("eth-updown-15m-"):
            continue
        oid = val if isinstance(val, str) else (val.get("id") or val.get("outcomeId"))
        if not oid:
            continue
        ts = int(slug.split("-")[-1])
        out[ts] = oid
    return out

def load_progress():
    if PROG_F.exists():
        return set(json.load(open(PROG_F)))
    return set()

def save_progress(done: set):
    with open(PROG_F, "w") as f:
        json.dump(sorted(done), f)

def fetch_one(ts: int, oid: str) -> tuple[int, list]:
    """Fetch prices-history for one contract. Returns (ts, history_list)."""
    for attempt in range(3):
        try:
            r = requests.get(CLOB_URL, params={
                "market":   oid,
                "startTs":  ts,
                "endTs":    ts + CANDLE_SEC,
                "fidelity": 1,
            }, timeout=15)
            if r.status_code == 200:
                return ts, r.json().get("history", [])
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
        except Exception:
            time.sleep(2)
    return ts, []

def history_to_rows(ts: int, history: list) -> list:
    rows = []
    for h in history:
        bar = max(0, min(89, (h["t"] - ts) // BAR_SEC))
        rows.append((ts, h["t"], float(h["p"]), bar))
    return rows

def flush_buffer(rows: list, writer: pq.ParquetWriter):
    if not rows:
        return
    starts = pa.array([r[0] for r in rows], type=pa.int32())
    ts_arr = pa.array([r[1] for r in rows], type=pa.int32())
    prices = pa.array([r[2] for r in rows], type=pa.float32())
    bars   = pa.array([r[3] for r in rows], type=pa.int8())
    tbl    = pa.table({"candle_start_ts": starts, "ts": ts_arr,
                       "price": prices, "bar_10s": bars}, schema=SCHEMA)
    with _lock:
        writer.write_table(tbl)

def main():
    log("Loading outcomeId cache...")
    contracts = load_cache()
    log(f"{len(contracts):,} ETH contracts in cache")

    done = load_progress()
    log(f"{len(done):,} already downloaded -- {len(contracts) - len(done):,} remaining")

    remaining = [(ts, contracts[ts]) for ts in sorted(contracts) if ts not in done]
    if not remaining:
        log("Nothing to do."); return

    # Remove a corrupted file if there's no valid progress
    if OUT_PATH.exists() and len(done) == 0:
        OUT_PATH.unlink()

    # Append mode (ParquetWriter creates a new file)
    out_tmp = OUT_PATH.with_suffix(".tmp.parquet")
    writer  = pq.ParquetWriter(out_tmp, SCHEMA, compression="snappy")

    buffer = []
    n_ok   = 0
    n_done = 0
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        futures = {pool.submit(fetch_one, ts, oid): ts for ts, oid in remaining}

        for fut in as_completed(futures):
            ts, hist = fut.result()
            rows = history_to_rows(ts, hist)
            if rows:
                buffer.extend(rows)
                n_ok += 1
            done.add(ts)
            n_done += 1

            if n_done % WRITE_EVERY == 0:
                flush_buffer(buffer, writer)
                buffer.clear()
                gc.collect()
                save_progress(done)
                elapsed = time.time() - t_start
                rate = n_done / elapsed
                eta  = (len(remaining) - n_done) / rate / 60
                log(f"[{n_done}/{len(remaining)}] {n_done/len(remaining)*100:.1f}% | "
                    f"{rate:.1f} req/s | ETA {eta:.0f}min | {n_ok} with data")

    # Final flush
    flush_buffer(buffer, writer)
    writer.close()
    save_progress(done)

    # Merge with existing data if needed
    if OUT_PATH.exists():
        log("Merging with existing data...")
        old = pq.read_table(OUT_PATH, schema=SCHEMA)
        new = pq.read_table(out_tmp, schema=SCHEMA)
        merged = pa.concat_tables([old, new])
        pq.write_table(merged, OUT_PATH, compression="snappy")
        out_tmp.unlink()
        del old, new, merged; gc.collect()
    else:
        out_tmp.rename(OUT_PATH)

    sz = OUT_PATH.stat().st_size / 1e6
    elapsed = time.time() - t_start
    log(f"Done in {elapsed/60:.1f}min | {n_ok:,} contracts with data | {sz:.1f} MB")

    tbl = pq.read_table(OUT_PATH).to_pandas()
    n_contracts = tbl["candle_start_ts"].nunique()
    med_pts = tbl.groupby("candle_start_ts").size().median()
    log(f"Total: {len(tbl):,} points | {n_contracts:,} contracts | median {med_pts:.0f} pts/contract")


if __name__ == "__main__":
    main()
