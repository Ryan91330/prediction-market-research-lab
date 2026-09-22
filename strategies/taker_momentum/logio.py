"""Output: console + CSV files under state/{asset}/. Creates the directory on import.
CSVs are namespaced per asset (state/btc/, state/eth/, ...): under the multi-asset launcher, N processes write in
parallel, so without namespacing they would stomp on the SAME files (corruption). The [ASSET] console prefix also
lets you tell the N processes' interleaved stdout apart."""
import csv
import os
import pathlib
from datetime import datetime, timezone

import config as C

# STATE_TAG: optional suffix for both the state directory and the console tag, so two processes for the SAME asset
# can run with SEPARATE logs (e.g. a default config vs. an experimental variant of the same asset side by side).
# Empty by default = unchanged behavior.
_STAG = os.getenv("STATE_TAG", "").strip()
_TAG = C.ASSET.upper() + (f":{_STAG}" if _STAG else "")
_STATE = os.path.join("state", C.ASSET + (f"-{_STAG}" if _STAG else ""))   # one subdirectory per asset(+tag) = no clobbering
pathlib.Path(_STATE).mkdir(parents=True, exist_ok=True)
FILLS_LOG   = os.path.join(_STATE, "fills_log.csv")
CANDLES_LOG = os.path.join(_STATE, "candles_log.csv")
FEED_LOG    = os.path.join(_STATE, "feed_log.csv")   # feed health + what the signal was seeing
FAK_LOG     = os.path.join(_STATE, "fak_log.csv")    # FAK execution quality: fill rate + the REAL fill price
                                                     # (via balance delta) vs. the limit price
LAT_LOG     = os.path.join(_STATE, "lat_log.csv")    # per-stage latency: kline close -> received -> decided ->
                                                     # POSTed -> fill confirmed
SHADOW_LOG  = os.path.join(_STATE, "shadow1s_log.csv")  # measurement campaign: kline_1s close vs. a locally
                                                     # reconstructed 1s grid (from raw ticks)
GATE_LOG    = os.path.join(_STATE, "gate_log.csv")   # gate state at every fill (pwin/discount/stale/score) — used
                                                     # to attribute live P&L by entry-margin bucket and to monitor
                                                     # the stale-quote boost in production
LEADRATIO_LOG = os.path.join(_STATE, "leadratio_log.csv")  # R = Δmid_Polymarket/Δfair (lagging vs. informed discount)
EXIT_LOG    = os.path.join(_STATE, "exit_log.csv")   # near-tie exits (directional stop-loss / lock-win hedges)
EFCF_LOG    = os.path.join(_STATE, "ef_cf_log.csv")  # paired early-fire counterfactual (ask at the early-fire tick
                                                     # vs. ask when the real kline for the same event arrives) —
                                                     # the causal value of the timing advantage, without the
                                                     # selection bias of comparing early-fire vs. non-early-fire
                                                     # trades directly


def now_utc():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    print(f"[{now_utc()}][{_TAG:>4}] {msg}", flush=True)


def append_csv(path, header, row):
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        w.writerow(row)


def log_fill(slug, side, shares, cost, mid_now, tau, z):
    append_csv(FILLS_LOG,
        ["utc", "slug", "side", "shares", "cost", "mid", "tau_s", "z"],
        [_iso(), slug, side, round(shares, 2), round(cost, 3),
         round(mid_now, 3), round(tau, 1), round(z, 2)])


def log_candle(slug, up_sh, dn_sh, up_cost, dn_cost, balance):
    append_csv(CANDLES_LOG,
        ["utc", "slug", "up_sh", "dn_sh", "up_cost", "dn_cost", "balance"],
        [_iso(), slug, round(up_sh, 2), round(dn_sh, 2),
         round(up_cost, 3), round(dn_cost, 3), round(balance, 2)])


def log_feed(slug, elapsed, tau, z, ret_abs, d, fired, side, price):
    """Periodic trace + one line per fill: feed health + what the signal saw/would have done."""
    append_csv(FEED_LOG,
        ["utc", "slug", "elapsed_s", "tau_s", "z", "ret_abs", "feed_lag_s", "msg_age_s",
         "n_real", "npts", "fired", "side", "price"],
        [_iso(), slug, round(elapsed, 1), round(tau, 1),
         round(z, 2) if z is not None else "", round(ret_abs, 5),
         round(d["lag"], 2), round(d["msg_age"], 2), d["n_real"], d["npts"],
         int(fired), side or "", round(price, 3) if price else ""])


