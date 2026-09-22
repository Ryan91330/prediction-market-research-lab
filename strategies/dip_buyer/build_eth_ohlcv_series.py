"""
Builds the 10s OHLCV series for Polymarket ETH 15m contracts, joined with the
ETH/USDT Binance spot price.

Sources:
  - eth_price_series.parquet  : CLOB contract price (t in seconds, p float32)
  - ethusdt_agg_all.parquet   : Binance ETHUSDT aggTrades (ts_ms in microseconds)

Produces: data/polymarket_eth/eth_ohlcv_series.parquet
Columns per 10s bar:
  candle_start_ts : start of the 15m contract (int32, seconds)
  bar_idx         : bar index [0..89] within the contract (int8)
  bar_ts          : bar start timestamp (int32, seconds)
  open, high, low, close  : contract price (float32)
  n_ticks         : number of CLOB ticks in the bar (int16)
  eth_open, eth_high, eth_low, eth_close : ETH spot price from Binance (float32)
  eth_n_trades    : number of Binance trades in the bar (int32)
  eth_volume      : ETH volume from Binance in the bar (float32)

Memory strategy:
  - aggTrades (1.4 GB) read by row group (~256 MB each), never loaded fully into RAM
  - processed in 1-day windows (~5760 10s bars, ~518,400 Binance trades)
  - incremental writes via ParquetWriter
"""
import gc
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta

PS_PATH  = Path("data/polymarket_eth/eth_price_series.parquet")
AGG_PATH = Path("data/polymarket_eth/ethusdt_agg_all.parquet")
OUT_PATH = Path("data/polymarket_eth/eth_ohlcv_series.parquet")
BAR_SEC  = 10
CANDLE_SEC = 900

