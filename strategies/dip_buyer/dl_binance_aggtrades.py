"""
Download ETHUSDT aggTrades via Binance Vision (bulk S3, ~14MB/day zipped).
Much faster than the REST API (1 zip vs ~1000 requests/day).
Memory-optimized: streaming processing, compact dtypes, pyarrow.
"""
import io, zipfile, requests, time, gc
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_URL  = "https://data.binance.vision/data/spot/daily/aggTrades/ETHUSDT"
OUT_DIR   = Path("data/polymarket_eth")
AGG_DIR   = OUT_DIR / "binance_agg"
AGG_DIR.mkdir(parents=True, exist_ok=True)
OUT_ALL   = OUT_DIR / "ethusdt_agg_all.parquet"

START_DATE = "2025-11-22"
END_DATE   = "2026-06-05"

# Compact Arrow schema -- saves ~40% RAM vs default float64
SCHEMA = pa.schema([
    pa.field("ts_ms",          pa.int64()),
    pa.field("price",          pa.float32()),
    pa.field("qty",            pa.float32()),
    pa.field("is_buyer_maker", pa.bool_()),
])

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_day(date_str: str) -> tuple[bool, int]:
    """Download one day via Binance Vision bulk. Returns (ok, n_bytes)."""
    path = AGG_DIR / f"ethusdt_agg_{date_str}.parquet"
    if path.exists() and path.stat().st_size > 10_000:
        return True, 0

    url = f"{BASE_URL}/ETHUSDT-aggTrades-{date_str}.zip"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60, stream=True)
            if r.status_code == 404:
                return False, 0
            if r.status_code == 429:
                log("  Rate limit, waiting 60s"); time.sleep(60); continue
            r.raise_for_status()
            raw = r.content
            break
        except Exception as e:
            log(f"  Attempt {attempt+1}/3: {e}")
            time.sleep(5)
    else:
        return False, 0

    n_bytes = len(raw)

    # Streaming extraction -- never held fully in memory
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as f:
            # Binance Vision aggTrades columns:
            # agg_id, price, qty, first_trade_id, last_trade_id, transact_time, is_buyer_maker, best_price
            df = pd.read_csv(
                f,
                header=None,
                names=["agg_id","price","qty","first_id","last_id","ts_ms","is_buyer_maker","_best"],
                usecols=["ts_ms","price","qty","is_buyer_maker"],
                dtype={"ts_ms": "int64", "price": "float32",
                       "qty": "float32", "is_buyer_maker": "bool"},
                engine="c",
                memory_map=False,
            )

    del raw
    gc.collect()

    table = pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False)
    del df
    pq.write_table(table, path, compression="snappy", row_group_size=500_000)
    del table
    gc.collect()
    return True, n_bytes


def consolidate():
    """Merge all daily files into one, streaming via ParquetWriter."""
    log(f"Consolidating into {OUT_ALL.name}...")
    files = sorted(AGG_DIR.glob("ethusdt_agg_*.parquet"))
    if not files:
        log("Nothing to consolidate."); return

    writer = None
    total = 0
    for f in files:
        tbl = pq.read_table(f, schema=SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(OUT_ALL, SCHEMA, compression="snappy")
        writer.write_table(tbl)
        total += len(tbl)
        del tbl
    if writer:
        writer.close()

    sz = OUT_ALL.stat().st_size / 1e6
    log(f"Done: {OUT_ALL.name} | {total:,} trades | {sz:.1f} MB")


def main():
    d = datetime.strptime(START_DATE, "%Y-%m-%d")
    end = datetime.strptime(END_DATE, "%Y-%m-%d")
    dates = []
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    remaining = [x for x in dates
                 if not (AGG_DIR / f"ethusdt_agg_{x}.parquet").exists()
                 or (AGG_DIR / f"ethusdt_agg_{x}.parquet").stat().st_size <= 10_000]

    log(f"Binance Vision aggTrades ETHUSDT | {len(remaining)}/{len(dates)} days to download")
    log(f"Method: bulk zip S3 (~14MB/day, ~1s/day)")

    total_bytes = 0
    ok_count = 0
    for i, date_str in enumerate(remaining):
        ok, nb = fetch_day(date_str)
        total_bytes += nb
        if ok and nb > 0:
            ok_count += 1
            sz_mb = nb / 1e6
            p = AGG_DIR / f"ethusdt_agg_{date_str}.parquet"
            pq_mb = p.stat().st_size / 1e6 if p.exists() else 0
            log(f"[{i+1}/{len(remaining)}] {date_str} | zip {sz_mb:.1f}MB -> parquet {pq_mb:.1f}MB")
        elif not ok:
            log(f"[{i+1}/{len(remaining)}] {date_str} | SKIP (404 or error)")

    log(f"Download finished | {ok_count} days OK | {total_bytes/1e6:.0f} MB raw")
    consolidate()


if __name__ == "__main__":
    main()
