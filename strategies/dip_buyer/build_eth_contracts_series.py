"""
Builds the Polymarket ETH 15m contract series from eth_trades_all.parquet.
Memory-optimized: compact dtypes + vectorized groupby (no Python loop).

Produces: data/polymarket_eth/eth_contracts_series.parquet
Columns:
    candle_start_ts  : candle start (unix s)
    candle_end_ts    : candle end (unix s)
    n_trades         : number of trades
    open_price       : first price
    close_price      : last price
    min_price        : minimum price
    max_price        : maximum price
    volume_usd       : total volume (USDC)
    vol_buy          : buy-side volume
    vol_sell         : sell-side volume
    buy_pressure     : vol_buy / volume_usd
    resolved_up      : 1 if close_price > 0.5 (UP contract won)
    price_drop_min   : min_price / open_price -- maximum depth of the dip
    time_of_min_pct  : when the minimum was reached (0=start, 1=end of candle)
"""
import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path

CANDLE_SEC = 900   # 15 minutes
IN_PATH    = Path("data/polymarket_eth/eth_trades_all.parquet")
OUT_PATH   = Path("data/polymarket_eth/eth_contracts_series.parquet")

def log(msg):
    from datetime import datetime, timezone
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_compact(path: Path) -> pd.DataFrame:
    """Load with compact dtypes -- cuts RAM by ~3x."""
    df = pd.read_parquet(path, columns=["candle_start_ts","ts_ms","price","amount","side"])
    df["price"]          = df["price"].astype("float32")
    df["amount"]         = df["amount"].astype("float32")
    df["candle_start_ts"] = df["candle_start_ts"].astype("int32")
    df["ts_ms"]          = df["ts_ms"].astype("int64")
    df["is_buy"]         = (df["side"] == "buy").astype("bool")
    df.drop(columns=["side"], inplace=True)
    return df


def compute_time_of_min(df: pd.DataFrame) -> pd.Series:
    """
    For each contract: the moment the minimum price is reached, normalized to [0,1].
    0 = start, 1 = end of candle.
    Vectorized via idxmin + merge.
    """
    # Index of the cheapest trade per contract
    idx_min = df.groupby("candle_start_ts")["price"].idxmin()
    ts_at_min = df.loc[idx_min, ["candle_start_ts", "ts_ms"]].set_index("candle_start_ts")["ts_ms"]
    return ts_at_min


def build_series(df: pd.DataFrame) -> pd.DataFrame:
    log(f"Vectorized groupby over {len(df):,} trades, {df['candle_start_ts'].nunique():,} contracts...")

    grp = df.groupby("candle_start_ts")

    # Vectorized aggregations in one pass
    agg = grp.agg(
        n_trades  = ("price",  "count"),
        open_price= ("price",  "first"),   # first trade in file order
        close_price=("price",  "last"),
        min_price = ("price",  "min"),
        max_price = ("price",  "max"),
        volume_usd= ("amount", "sum"),
        vol_buy   = ("amount", lambda x: x[df.loc[x.index, "is_buy"]].sum()),
    ).reset_index()

    # For correct open/close, sort by ts_ms first
    log("Sorting by ts_ms for correct open/close...")
    df_sorted = df.sort_values(["candle_start_ts","ts_ms"])
    grp_sorted = df_sorted.groupby("candle_start_ts")

    agg["open_price"]  = grp_sorted["price"].first().values
    agg["close_price"] = grp_sorted["price"].last().values

    # timestamp of the minimum (for time_of_min_pct)
    ts_at_min = compute_time_of_min(df_sorted)
    agg["ts_at_min"] = agg["candle_start_ts"].map(ts_at_min)

    # start/end timestamps
    ts_first = grp_sorted["ts_ms"].first()
    ts_last  = grp_sorted["ts_ms"].last()
    agg["ts_first"] = agg["candle_start_ts"].map(ts_first)
    agg["ts_last"]  = agg["candle_start_ts"].map(ts_last)

    del df_sorted, grp_sorted
    import gc; gc.collect()

    # Derived columns
    agg["candle_end_ts"]    = agg["candle_start_ts"] + CANDLE_SEC
    agg["vol_sell"]         = agg["volume_usd"] - agg["vol_buy"]
    agg["buy_pressure"]     = (agg["vol_buy"] / agg["volume_usd"].replace(0, np.nan)).astype("float32")
    agg["resolved_up"]      = (agg["close_price"] > 0.5).astype("int8")
    agg["price_drop_min"]   = (agg["min_price"] / agg["open_price"].replace(0, np.nan)).astype("float32")
    # Normalized time of minimum: (ts_at_min - ts_first) / (ts_last - ts_first)
    span = (agg["ts_last"] - agg["ts_first"]).replace(0, np.nan)
    agg["time_of_min_pct"]  = ((agg["ts_at_min"] - agg["ts_first"]) / span).astype("float32")

    # Drop intermediate columns
    agg.drop(columns=["ts_at_min","ts_first","ts_last"], inplace=True)

    # Compact output dtypes
    for col in ["open_price","close_price","min_price","max_price","volume_usd","vol_buy","vol_sell"]:
        agg[col] = agg[col].astype("float32")
    agg["n_trades"] = agg["n_trades"].astype("int32")

    return agg.sort_values("candle_start_ts").reset_index(drop=True)


def main():
    log(f"Loading {IN_PATH} (compact dtypes)...")
    df = load_compact(IN_PATH)
    ram = df.memory_usage(deep=True).sum() / 1e6
    log(f"{len(df):,} trades loaded | {ram:.0f} MB RAM")

    contracts = build_series(df)
    del df
    import gc; gc.collect()

    log(f"Writing {OUT_PATH}...")
    contracts.to_parquet(OUT_PATH, index=False, compression="snappy")
    sz = OUT_PATH.stat().st_size / 1e6
    log(f"{len(contracts):,} contracts | {sz:.1f} MB")
    log(f"Columns: {list(contracts.columns)}")
    log(f"Period: {pd.to_datetime(contracts['candle_start_ts'].min(), unit='s', utc=True).date()} "
        f"-> {pd.to_datetime(contracts['candle_start_ts'].max(), unit='s', utc=True).date()}")

    print("\nPreview:")
    print(contracts.head(5).to_string())
    print("\nStats:")
    print(contracts[["open_price","close_price","min_price","price_drop_min","volume_usd","buy_pressure"]].describe())


if __name__ == "__main__":
    main()