SCHEMA = pa.schema([
    pa.field("candle_start_ts", pa.int32()),
    pa.field("bar_idx",         pa.int8()),
    pa.field("bar_ts",          pa.int32()),
    pa.field("open",            pa.float32()),
    pa.field("high",            pa.float32()),
    pa.field("low",             pa.float32()),
    pa.field("close",           pa.float32()),
    pa.field("n_ticks",         pa.int16()),
    pa.field("eth_open",        pa.float32()),
    pa.field("eth_high",        pa.float32()),
    pa.field("eth_low",         pa.float32()),
    pa.field("eth_close",       pa.float32()),
    pa.field("eth_n_trades",    pa.int32()),
    pa.field("eth_volume",      pa.float32()),
])

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def build_contract_ohlcv(ps_day: pd.DataFrame) -> pd.DataFrame:
    """
    Resamples CLOB ticks into 10s bars for every contract in a given day.
    ps_day columns: candle_start_ts, ts (seconds), price
    """
    if ps_day.empty:
        return pd.DataFrame()

    # bar_ts = bar start timestamp (rounded to 10s from contract start)
    ps_day = ps_day.copy()
    ps_day["bar_ts"] = (ps_day["candle_start_ts"]
                        + ((ps_day["ts"] - ps_day["candle_start_ts"]) // BAR_SEC) * BAR_SEC).astype("int32")
    ps_day["bar_idx"] = ((ps_day["ts"] - ps_day["candle_start_ts"]) // BAR_SEC).clip(0, 89).astype("int8")

    grp = ps_day.groupby(["candle_start_ts", "bar_ts", "bar_idx"])["price"]
    bars = pd.DataFrame({
        "open":    grp.first(),
        "high":    grp.max(),
        "low":     grp.min(),
        "close":   grp.last(),
        "n_ticks": grp.count().astype("int16"),
    }).reset_index()

    # Forward-fill missing bars within each contract
    result = []
    for cs_ts, grp_c in bars.groupby("candle_start_ts"):
        # Full 0..89 grid
        grid = pd.DataFrame({
            "candle_start_ts": cs_ts,
            "bar_idx": np.arange(90, dtype="int8"),
            "bar_ts":  (cs_ts + np.arange(90) * BAR_SEC).astype("int32"),
        })
        merged = grid.merge(grp_c[["bar_idx","open","high","low","close","n_ticks"]],
                            on="bar_idx", how="left")
        # Forward-fill the previous close as open/high/low/close for empty bars
        merged[["open","high","low","close"]] = (
            merged[["open","high","low","close"]].ffill()
        )
        merged["n_ticks"] = merged["n_ticks"].fillna(0).astype("int16")
        result.append(merged)

    return pd.concat(result, ignore_index=True)


def build_eth_ohlcv(agg_day: pd.DataFrame, day_start_s: int, day_end_s: int) -> pd.DataFrame:
    """
    Resamples Binance aggTrades into 10s bars for the full day.
    agg_day columns: ts_s (seconds, int32), price, qty
    """
    if agg_day.empty:
        # Return an empty grid
        n_bars = (day_end_s - day_start_s) // BAR_SEC
        return pd.DataFrame({
            "bar_ts":      (day_start_s + np.arange(n_bars) * BAR_SEC).astype("int32"),
            "eth_open":    np.full(n_bars, np.nan, dtype="float32"),
            "eth_high":    np.full(n_bars, np.nan, dtype="float32"),
            "eth_low":     np.full(n_bars, np.nan, dtype="float32"),
            "eth_close":   np.full(n_bars, np.nan, dtype="float32"),
            "eth_n_trades": np.zeros(n_bars, dtype="int32"),
            "eth_volume":  np.zeros(n_bars, dtype="float32"),
        })

    agg_day = agg_day.copy()
    agg_day["bar_ts"] = (agg_day["ts_s"] // BAR_SEC * BAR_SEC).astype("int32")

    grp = agg_day.groupby("bar_ts")
    eth_bars = pd.DataFrame({
        "eth_open":     grp["price"].first(),
        "eth_high":     grp["price"].max(),
        "eth_low":      grp["price"].min(),
        "eth_close":    grp["price"].last(),
        "eth_n_trades": grp["price"].count().astype("int32"),
        "eth_volume":   grp["qty"].sum().astype("float32"),
    }).reset_index()

    # Full grid over the day
    n_bars = (day_end_s - day_start_s) // BAR_SEC
    grid = pd.DataFrame({
        "bar_ts": (day_start_s + np.arange(n_bars) * BAR_SEC).astype("int32")
    })
    merged = grid.merge(eth_bars, on="bar_ts", how="left")
    merged[["eth_open","eth_high","eth_low","eth_close"]] = (
        merged[["eth_open","eth_high","eth_low","eth_close"]].ffill()
    )
    merged["eth_n_trades"] = merged["eth_n_trades"].fillna(0).astype("int32")
    merged["eth_volume"]   = merged["eth_volume"].fillna(0).astype("float32")
    return merged


def main():
    log("Loading eth_price_series.parquet...")
    ps = pd.read_parquet(PS_PATH)
    ps["ts"] = ps["ts"].astype("int32")
    ps["candle_start_ts"] = ps["candle_start_ts"].astype("int32")
    log(f"price_series: {len(ps):,} ticks | {ps['candle_start_ts'].nunique():,} contracts")

    # Period covered
    ts_min = int(ps["candle_start_ts"].min())
    ts_max = int(ps["candle_start_ts"].max()) + CANDLE_SEC
    log(f"Period: {datetime.fromtimestamp(ts_min, tz=timezone.utc).date()} -> "
        f"{datetime.fromtimestamp(ts_max, tz=timezone.utc).date()}")

    # Open the aggTrades file in streaming mode (row groups)
    log("Opening aggTrades in streaming mode (row groups)...")
    pf_agg = pq.ParquetFile(AGG_PATH)
    log(f"aggTrades: {pf_agg.num_row_groups} row groups")

    # Build a day -> row-groups index for aggTrades
    # Each row group covers ~1 day -- read the min/max ts_s per row group
    log("Indexing aggTrades row groups by day (fast scan)...")
    rg_index = []  # list of (day_start_s, day_end_s, rg_idx)
    for rg_idx in range(pf_agg.num_row_groups):
        rg_meta = pf_agg.metadata.row_group(rg_idx)
        # stats on ts_ms (microseconds)
        ts_col = None
        for i in range(rg_meta.num_columns):
            if pf_agg.metadata.row_group(rg_idx).column(i).path_in_schema == "ts_ms":
                ts_col = pf_agg.metadata.row_group(rg_idx).column(i)
                break
        if ts_col and ts_col.statistics:
            s_min = int(ts_col.statistics.min) // 1_000_000
            s_max = int(ts_col.statistics.max) // 1_000_000
        else:
            # Fallback: read the first/last two rows
            rg_df = pf_agg.read_row_group(rg_idx, columns=["ts_ms"]).to_pandas()
            s_min = int(rg_df["ts_ms"].min()) // 1_000_000
            s_max = int(rg_df["ts_ms"].max()) // 1_000_000
            del rg_df; gc.collect()
        rg_index.append((s_min, s_max, rg_idx))

    log(f"Row-group index: {len(rg_index)} entries | "
        f"{datetime.fromtimestamp(rg_index[0][0], tz=timezone.utc).date()} -> "
        f"{datetime.fromtimestamp(rg_index[-1][1], tz=timezone.utc).date()}")

    # Build the list of days to process
    day_cur = datetime.fromtimestamp(ts_min, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    day_end_dt = datetime.fromtimestamp(ts_max, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    days = []
    while day_cur < day_end_dt:
        days.append(day_cur)
        day_cur += timedelta(days=1)
    log(f"{len(days)} days to process")

    # Incremental write
    writer = pq.ParquetWriter(OUT_PATH, SCHEMA, compression="snappy")
    total_bars = 0

    for day_idx, day_dt in enumerate(days):
        day_s   = int(day_dt.timestamp())
        day_e   = day_s + 86400

        # --- Contracts for this day ---
        ps_day = ps[(ps["candle_start_ts"] >= day_s) & (ps["candle_start_ts"] < day_e)]
        if ps_day.empty:
            continue

        # --- aggTrades for this day (row groups that cover it) ---
        rgs_needed = [rg for s, e, rg in rg_index if s < day_e and e >= day_s]
        if rgs_needed:
            chunks = []
            for rg in rgs_needed:
                rg_df = pf_agg.read_row_group(rg, columns=["ts_ms","price","qty"]).to_pandas()
                # Filter to the day + convert us -> s
                rg_df["ts_s"] = (rg_df["ts_ms"] // 1_000_000).astype("int32")
                rg_df = rg_df[(rg_df["ts_s"] >= day_s) & (rg_df["ts_s"] < day_e)]
                if not rg_df.empty:
                    chunks.append(rg_df[["ts_s","price","qty"]])
                del rg_df; gc.collect()
            agg_day = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(
                columns=["ts_s","price","qty"])
        else:
            agg_day = pd.DataFrame(columns=["ts_s","price","qty"])

        # --- Build contract OHLCV ---
        contract_bars = build_contract_ohlcv(ps_day)
        if contract_bars.empty:
            del agg_day; gc.collect()
            continue

        # --- Build ETH OHLCV ---
        eth_bars = build_eth_ohlcv(agg_day, day_s, day_e)
        del agg_day; gc.collect()

        # --- Join: each contract bar gets its ETH prices ---
        merged = contract_bars.merge(eth_bars, on="bar_ts", how="left")

        # Fill NaN ETH values (bars with no Binance trades)
        for col in ["eth_open","eth_high","eth_low","eth_close"]:
            merged[col] = merged[col].ffill().astype("float32")
        merged["eth_n_trades"] = merged["eth_n_trades"].fillna(0).astype("int32")
        merged["eth_volume"]   = merged["eth_volume"].fillna(0).astype("float32")

        # Write via Arrow
        tbl = pa.Table.from_pandas(merged[list(SCHEMA.names)], schema=SCHEMA,
                                   preserve_index=False)
        writer.write_table(tbl)
        total_bars += len(merged)
        del merged, contract_bars, eth_bars, tbl; gc.collect()

        if (day_idx + 1) % 30 == 0 or day_idx == 0:
            log(f"[{day_idx+1}/{len(days)}] {day_dt.date()} | "
                f"{total_bars:,} bars written")

    writer.close()
    sz = OUT_PATH.stat().st_size / 1e6
    log(f"{OUT_PATH.name} | {total_bars:,} bars | {sz:.1f} MB")

    # --- Verification: print one full contract ---
    log("Verification -- reading a sample contract...")
    result = pd.read_parquet(OUT_PATH)
    log(f"Total: {len(result):,} bars | {result['candle_start_ts'].nunique():,} contracts")

    ex_ts = int(result["candle_start_ts"].iloc[100])  # take the 100th (not the 1st, often incomplete)
    ex = result[result["candle_start_ts"] == ex_ts].sort_values("bar_idx")
    ex_dt = datetime.fromtimestamp(ex_ts, tz=timezone.utc)
    print(f"\n=== Sample contract: {ex_dt} (candle_start_ts={ex_ts}) ===")
    print(f"Bars: {len(ex)} | bar_idx 0..{ex['bar_idx'].max()}")
    print(ex[["bar_idx","bar_ts","open","high","low","close","n_ticks",
              "eth_open","eth_close","eth_volume"]].to_string(index=False))


if __name__ == "__main__":
    main()