def log_leadratio(slug, side, R, dmid, dfair, dec, pwin, ask, z, tau, status):
    """LEAD-RATIO: R = Δmid_Polymarket/Δfair over the pre-fire window + the discount, on every fire AND every
    ratio-based skip. status: 'fire' (trade taken) | 'skip_ratio' (skipped as an informed-discount trap)."""
    append_csv(LEADRATIO_LOG,
        ["utc", "slug", "side", "R", "dmid", "dfair", "dec", "pwin", "ask", "z", "tau_s", "status"],
        [_iso(), slug, side,
         (round(R, 3) if R is not None else ""), (round(dmid, 4) if dmid is not None else ""),
         (round(dfair, 4) if dfair is not None else ""), (round(dec, 3) if dec is not None else ""),
         (round(pwin, 3) if pwin is not None else ""), round(ask, 3),
         round(z, 2) if z is not None else "", round(tau, 1), status])


def log_exit(side, req_sh, got_sh, avg_real, floor, status, mode="sell", ref_px=None, locked=None):
    """Near-tie exit: logs every attempt (fill/partial/no-fill/rejected/error/too-small/none).
    v2 (buy-opposite): +mode ('sell' legacy | 'lose'/'lockwin' = hedge branch), +ref_px (the opposite side's ask at
    decision time), +locked_pnl ($ locked in by the hedge pairs, before fees — the telemetry used to recalibrate
    EXIT_LOCKWIN_MAXP). The older 7-column schema is renamed to exit_log_v1.csv on first write of the new schema
    (never mix schemas within one CSV)."""
    if os.path.exists(EXIT_LOG):
        try:
            with open(EXIT_LOG) as f:
                if "mode" not in f.readline():
                    os.replace(EXIT_LOG, EXIT_LOG.replace(".csv", "_v1.csv"))
        except Exception:
            pass
    append_csv(EXIT_LOG,
        ["utc", "side", "mode", "req_sh", "got_sh", "avg_real", "floor", "ref_px", "locked_pnl", "status"],
        [_iso(), side, mode, round(req_sh, 2), round(got_sh, 2),
         (round(avg_real, 4) if avg_real else ""), floor,
         (round(ref_px, 3) if ref_px else ""), (round(locked, 4) if locked is not None else ""), status])


def log_fak(side, ask, limit, req_sh, got_sh, avg_real, status):
    """FAK execution quality — including trades that were entirely missed (status rejected/nofill/error)."""
    append_csv(FAK_LOG,
        ["utc", "side", "ask", "limit", "req_sh", "got_sh", "avg_real", "status"],
        [_iso(), side, round(ask, 3), limit, req_sh, round(got_sh, 2),
         (round(avg_real, 4) if avg_real else ""), status])


# --- Cross-asset direction registry (leader-lock mechanism): state/_xdir/{start_ts}.csv, shared across the N
# processes (atomic POSIX append for a short line). Written on first fill, read before every alt-asset entry.
pathlib.Path(C.XDIR_DIR).mkdir(parents=True, exist_ok=True)


def xdir_write(start_ts, side):
    """Publishes OUR commitment (asset, side) for this window. Best-effort: a failure here never blocks trading."""
    try:
        with open(os.path.join(C.XDIR_DIR, f"{int(start_ts)}.csv"), "a") as f:
            f.write(f"{C.ASSET},{side},{_iso()}\n")
    except Exception as e:
        log(f"⚠️ xdir_write: {e}")


def xdir_leader_side(start_ts, leader):
    """Side committed by `leader` on this window, or None. A missing/corrupt file means None (no veto)."""
    try:
        with open(os.path.join(C.XDIR_DIR, f"{int(start_ts)}.csv")) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 2 and parts[0] == leader:
                    return parts[1]
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"⚠️ xdir_read: {e}")
    return None


def xdir_prune(now_ts):
    """Deletes registry files for windows that closed more than 15 minutes ago (best-effort, called at the start
    of each candle)."""
    try:
        for fn in os.listdir(C.XDIR_DIR):
            base = fn.split(".")[0]
            if base.isdigit() and int(base) < now_ts - 900:
                os.unlink(os.path.join(C.XDIR_DIR, fn))
    except Exception:
        pass


def log_gate(slug, side, pwin, ask, tau, z, stale_mult, score, shares, cost, ncross=None):
    """One line per FILL: what the gate saw (pwin, decision-time ask, discount) + the sizing multipliers applied.
    Used to reconcile against settlement outcomes and attribute live P&L by discount bucket / entry-score bucket.
    +ncross: checks the live distribution/win-rate of the ncross buckets under the NCROSS_MAX filter.
    The schema is versioned: an older file without the `ncross` column is renamed to gate_log_v1.csv on first
    write of the new schema."""
    if os.path.exists(GATE_LOG):
        try:
            with open(GATE_LOG) as f:
                if "ncross" not in f.readline():
                    os.replace(GATE_LOG, GATE_LOG.replace(".csv", "_v1.csv"))
        except Exception:
            pass
    append_csv(GATE_LOG,
        ["utc", "slug", "side", "pwin", "ask", "decote", "tau_s", "z", "stale_mult", "score", "shares", "cost", "ncross"],
        [_iso(), slug, side, round(pwin, 4) if pwin is not None else "", round(ask, 3),
         round(pwin - ask, 4) if pwin is not None else "", round(tau, 1), round(z, 2) if z is not None else "",
         stale_mult, score if score is not None else "", round(shares, 2), round(cost, 4),
         ncross if ncross is not None else ""])


