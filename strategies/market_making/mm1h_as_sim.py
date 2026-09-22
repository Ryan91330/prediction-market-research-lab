"""
AS SIM — Avellaneda-Stoikov market maker adapted to a binary outcome, BTC up/down HOURLY markets.

Architecture (see the strategy README for the reasoning):
  - Off-book binary fair value: p_hat = Phi(ln(S/K) / (sigma * sqrt(tau)))  (1s klines from the
    underlying spot leader, close available at t+1s)
  - Reservation price: r = p_hat - (q/Q_REF) * INV_GAMMA * phi(d)^2
  - Spread: delta_AS = (1/gamma) * ln(1 + gamma/kappa) + a staleness buffer STALE_K * sigma_c * sqrt(LAT+1)
  - Hawkes-lite intensity proxy (5s/120s realized-vol ratio): pull quotes if ratio > 3
  - Tolerance band (0.5c), emulated post-only, no-quote near strike into the final minutes + pre-open
  - Latency: decision made at step i using the book seen at i-1, becomes effective at i+LAT_STEPS
    (1s grid)
  - Fills: STRICT (traded-through) and TOUCH (touched) simulated in the same run

JUDGED METRIC = raw trading PnL (mo_exp); rebates are reported separately, never counted as the
primary edge (see README: rebates were roughly an order of magnitude too small to matter here).
Output: data/mm1h_sim_fills_{mode}.parquet, data/mm1h_sim_contracts_{mode}.csv, stdout report.
"""

import glob
import os
import re
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.special import ndtr

warnings.filterwarnings("ignore")

CFG = dict(
    CONTRACTS_GLOB = "data/pmdata/btc_1h/*.parquet",
    KLINES_DIR     = "data/btc_klines_1s",
    MAX_CONTRACTS  = None,
    # ---- Grid & latency ----
    GRID_MS        = 1000,        # 1s (1s klines; real measured latency ~0.5-1s)
    LAT_STEPS      = 1,           # decision -> effective: +1s
    # ---- Quoting ----
    SIZE_SHARES    = 20.0,
    Q_MAX          = 100.0,
    Q_REF          = 100.0,
    INV_GAMMA      = 0.30,
    AS_GAMMA       = 10.0,
    AS_KAPPA       = 600.0,
    STALE_K        = 2.0,
    MIN_HALF_SPREAD= 0.01,
    TOL_CENTER     = 0.005,
    TICK           = 0.01,
    P_MIN          = 0.03,
    P_MAX          = 0.97,
    # ---- Vol & Hawkes-lite ----
    VOL_HL_S       = 60.0,
    VOL_FAST_HL_S  = 5.0,
    VOL_SLOW_HL_S  = 120.0,
    HAWKES_PULL    = 3.0,
    SIGMA_FLOOR    = 3e-6,
    # ---- Risk guardian (1h-scale) ----
    NOQUOTE_TAU_S  = 240.0,       # near expiry ATM: gamma explodes (same convention as shorter windows)
    NOQUOTE_D      = 2.0,
    HARD_CUTOFF_S  = 60.0,
    PULL_K         = 3.0,
    PULL_COOLDOWN_S= 3.0,
    QUOTE_DELAY_S  = 10.0,
    HUMILITY_GAP   = None,
    MOM_S          = 30.0,
    # ---- Fees / rebates ----
    FEE_RATE       = 0.07,
    REBATE_SHARE   = 0.20,
    # ---- Fill ----
    DEDUPE_MS      = 30,
)
CFG["GRID_S"]   = CFG["GRID_MS"] / 1000.0
CFG["STEPS_1S"] = max(1, int(round(1000 / CFG["GRID_MS"])))

ET = ZoneInfo("America/New_York")
MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}
_SLUG_RE = re.compile(r"bitcoin-up-or-down-([a-z]+)-(\d+)(?:-(\d{4}))?-(\d+)(am|pm)-et")
PERIOD_S = 3600


