"""
REAL MAKER MAP — BTC up/down HOURLY.

Zero simulation: every trade on the tape has a maker on the other side, and that maker's
PnL/share at expiry is fully known (taker BUY -> maker sold at p -> pnl = p - payout;
taker SELL -> pnl = payout - p). Aggregations: price x time-remaining, distance-from-mid x
(tau, price), monthly stability.

PRIMARY JUDGE = raw PnL (rebates are not treated as the edge; see README). The rebate (an
upper-bound pro-rata estimate of 0.2 x 0.07 x p(1-p)) is reported separately.

Output: stdout + data/mm1h_maker_trades.parquet (enriched trades, for further analysis).
"""

import glob
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

GLOB        = "data/pmdata/btc_1h/*.parquet"
OUT_TRADES  = Path("data/mm1h_maker_trades.parquet")
PERIOD_S    = 3600
DEDUPE_MS   = 30
FEE_RATE    = 0.07
REBATE_SH   = 0.20

ET = ZoneInfo("America/New_York")
MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}
_SLUG_RE = re.compile(
    r"bitcoin-up-or-down-([a-z]+)-(\d+)(?:-(\d{4}))?-(\d+)(am|pm)-et")

P_BINS    = [0.03, 0.15, 0.35, 0.65, 0.85, 0.97]
TAU_BINS  = [0, 60, 300, 900, 1800, 3600]
DIST_BINS = [-1.0, 0.004, 0.014, 0.034, 0.074, 1.0]
DIST_LBL  = ["touch(<0.5c)", "0.5-1.4c", "1.5-3.4c", "3.5-7.4c", "deep(>7.4c)"]


def contract_start(path: str) -> int | None:
    m = _SLUG_RE.search(Path(path).stem)
    if not m:
        return None
    month, day, year, hh, ap = m.groups()
    year = int(year) if year else 2026
    h = int(hh) % 12 + (12 if ap == "pm" else 0)
    return int(datetime(year, MONTHS[month], int(day), h, tzinfo=ET).timestamp())


def load_contract(path: str):
    cs = contract_start(path)
    if cs is None:
        return None
    df = pd.read_parquet(path, columns=[
        "local_timestamp", "event_type", "best_bid", "best_ask",
        "trade_price", "trade_size", "trade_side", "winning_outcome"])

    # Payout of the Up token: winning_outcome ('yes'=Up) preferred, fallback to final mid
    wo = df["winning_outcome"].dropna()
    if len(wo) and wo.iloc[-1] in ("yes", "no"):
        payout = 1.0 if wo.iloc[-1] == "yes" else 0.0
    else:
        l1f = df[["best_bid", "best_ask"]].ffill()
        mid_end = (l1f["best_bid"].iloc[-1] + l1f["best_ask"].iloc[-1]) / 2.0
        if np.isnan(mid_end) or abs(mid_end - 0.5) < 0.15:
            lt = df["trade_price"].dropna()
            if len(lt) == 0 or abs(lt.iloc[-1] - 0.5) < 0.15:
                return None
            mid_end = lt.iloc[-1]
        payout = 1.0 if mid_end > 0.5 else 0.0

    is_tr = (df["event_type"] == "last_trade_price").values
    tr_idx = np.where(is_tr & df["trade_price"].notna().values
                      & df["trade_size"].notna().values)[0]
    if len(tr_idx) == 0:
        return None

    # Mid just BEFORE each trade (last known L1 state before the trade's own row)
    l1 = df[["best_bid", "best_ask"]].ffill()
    mid_all = ((l1["best_bid"] + l1["best_ask"]) / 2.0).shift(1).values
    mid_at = mid_all[tr_idx]

    tms  = df["local_timestamp"].values[tr_idx].astype("datetime64[ms]").astype(np.int64)
    tpx  = df["trade_price"].values[tr_idx].astype(np.float64)
    tsz  = df["trade_size"].values[tr_idx].astype(np.float64)
    tbuy = (df["trade_side"].values[tr_idx] == "BUY")

    keep = np.ones(len(tr_idx), dtype=bool)          # de-dupe YES/NO mirror prints
    if len(tr_idx) > 1:
        same = (np.diff(tms) < DEDUPE_MS) & (np.diff(tpx) == 0) & \
               (np.diff(tsz) == 0) & (tbuy[1:] == tbuy[:-1])
        keep[1:] = ~same
    tms, tpx, tsz, tbuy, mid_at = tms[keep], tpx[keep], tsz[keep], tbuy[keep], mid_at[keep]

    tau = ((cs + PERIOD_S) * 1000 - tms) / 1000.0    # seconds remaining (>3600 = pre-open)
    # Maker-side cushion: taker BUY -> maker was selling at p (cushion p - mid); SELL -> mid - p
    dist = np.where(tbuy, tpx - mid_at, mid_at - tpx)
    pnl  = np.where(tbuy, tpx - payout, payout - tpx)
    reb  = REBATE_SH * FEE_RATE * tpx * (1 - tpx)
    return pd.DataFrame(dict(
        p=tpx, tau=tau, sz=tsz, dist=dist, pnl=pnl, reb=reb, taker_buy=tbuy,
        month=np.full(len(tpx), datetime.fromtimestamp(cs, tz=ET).strftime("%Y-%m")),
        week=np.full(len(tpx), datetime.fromtimestamp(cs, tz=ET).strftime("%W")),
        hour_et=np.full(len(tpx), datetime.fromtimestamp(cs, tz=ET).hour)))