def log_shadow(sec, close_kline, close_local, t_local, t_kline):
    """One line per second: kline close vs. locally-reconstructed close + how late each one became available
    (ms after the end of that second). adv_ms > 0 means the local close was available BEFORE the kline (the
    advantage a local-tick source would capture). Empty close_kline means the kline was never received for that
    second (a gap in the Binance feed)."""
    eq = "" if (close_kline is None or close_local is None) else int(abs(close_kline - close_local) < 1e-9)
    append_csv(SHADOW_LOG,
        ["utc", "sec", "close_kline", "close_local", "eq", "t_local_ms", "t_kline_ms", "adv_ms"],
        [_iso(), sec,
         close_kline if close_kline is not None else "",
         close_local if close_local is not None else "", eq,
         round((t_local - (sec + 1)) * 1000.0, 1),
         round((t_kline - (sec + 1)) * 1000.0, 1) if t_kline else "",
         round((t_kline - t_local) * 1000.0, 1) if t_kline else ""])


def log_lat(slug, side, status, got, ask, e_close_ms, t_recv, t_decision, t_sent, t_ret, t_conf,
            src="", ask_ret=None):
    """Breaks down per-stage latency (ms) for one order attempt. All t* values are wall-clock time.time();
    e_close_ms = close time of the last kline (ms, Binance event-time). Stages:
      feed_ms = received − e_close (Binance transport+push; includes a structural ~1s if we stamp kline OPEN time)
      sig_ms  = decision − received (loop wait + signal computation)
      send_ms = POST sent − decision      rtt_ms = POST response − sent (order-matcher round trip)
      conf_ms = fill confirmed − POST response    total_ms = confirmed − e_close
    Used to see where the latency budget actually goes (structural vs. recoverable) and to see what the POST
    response itself contains. `src` = ef|kl, recorded AT DECISION TIME (provisional early-fire close vs. kline —
    avoids inferring it after the fact); `ask_ret` = the traded side's ask re-read at the POST response, i.e. how
    much the ask moved while the order was in flight to the matcher."""
    if os.path.exists(LAT_LOG):
        try:
            with open(LAT_LOG) as f:
                if "src" not in f.readline():
                    os.replace(LAT_LOG, LAT_LOG.replace(".csv", "_v1.csv"))
        except Exception:
            pass
    def ms(a, b):
        return round((a - b) * 1000.0, 1) if (a and b) else ""
    feed_ms = round(t_recv * 1000.0 - e_close_ms, 1) if (t_recv and e_close_ms) else ""
    total_ms = round(t_conf * 1000.0 - e_close_ms, 1) if (t_conf and e_close_ms) else ""
    append_csv(LAT_LOG,
        ["utc", "slug", "side", "status", "got", "ask",
         "feed_ms", "sig_ms", "send_ms", "rtt_ms", "conf_ms", "total_ms", "src", "ask_ret"],
        [_iso(), slug, side, status, round(got, 2), round(ask, 3),
         feed_ms, ms(t_decision, t_recv), ms(t_sent, t_decision),
         ms(t_ret, t_sent), ms(t_conf, t_ret), total_ms,
         src, round(ask_ret, 3) if ask_ret else ""])


def log_efcf(slug, side, sec, ask_fire, ask_kline, wait_ms, got, kline_ok):
    """One line per early-fire trigger (paired telemetry): the traded side's ask at the moment of the early-fire
    tick vs. the SAME ask re-read when the real kline for that same second arrives — i.e. what the kline-only path
    would have seen on the exact same event. dask_c > 0 means the ask moved up while the early-fire path was
    already ahead = the entry-price benefit of firing early, measured per-event without the selection bias of
    comparing early-fire vs. non-early-fire trades across different events. got>0 means our own fill already
    consumed the book before the re-read, contaminating ask_kline (overstates the benefit) — separate out got==0
    when reconciling this log. kline_ok=0 means the kline never arrived for that second (a feed gap)."""
    append_csv(EFCF_LOG,
        ["utc", "slug", "side", "sec", "ask_fire", "ask_kline", "dask_c", "wait_ms", "got", "kline_ok"],
        [_iso(), slug, side, int(sec), round(ask_fire, 3),
         round(ask_kline, 3) if ask_kline else "",
         round((ask_kline - ask_fire) * 100.0, 2) if (ask_kline and ask_fire) else "",
         round(wait_ms, 1), round(got, 2) if got is not None else "", int(kline_ok)])