def contract_start(path):
    m = _SLUG_RE.search(Path(path).stem)
    if not m:
        return None
    month, day, year, hh, ap = m.groups()
    year = int(year) if year else 2026
    h = int(hh) % 12 + (12 if ap == "pm" else 0)
    return int(datetime(year, MONTHS[month], int(day), h, tzinfo=ET).timestamp())


# -- Local 1s spot klines (1-day RAM cache) ----------------------------------
_DAY_RAM = {}

def _load_day(ds):
    path = os.path.join(CFG["KLINES_DIR"], f"BTCUSDT-1s-{ds}.csv")
    if not os.path.exists(path):
        print(f"[WARN] missing klines {ds}")
        return np.empty(0, np.int64), np.empty(0, np.float64)
    df = pd.read_csv(path, header=None, usecols=[0, 4], names=["ot", "close"])
    unit_div = {13: 1, 16: 1000, 10: 0.001, 19: 1_000_000}[len(str(abs(int(df["ot"].iloc[0]))))]
    tms = (df["ot"].values / unit_div).astype(np.int64) + 1000   # close available at open+1s (causal)
    return tms, df["close"].values.astype(np.float64)

def binance_window(t0_ms, t1_ms):
    """(timestamps_ms, close) covering [t0,t1], UTC days concatenated, 1-2 day cache."""
    d0 = pd.Timestamp(t0_ms, unit="ms").normalize()
    d1 = pd.Timestamp(t1_ms, unit="ms").normalize()
    needed = {d.strftime("%Y-%m-%d") for d in pd.date_range(d0, d1, freq="D")}
    for k in [k for k in _DAY_RAM if k not in needed]:
        del _DAY_RAM[k]
    chunks_t, chunks_p = [], []
    for ds in sorted(needed):
        if ds not in _DAY_RAM:
            _DAY_RAM[ds] = _load_day(ds)
        t, p = _DAY_RAM[ds]
        chunks_t.append(t); chunks_p.append(p)
    if not chunks_t:
        return np.empty(0, np.int64), np.empty(0, np.float64)
    t = np.concatenate(chunks_t); p = np.concatenate(chunks_p)
    i0, i1 = np.searchsorted(t, t0_ms, "left"), np.searchsorted(t, t1_ms, "right")
    return t[i0:i1], p[i0:i1]