def wavg(x, col="pnl"):
    return np.average(x[col], weights=x["sz"]) * 100 if len(x) else np.nan


def main():
    paths = sorted(glob.glob(GLOB), key=contract_start)
    recs, skipped = [], 0
    for k, path in enumerate(paths):
        if k % 200 == 0:
            print(f"  {k}/{len(paths)}")
        try:
            r = load_contract(path)
            if r is None:
                skipped += 1
            else:
                recs.append(r)
        except Exception as e:
            print(f"  ERR {Path(path).stem}: {e}")
            skipped += 1

    d = pd.concat(recs, ignore_index=True)
    d.to_parquet(OUT_TRADES, compression="snappy")
    live = d[(d["tau"] > 0) & (d["tau"] <= PERIOD_S)].copy()
    pre  = d[d["tau"] > PERIOD_S]
    print(f"\n{len(d):,} trades ({len(live):,} live, {len(pre):,} pre-open) "
          f"| {len(paths)} contracts, {skipped} skipped | -> {OUT_TRADES}")

    live["p_bin"]    = pd.cut(live["p"], P_BINS)
    live["tau_bin"]  = pd.cut(live["tau"], TAU_BINS)
    live["dist_bin"] = pd.cut(live["dist"], DIST_BINS, labels=DIST_LBL)

    g_live, r_live = wavg(live), wavg(live, "reb")
    g_pre = wavg(pre)
    print("\n" + "=" * 78)
    print(f" GLOBAL raw maker PnL: live {g_live:+.3f} c/sh | pre-open {g_pre:+.3f} c/sh")
    print(f" (rebate upper bound {r_live:+.3f} -> net {g_live + r_live:+.3f}; "
          f"the judge is the RAW number)")
    print("=" * 78)

    lv = live.dropna(subset=["p_bin", "tau_bin"])
    print("\n=== RAW c/sh: PRICE x SECONDS REMAINING (size-weighted) ===")
    print(lv.groupby(["p_bin", "tau_bin"], observed=False)
            .apply(wavg, include_groups=False).unstack().round(3).to_string())
    volz = lv.groupby(["p_bin", "tau_bin"], observed=False)["sz"].sum().unstack()
    print("\n=== Share of volume by zone (%) ===")
    print((volz / volz.values.sum() * 100).round(1).to_string())

    ld = live.dropna(subset=["dist_bin", "tau_bin"])
    print("\n=== RAW c/sh: DISTANCE FROM MID x TIME REMAINING (touch vs. depth) ===")
    print(ld.groupby(["dist_bin", "tau_bin"], observed=False)
            .apply(wavg, include_groups=False).unstack().round(3).to_string())
    vd = ld.groupby(["dist_bin", "tau_bin"], observed=False)["sz"].sum().unstack()
    print("\n=== Volume (%) ===")
    print((vd / vd.values.sum() * 100).round(1).to_string())

    lp = live.dropna(subset=["dist_bin", "p_bin"])
    print("\n=== RAW c/sh: DISTANCE x PRICE ===")
    print(lp.groupby(["dist_bin", "p_bin"], observed=False)
            .apply(wavg, include_groups=False).unstack().round(3).to_string())

    print("\n=== STABILITY: RAW c/sh BY WEEK (key zones) ===")
    zones = {
        "GLOBAL live":                  live,
        "touch, all zones":             live[live["dist"] <= 0.004],
        "1-3.4c from mid":              live[(live["dist"] > 0.004) & (live["dist"] <= 0.034)],
        "deep >=3.5c":                  live[live["dist"] >= 0.035],
        "near-certain tau<120 p>.65":   live[(live["tau"] <= 120) & (live["p"] > 0.65)],
        "early fade p>.85 tau>1800":    live[(live["tau"] > 1800) & (live["p"] > 0.85)],
        "dip p<.15 tau>1800":           live[(live["tau"] > 1800) & (live["p"] < 0.15)],
        "ATM body .35-.65 tau>300":     live[(live["tau"] > 300) & (live["p"].between(0.35, 0.65))],
    }
    weeks = sorted(live["week"].unique())
    tbl = pd.DataFrame({z: {w: wavg(x[x["week"] == w]) for w in weeks}
                        for z, x in zones.items()}).T
    tbl["vol_total_sh"] = [zones[z]["sz"].sum() for z in tbl.index]
    tbl["raw_c_sh"]     = [wavg(zones[z]) for z in tbl.index]
    print(tbl.round(3).to_string())

    print("\n=== RAW c/sh BY HOUR ET (session) ===")
    byh = live.groupby("hour_et").apply(wavg, include_groups=False)
    volh = live.groupby("hour_et")["sz"].sum()
    print(pd.DataFrame({"raw_c_sh": byh.round(3),
                        "vol_%": (volh / volh.sum() * 100).round(1)}).to_string())


if __name__ == "__main__":
    main()