# -- Per-contract grid --------------------------------------------------------
def build_contract_grid(path, cfg=CFG):
    cs_unix = contract_start(path)
    if cs_unix is None:
        return None, "unreadable slug"
    candle_start = pd.Timestamp(cs_unix, unit="s")
    candle_end   = candle_start + pd.Timedelta(seconds=PERIOD_S)

    df = pd.read_parquet(path, columns=["local_timestamp", "event_type", "best_bid", "best_ask",
                                        "trade_price", "trade_size", "trade_side", "winning_outcome"])
    df = df.dropna(subset=["local_timestamp"])
    if len(df) < 50:
        return None, "too few book events"

    GRID_MS = cfg["GRID_MS"]
    t_data0 = df["local_timestamp"].min()
    g0 = (int(t_data0.value // 1_000_000) // GRID_MS + 1) * GRID_MS
    g1 = int(candle_end.value // 1_000_000)
    grid_ms = np.arange(g0, g1 + GRID_MS, GRID_MS, dtype=np.int64)
    n = len(grid_ms)
    if n < 100:
        return None, "grid too short"
    ev_ms = df["local_timestamp"].values.astype("datetime64[ms]").astype(np.int64)

    # L1 forward-filled per step
    l1 = df[["best_bid", "best_ask"]].ffill()
    idx = np.searchsorted(ev_ms, grid_ms, side="right") - 1
    valid = idx >= 0
    bb = np.full(n, np.nan); ba = np.full(n, np.nan)
    bb[valid] = l1["best_bid"].values[idx[valid]]
    ba[valid] = l1["best_ask"].values[idx[valid]]

    # De-duplicated tape, bounded to [g0, g1] (the parquet continues ~20min post-close)
    tr = df[df["event_type"] == "last_trade_price"].dropna(subset=["trade_price", "trade_size"])
    sell_min = np.full(n, np.inf);  sell_vol = np.zeros(n)
    buy_max  = np.full(n, -np.inf); buy_vol  = np.zeros(n)
    if len(tr):
        tms  = tr["local_timestamp"].values.astype("datetime64[ms]").astype(np.int64)
        tpx  = tr["trade_price"].values.astype(np.float64)
        tsz  = tr["trade_size"].values.astype(np.float64)
        tbuy = (tr["trade_side"].values == "BUY")
        keep = np.ones(len(tr), dtype=bool)
        if len(tr) > 1:
            same = (np.diff(tms) < cfg["DEDUPE_MS"]) & (np.diff(tpx) == 0) & \
                   (np.diff(tsz) == 0) & (tbuy[1:] == tbuy[:-1])
            keep[1:] = ~same
        inwin = (tms >= g0) & (tms <= g1)
        keep &= inwin
        tms, tpx, tsz, tbuy = tms[keep], tpx[keep], tsz[keep], tbuy[keep]
        step = np.clip(np.searchsorted(grid_ms, tms, side="left"), 0, n - 1)
        s_m, b_m = ~tbuy, tbuy
        np.minimum.at(sell_min, step[s_m], tpx[s_m]);  np.add.at(sell_vol, step[s_m], tsz[s_m])
        np.maximum.at(buy_max,  step[b_m], tpx[b_m]);  np.add.at(buy_vol,  step[b_m], tsz[b_m])
    sell_min[np.isinf(sell_min)] = np.nan
    buy_max[np.isinf(buy_max)]   = np.nan

    # Underlying spot leader
    bms, bpx = binance_window(g0 - 600_000, g1)
    if len(bpx) < 200:
        return None, "insufficient spot-leader data"
    bix = np.searchsorted(bms, grid_ms, side="right") - 1
    if (bix < 0).all():
        return None, "spot leader does not cover window"
    bix = np.clip(bix, 0, len(bpx) - 1)
    S = bpx[bix]

    # EWMA vol + intensity ratio
    GRID_S = cfg["GRID_S"]
    logS = np.log(S)
    r = pd.Series(np.diff(logS, prepend=logS[0]))
    hl  = cfg["VOL_HL_S"] / GRID_S
    sig = r.ewm(halflife=hl, min_periods=int(hl)).std().values
    sig = np.maximum(np.nan_to_num(sig, nan=cfg["SIGMA_FLOOR"]), cfg["SIGMA_FLOOR"])
    sf  = r.ewm(halflife=cfg["VOL_FAST_HL_S"] / GRID_S).std().values
    ss  = r.ewm(halflife=cfg["VOL_SLOW_HL_S"] / GRID_S).std().values
    ratio = np.nan_to_num(sf / np.maximum(ss, cfg["SIGMA_FLOOR"]), nan=1.0)

    # Binary fair value (strike = last spot-leader close before open, backward-looking)
    cs_ms = int(candle_start.value // 1_000_000)
    i_live = int(np.searchsorted(grid_ms, cs_ms, side="left"))
    if i_live >= n - 10:
        return None, "no live phase"
    K = S[i_live]
    tau = np.maximum((g1 - grid_ms) / 1000.0, 1e-9)
    sig_rem = sig * np.sqrt(tau / GRID_S)
    d  = np.clip(np.log(S / K) / np.maximum(sig_rem, 1e-12), -8, 8)
    fv = ndtr(d)
    phi = np.exp(-0.5 * d * d) / np.sqrt(2 * np.pi)
    sig_c = phi * np.sqrt(GRID_S / tau)
    live = np.zeros(n, dtype=bool); live[i_live:] = True
    fv[~live] = np.nan

    # Risk flags
    k1 = cfg["STEPS_1S"]
    r1s = np.abs(logS - np.concatenate([np.full(k1, logS[0]), logS[:-k1]]))
    hard_pull = r1s > cfg["PULL_K"] * sig * np.sqrt(k1)
    noquote = (tau < cfg["HARD_CUTOFF_S"]) \
            | ((tau < cfg["NOQUOTE_TAU_S"]) & (np.abs(d) < cfg["NOQUOTE_D"])) \
            | (ratio > cfg["HAWKES_PULL"]) \
            | (grid_ms < cs_ms + int(cfg["QUOTE_DELAY_S"] * 1000))

    # Outcome: winning_outcome from the parquet (reliable), fallback to final mid then spot leader
    wo = df["winning_outcome"].dropna()
    if len(wo) and wo.iloc[-1] in ("yes", "no"):
        outcome_up, out_src = (wo.iloc[-1] == "yes"), "resolved"
    else:
        mid = (bb + ba) / 2.0
        last_mid = mid[~np.isnan(mid)][-1] if (~np.isnan(mid)).any() else np.nan
        if np.isnan(last_mid) or abs(last_mid - 0.5) < 0.15:
            outcome_up, out_src = bool(S[-1] > K), "spot_leader"
        else:
            outcome_up, out_src = bool(last_mid > 0.5), "mid"

    return dict(slug=Path(path).stem, candle_start=candle_start,
                grid_ms=grid_ms, n=n, i_live=i_live, S=S, K=K, bb=bb, ba=ba,
                sell_min=sell_min, sell_vol=sell_vol, buy_max=buy_max, buy_vol=buy_vol,
                sig=sig, ratio=ratio, tau=tau, d=d, fv=fv, phi=phi, sig_c=sig_c,
                live=live, hard_pull=hard_pull, noquote=noquote,
                outcome_up=outcome_up, outcome_src=out_src,
                week=datetime.fromtimestamp(cs_unix, tz=ET).strftime("%W"),
                hour_et=datetime.fromtimestamp(cs_unix, tz=ET).hour), None


# -- MM engine -----------------------------------------------------------------
def simulate_mm(g, fill_mode, cfg=CFG):
    n, i0  = g["n"], g["i_live"]
    LAT    = cfg["LAT_STEPS"]
    SIZE   = cfg["SIZE_SHARES"]
    TICK   = cfg["TICK"]
    strict = (fill_mode == "STRICT")
    reb_k  = cfg["REBATE_SHARE"] * cfg["FEE_RATE"]
    cool   = int(cfg["PULL_COOLDOWN_S"] / cfg["GRID_S"])
    stale_h = cfg["STALE_K"] * np.sqrt(LAT + 1)
    delta_as = (1.0 / cfg["AS_GAMMA"]) * np.log(1.0 + cfg["AS_GAMMA"] / cfg["AS_KAPPA"])

    fv, phi, sig_c = g["fv"], g["phi"], g["sig_c"]
    bb, ba = g["bb"], g["ba"]
    sell_min, sell_vol = g["sell_min"], g["sell_vol"]
    buy_max,  buy_vol  = g["buy_max"],  g["buy_vol"]
    noquote, hard_pull = g["noquote"], g["hard_pull"]

    q = 0.0; cash = 0.0; rebates = 0.0
    cur_bid = cur_ask = np.nan
    pending = []
    last_center = np.nan
    pull_until = -1
    fills = []
    max_abs_q = 0.0; steps_quoted = 0

    for i in range(i0, n):
        while pending and pending[0][0] <= i:
            _, cur_bid, cur_ask = pending.pop(0)

        if not np.isnan(cur_bid):
            hit = (sell_min[i] < cur_bid) if strict else (sell_min[i] <= cur_bid)
            crossed = (not np.isnan(ba[i])) and (ba[i] <= cur_bid)
            if hit or crossed:
                fs = min(SIZE, sell_vol[i]) if (hit and sell_vol[i] > 0) else SIZE
                q += fs; cash -= fs * cur_bid
                rebates += reb_k * fs * cur_bid * (1 - cur_bid)
                fills.append(dict(i=i, side=1, px=cur_bid, sz=fs, fv=fv[i], q_after=q,
                                  tau=g["tau"][i]))
                cur_bid = np.nan
        if not np.isnan(cur_ask):
            hit = (buy_max[i] > cur_ask) if strict else (buy_max[i] >= cur_ask)
            crossed = (not np.isnan(bb[i])) and (bb[i] >= cur_ask)
            if hit or crossed:
                fs = min(SIZE, buy_vol[i]) if (hit and buy_vol[i] > 0) else SIZE
                q -= fs; cash += fs * cur_ask
                rebates += reb_k * fs * cur_ask * (1 - cur_ask)
                fills.append(dict(i=i, side=-1, px=cur_ask, sz=fs, fv=fv[i], q_after=q,
                                  tau=g["tau"][i]))
                cur_ask = np.nan
        max_abs_q = max(max_abs_q, abs(q))

        if hard_pull[i]:
            pull_until = i + cool
        blocked = noquote[i] or (i <= pull_until) or np.isnan(fv[i])

        if blocked:
            want_bid = want_ask = np.nan
        else:
            center = fv[i] - (q / cfg["Q_REF"]) * cfg["INV_GAMMA"] * (phi[i] ** 2)
            hs = max(cfg["MIN_HALF_SPREAD"], delta_as + stale_h * sig_c[i])
            want_bid = np.floor((center - hs) / TICK) * TICK
            want_ask = np.ceil((center + hs) / TICK) * TICK
            pbb, pba = bb[i - 1], ba[i - 1]
            if not np.isnan(pba): want_bid = min(want_bid, pba - TICK)
            if not np.isnan(pbb): want_ask = max(want_ask, pbb + TICK)
            if q >= cfg["Q_MAX"]:  want_bid = np.nan
            if q <= -cfg["Q_MAX"]: want_ask = np.nan
            if not (cfg["P_MIN"] <= want_bid <= cfg["P_MAX"]): want_bid = np.nan
            if not (cfg["P_MIN"] <= want_ask <= cfg["P_MAX"]): want_ask = np.nan
            if not np.isnan(want_bid) and not np.isnan(want_ask) and want_bid >= want_ask:
                want_bid = want_ask = np.nan
            steps_quoted += 1

            if not np.isnan(last_center) and abs(center - last_center) < cfg["TOL_CENTER"] \
               and not pending \
               and (np.isnan(want_bid) == np.isnan(cur_bid)) \
               and (np.isnan(want_ask) == np.isnan(cur_ask)):
                continue
            last_center = center

        same_b = (np.isnan(want_bid) and np.isnan(cur_bid)) or (want_bid == cur_bid)
        same_a = (np.isnan(want_ask) and np.isnan(cur_ask)) or (want_ask == cur_ask)
        if not (same_b and same_a):
            pending.append((i + LAT, want_bid, want_ask))

    payout = 1.0 if g["outcome_up"] else 0.0
    settle = q * payout
    cash += settle

    for f in fills:
        for lbl, hs_ in (("mo_1s", 1), ("mo_5s", 5), ("mo_30s", 30), ("mo_120s", 120)):
            j = min(f["i"] + hs_ * cfg["STEPS_1S"], n - 1)
            f[lbl] = (fv[j] - f["px"]) * f["side"] if not np.isnan(fv[j]) else np.nan
        f["mo_exp"] = (payout - f["px"]) * f["side"]
        f["edge_fv"] = (f["fv"] - f["px"]) * f["side"] if not np.isnan(f["fv"]) else np.nan
        f["slug"] = g["slug"]; f["week"] = g["week"]; f["hour_et"] = g["hour_et"]

    summary = dict(slug=g["slug"], candle_start=g["candle_start"], week=g["week"],
                   hour_et=g["hour_et"], n_fills=len(fills),
                   pnl=cash + rebates, pnl_trading=cash, rebates=rebates,
                   settle=settle, q_end=q, max_abs_q=max_abs_q,
                   quoted_pct=steps_quoted / max(1, n - i0),
                   outcome_up=g["outcome_up"], outcome_src=g["outcome_src"],
                   vol_mean=float(np.nanmean(g["sig"][i0:])))
    return fills, summary


def report(df_f, df_s, mode):
    n_c = len(df_s)
    print("\n" + "=" * 66)
    print(f" AS SIM REPORT 1h — {n_c} contracts | {mode} | latency {CFG['LAT_STEPS']*CFG['GRID_MS']}ms")
    print("=" * 66)
    print(f" RAW TRADING PNL (the judge)  : {df_s['pnl_trading'].sum():+10.2f} $")
    print(f" Maker rebates (upper bound)  : {df_s['rebates'].sum():+10.2f} $")
    print(f" Raw PnL / contract           : {df_s['pnl_trading'].sum()/n_c:+10.4f} $")
    print(f" Winning contracts (raw)      : {(df_s['pnl_trading']>0).mean():10.1%}")
    print(f" Fills                        : {len(df_f):10d}  ({len(df_f)/n_c:.1f}/contract)")
    if len(df_f):
        print(f" Filled volume                : {(df_f['px']*df_f['sz']).sum():10.2f} $")
        print("-" * 66)
        print(" MARKOUTS c/share (raw, the truth about adverse selection)")
        for c in ["edge_fv", "mo_1s", "mo_5s", "mo_30s", "mo_120s", "mo_exp"]:
            v = df_f[c].dropna()
            print(f"   {c:8s} : {v.mean()*100:+7.3f} c/sh  (median {v.median()*100:+6.2f}, n={len(v)})")
        reb_sh = df_s["rebates"].sum() / df_f["sz"].sum()
        print(f"   rebate   : {reb_sh*100:+7.3f} c/sh -> net at expiry {(df_f['mo_exp'].mean()+reb_sh)*100:+.3f} c/sh")
        print("-" * 66)
        print(" Raw trading PnL by WEEK ($):")
        print(df_s.groupby("week")["pnl_trading"].agg(["sum", "count"]).round(2).to_string())
        print(" Worst contracts (raw):")
        print(df_s.nsmallest(5, "pnl_trading")[["slug", "pnl_trading", "n_fills", "max_abs_q", "settle"]]
              .to_string(index=False))


def main():
    paths = sorted(glob.glob(CFG["CONTRACTS_GLOB"]), key=contract_start)
    if CFG["MAX_CONTRACTS"]:
        paths = paths[:CFG["MAX_CONTRACTS"]]
    import time as _t
    t0 = _t.time()
    res = {m: ([], []) for m in ("STRICT", "TOUCH")}
    skips = {}
    for k, path in enumerate(paths):
        if k % 100 == 0:
            print(f"[{k}/{len(paths)}] {_t.time()-t0:.0f}s")
        try:
            g, skip = build_contract_grid(path)
            if g is None:
                skips[skip] = skips.get(skip, 0) + 1
                continue
            for mode in ("STRICT", "TOUCH"):
                fills, summary = simulate_mm(g, mode)
                res[mode][0].extend(fills)
                res[mode][1].append(summary)
        except Exception as e:
            skips[f"ERR {type(e).__name__}"] = skips.get(f"ERR {type(e).__name__}", 0) + 1
    print(f"\nDone in {(_t.time()-t0)/60:.1f} min | skips: {skips}")

    for mode in ("STRICT", "TOUCH"):
        df_f = pd.DataFrame(res[mode][0])
        df_s = pd.DataFrame(res[mode][1])
        df_f.to_parquet(f"data/mm1h_sim_fills_{mode}.parquet")
        df_s.to_csv(f"data/mm1h_sim_contracts_{mode}.csv", index=False)
        report(df_f, df_s, mode)


if __name__ == "__main__":
    main()
