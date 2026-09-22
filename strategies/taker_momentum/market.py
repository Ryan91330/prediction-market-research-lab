"""Polymarket: CLOB v2 client, balance, market metadata (Gamma), order-book websocket, taker (FAK) orders."""
import asyncio
import json
import os
import time

import requests
import websockets
from dotenv import load_dotenv

from py_clob_client_v2 import ClobClient, OrderType, SignatureTypeV2
from py_clob_client_v2.clob_types import MarketOrderArgsV2, BalanceAllowanceParams, AssetType
from py_clob_client_v2.order_builder.constants import BUY, SELL

import config as C
from logio import log, log_fak, log_lat, log_exit


def build_client():
    load_dotenv()
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", 137))
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER")
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", 2))
    if not private_key or not funder:
        raise SystemExit("POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER missing from the environment (.env)")
    sig_map = {0: SignatureTypeV2.EOA, 1: SignatureTypeV2.POLY_PROXY,
               2: SignatureTypeV2.POLY_GNOSIS_SAFE, 3: SignatureTypeV2.POLY_1271}
    temp = ClobClient(host, key=private_key, chain_id=chain_id)
    creds = temp.create_or_derive_api_key()
    client = ClobClient(host, key=private_key, chain_id=chain_id, creds=creds,
                        signature_type=sig_map.get(sig_type, SignatureTypeV2.POLY_GNOSIS_SAFE), funder=funder)
    log("✅ CLOB v2 client initialized")
    # ECDSA backend: a native implementation is roughly an order of magnitude faster per signature than the
    # pure-Python fallback. Not a bottleneck on its own (it's a small fraction of the measured round-trip time),
    # but free to take if available.
    try:
        import coincurve  # noqa: F401
        log("🔐 native ECDSA (coincurve)")
    except ImportError:
        log("🔐 pure-Python ECDSA (slower per order) — coincurve is in requirements, rebuild the image to get it")
    return client


def get_balance(client):
    try:
        res = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return float(res.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"⚠️ balance: {e}"); return None


def get_token_balance(client, token_id):
    """Real (shares) balance of an outcome token = the AUTHORITATIVE source for the position actually held.
    Used as a fallback when get_order is slow to confirm, and to reconcile books at the end of a candle."""
    try:
        res = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id))
        return float(res.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"⚠️ token balance: {e}"); return None


# --- Bounded ASYNC wrappers: get_balance/get_token_balance are SYNCHRONOUS httpx calls. Called directly on the
# event loop, they would freeze it for the round-trip duration (book/spot processing stalls). to_thread moves the
# call off the loop; wait_for puts a hard bound on how long that's allowed to take.
async def aget_balance(client, timeout=C.NET_TIMEOUT_S):
    try:
        return await asyncio.wait_for(asyncio.to_thread(get_balance, client), timeout)
    except asyncio.TimeoutError:
        log(f"⚠️ get_balance > {timeout}s (loop not blocked) — None"); return None


async def aget_token_balance(client, token_id, timeout=C.NET_TIMEOUT_S):
    try:
        return await asyncio.wait_for(asyncio.to_thread(get_token_balance, client, token_id), timeout)
    except asyncio.TimeoutError:
        log(f"⚠️ get_token_balance > {timeout}s (loop not blocked) — None"); return None


def warm_market_cache(client, cond_id):
    """Pre-loads tick size / neg-risk / fee / condition mapping for both tokens of a contract in a single call.
    Goal: keep GET requests OFF the critical path of the first taker order. Without this, a contract's very first
    order (a token never seen before) triggers extra lookups inside the order-building call before the POST —
    a meaningful share of the median round-trip time. Called in the background at the start of the contract, so
    the first FAK order is just a single POST."""
    if not client or not cond_id:
        return
    try:
        client.get_clob_market_info(cond_id)
    except Exception as e:
        log(f"⚠️ warm cache {cond_id[:10]}…: {e}")


def fetch_market(slug):
    try:
        res = requests.get(C.GAMMA_URL + slug, timeout=5)
        if res.status_code == 200:
            d = res.json()
            outcomes = json.loads(d.get("outcomes", "[]"))
            tokens = json.loads(d.get("clobTokenIds", "[]"))
            cond_id = d.get("conditionId")
            if len(outcomes) == 2 and len(tokens) == 2 and cond_id:
                return outcomes, tokens, cond_id
    except Exception as e:
        log(f"⚠️ Gamma {slug}: {e}")
    return None, None, None


def best_from_book_event(data):
    """(best_bid, best_ask, bid_size@best, ask_size@best) — the sizes feed the imbalance component of the entry score."""
    bids = [(float(x["price"]), float(x.get("size", 0))) for x in (data.get("bids") or []) if float(x.get("size", 0)) > 0]
    asks = [(float(x["price"]), float(x.get("size", 0))) for x in (data.get("asks") or []) if float(x.get("size", 0)) > 0]
    bb = max(bids, key=lambda t: t[0]) if bids else (0.0, 0.0)
    ba = min(asks, key=lambda t: t[0]) if asks else (0.0, 0.0)
    return bb[0], ba[0], bb[1], ba[1]


def _best_from_lad(lad):
    """Top-of-book from the locally-maintained L2 ladder."""
    bids = [(p, s) for p, s in lad["BUY"].items() if s > 0]
    asks = [(p, s) for p, s in lad["SELL"].items() if s > 0]
    bb = max(bids, key=lambda t: t[0]) if bids else (0.0, 0.0)
    ba = min(asks, key=lambda t: t[0]) if asks else (0.0, 0.0)
    return bb[0], ba[0], bb[1], ba[1]


def _apply_best(st, bid, ask, bsz, asz, wake):
    """Propagates the top-of-book into shared state. Fail-closed: an empty side (0) is propagated as-is (the gate
    stays blocked whenever bid/ask<=0, as before). hist/wake are only updated on a VALID top that CHANGES: a high
    rate of book update events shouldn't spam either the stale-boost/lead-ratio history buffer or the decision loop."""
    changed = (bid != st["bid"]) or (ask != st["ask"])
    st["t"] = time.time()          # any event for this token counts as a freshness heartbeat (BOOK_TTL measures flow)
    if not changed:
        return
    st["bid"], st["ask"] = bid, ask
    if bsz > 0:
        st["bid_sz"] = bsz
    if asz > 0:
        st["ask_sz"] = asz
    if bid > 0 and ask > 0:
        h = st.get("hist")         # (t, bid, ask) history, used by the stale-quote boost and the lead-ratio filter
        if h is not None:
            h.append((st["t"], bid, ask))
        if wake is not None:
            wake.set()             # top of book moved -> wake the decision loop (react before it reprices further)


async def watch_books(token_ids, books, health, stop_event, wake=None):
    counts = {}; t_counts = time.time()   # periodic diagnostic: tracks the real mix of event types received, to
    while not stop_event.is_set():        # confirm the best_bid_ask/price_change/book handling below stays correct
        try:
            async with websockets.connect(C.CLOB_WS_URL, close_timeout=1) as ws:   # close_timeout: clean shutdown (see feed.py)
                await ws.send(json.dumps({"type": "market", "assets_ids": token_ids, "custom_feature_enabled": True}))
                # Local L2 ladder per token, seeded by periodic `book` snapshots and kept current by `price_change`
                # deltas: without maintaining our own ladder, the anchoring ask can go stale by a meaningful amount
                # during a burst of updates.
                lads = {t: {"BUY": {}, "SELL": {}} for t in token_ids}

                async def keep_alive():
                    while True:
                        await asyncio.sleep(C.WS_PING_S)
                        try:
                            await ws.send("PING")
                        except Exception:
                            break
                ping_task = asyncio.create_task(keep_alive())
                try:
                    while not stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        health["t"] = time.time()
                        if msg == "PONG" or not msg.strip():
                            continue
                        try:
                            events = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(events, list):
                            events = [events]
                        for ev in events:
                            et = ev.get("event_type") or "?"
                            counts[et] = counts.get(et, 0) + 1
                            tok = ev.get("asset_id")
                            if tok not in books:
                                continue
                            st = books[tok]
                            if et == "book":
                                lad = lads.get(tok)
                                if lad is not None:   # a snapshot is ground truth: reseed the ladder (repairs any missed delta)
                                    lad["BUY"] = {float(x["price"]): float(x.get("size", 0)) for x in (ev.get("bids") or [])}
                                    lad["SELL"] = {float(x["price"]): float(x.get("size", 0)) for x in (ev.get("asks") or [])}
                                bid, ask, bsz, asz = best_from_book_event(ev)
                                _apply_best(st, bid, ask, bsz, asz, wake)
                            elif et == "price_change":
                                lad = lads.get(tok)
                                if lad is None or (not lad["BUY"] and not lad["SELL"]):
                                    continue          # not yet seeded by a snapshot
                                for ch in (ev.get("changes") or [ev]):   # supports both a `changes` array and flat fields
                                    side = ch.get("side")
                                    if side not in ("BUY", "SELL") or ch.get("price") is None:
                                        continue
                                    p = float(ch["price"]); s = float(ch.get("size", 0) or 0)
                                    if s > 0:
                                        lad[side][p] = s
                                    else:
                                        lad[side].pop(p, None)
                                bid, ask, bsz, asz = _best_from_lad(lad)
                                _apply_best(st, bid, ask, bsz, asz, wake)
                            elif et == "best_bid_ask":
                                bid = float(ev.get("best_bid") or 0.0); ask = float(ev.get("best_ask") or 0.0)
                                # partial update: a missing side keeps the previous value; sizes unknown here
                                _apply_best(st, bid if bid > 0 else st["bid"], ask if ask > 0 else st["ask"], 0.0, 0.0, wake)
                        if time.time() - t_counts >= 300:
                            log("📊 book WS event mix (5 min): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
                            counts = {}; t_counts = time.time()
                finally:
                    ping_task.cancel()
        except Exception as e:
            if not stop_event.is_set():
                log(f"⚠️ book WS: {e} — reconnecting in 2s"); await asyncio.sleep(2)


async def keepalive_clob(client, stop_event):
    """Periodic get_ok() heartbeat: keeps the httpx connection to the CLOB warm (otherwise an idle connection is
    closed and the next order pays a fresh TLS handshake on its critical path).
    Only ONE get_ok in flight at a time: it's a synchronous request with no hard timeout, so a silent network hang
    could otherwise block a worker thread indefinitely. Only relaunching once the previous call has finished caps
    this at a single stuck thread and lets the heartbeat pause itself gracefully during a hang, instead of
    accumulating stuck threads over time."""
    fut = None
    while not stop_event.is_set():
        if fut is None or fut.done():
            fut = asyncio.ensure_future(asyncio.to_thread(_get_ok_quiet, client))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=C.CLOB_KEEPALIVE_S)
        except asyncio.TimeoutError:
            pass
    if fut is not None:
        fut.cancel()


def _get_ok_quiet(client):
    try:
        client.get_ok()
    except Exception:
        pass


async def place_taker(client, token_id, side_name, ask, cash_before=None, pwin=None, amount_usdc=None, lat=None,
                      ask_probe=None, max_walk=None, cap_override=None):
    """TAKER market FAK buy of `amount_usdc` dollars (dollar-fraction sizing computed by the strategy layer; $1
    minimum). Returns (shares, cost, new_balance) or None. `lat` = (slug, e_close_ms, t_recv, t_decision, src) for
    the latency log (per-stage breakdown + src=ef|kl). `ask_probe` = a zero-argument callable that re-reads the
    CURRENT ask of the traded side from the live book: sampled at the POST response to measure how much the ask
    moved while the order was in flight (ask_ret).
    `max_walk` = the walk budget for THIS attempt (EF_WALK on early-fire attempts, since their anchor tends to run
    slightly lower — see config). None = use the default configured walk.
    Limit price = pwin − FILL_MARGIN (fair-value anchored) when pwin is known and LIMIT_MODE='fair'; otherwise
    ask + TAKER_SLIP.
    No network calls happen before the POST (the critical path stays minimal for the best fill rate). The real
    cost is always taken from the balance delta measured AFTER the order (the order's own returned price field is
    the limit, not the executed price). Every outcome — including a fully missed order (rejected/no-fill/error) —
    is recorded to the FAK log."""
    walk = max_walk if max_walk is not None else C.MAX_WALK
    if cap_override is not None:      # exit-hedge path: an EXPLICIT limit price (clamped to MAX_PRICE), bypasses
        price_cap = round(min(cap_override, C.MAX_PRICE), 2)   # the fair-value/walk logic below
    elif C.LIMIT_MODE == "fair" and pwin is not None:
        price_cap = round(min(pwin - C.FILL_MARGIN, ask + walk, C.MAX_PRICE), 2)
    else:
        price_cap = round(min(ask + C.TAKER_SLIP, C.MAX_PRICE), 2)
    if price_cap < C.MIN_PRICE or price_cap > C.MAX_PRICE or price_cap <= ask:
        return None
    if amount_usdc is None:                                  # fallback (legacy share-count sizing)
        amount_usdc = C.LOT_SHARES * price_cap
    amount_usdc = round(max(C.TAKER_MIN_USDC, amount_usdc), 2)
    req_sh = round(amount_usdc / ask, 2) if ask > 0 else round(amount_usdc / price_cap, 2)
    if C.DRY_RUN:
        log(f"🧪[DRY] TAKER BUY {side_name} ~{req_sh}sh @≤{price_cap} (≈{amount_usdc}$)")
        return (req_sh, amount_usdc, None)

    slug = e_close_ms = t_recv = t_decision = None; lat_src = ""
    if lat:
        slug, e_close_ms, t_recv, t_decision, lat_src = lat
    def _probe():
        try:
            return ask_probe() if ask_probe else None
        except Exception:
            return None
    def _emit_lat(status, got, t_sent, t_ret, t_conf, ask_ret=None):
        if lat and C.LAT_LOG:
            log_lat(slug, side_name, status, got, ask, e_close_ms, t_recv, t_decision, t_sent, t_ret, t_conf,
                    lat_src, ask_ret)

    t_sent = time.time()
    try:
        resp = await asyncio.to_thread(
            client.create_and_post_market_order,
            MarketOrderArgsV2(token_id=token_id, amount=amount_usdc, side=BUY,
                              price=price_cap, order_type=OrderType.FAK),
            None, OrderType.FAK, False)
        t_ret = time.time()
        ask_ret = _probe()   # ask re-read at the response — measures the leak while the order was in flight
        oid = resp.get("orderID") if resp else None
        status_raw = str((resp or {}).get("status", "")).lower()
        if not oid:
            log(f"⚠️ REJECTED taker {side_name}@≤{price_cap}: {resp.get('errorMsg', resp) if resp else '?'}")
            log_fak(side_name, ask, price_cap, req_sh, 0.0, None, "rejected")
            _emit_lat("rejected", 0.0, t_sent, t_ret, t_ret, ask_ret); return None
        # FAST PATH: only short-circuit on an EXPLICIT "unmatched" status (nothing matched at all, unambiguous for
        # a FAK order). -> instant no-fill = an earlier retry within the short opportunity window = a less-repriced
        # ask. Any other status (matched, empty, live, delayed, unknown) falls through to the proven path (sleep +
        # confirm via balance delta) = zero risk of an uncounted fill.
        # "match" is a substring of "unmatched" -> test whole words, and any transaction hash cancels the fast path.
        is_unmatched = ("unmatched" in status_raw)
        has_match = bool((resp or {}).get("transactionsHashes")) or bool((resp or {}).get("transactionHashes")) \
            or ("matched" in status_raw and not is_unmatched)
        if is_unmatched and not has_match:
            log(f"… taker {side_name} not filled (FAK, status=unmatched, instant)")
            log_fak(side_name, ask, price_cap, req_sh, 0.0, None, "nofill")
            _emit_lat("nofill", 0.0, t_sent, t_ret, time.time(), ask_ret); return None
        # ===== 200 + orderID = the FAK MATCHED (otherwise it's the 400 path caught below) -> this is a fill. =====
        # Confirm the size by polling get_order until size_matched>0 (as opposed to reading once and giving up,
        # which can under-report a fill that lands a moment later and would then leave the cap/gap/cash tracking
        # stale). A 200 response is never reported back as a no-fill.
        got = 0.0; deadline = time.time() + C.CONFIRM_MAX
        while True:
            await asyncio.sleep(C.CONFIRM_S)
            try:
                # bounded: a hanging get_order must not stall place_taker beyond CONFIRM_MAX — the authoritative
                # token-balance fallback takes over regardless.
                o = await asyncio.wait_for(asyncio.to_thread(client.get_order, oid), C.NET_TIMEOUT_S)
                got = float((o or {}).get("size_matched", 0) or 0)
            except (asyncio.TimeoutError, Exception):
                pass
            if got > 0.01 or time.time() >= deadline:
                break
        new_bal = await aget_balance(client)   # threaded+bounded: doesn't freeze the loop during the round trip
        t_conf = time.time()
        real_cost = (cash_before - new_bal) if (cash_before is not None and new_bal is not None) else None
        # SIZE: get_order -> else the real token balance (authoritative) -> else an estimate. Never 0 on a 200.
        if got <= 0.01:
            pos = await aget_token_balance(client, token_id)
            if pos and pos > 0.01:
                got = pos; src = "position"
            else:
                got = round((real_cost / ask) if (real_cost and real_cost > 0 and ask > 0)
                            else (amount_usdc / price_cap), 2)
                src = "estimated"
            log(f"⚠️ FAK 200 but size unconfirmed by get_order -> {src} {got:.2f}sh (real fill, recording it anyway)")
        else:
            src = "get_order"
        # COST: balance delta (real) -> else a conservative fallback; returned cash always reflects the cost
        if real_cost and real_cost > 0.0:
            cost = round(real_cost, 4); ret_bal = new_bal
        else:
            cost = round(got * price_cap, 4)                     # fallback (overestimates = conservative for the cap)
            ret_bal = (cash_before - cost) if cash_before is not None else new_bal
        avg_real = (cost / got) if got > 0 else None
        status = "filled" if got >= req_sh * 0.95 else "partial"
        log(f"🎯 TAKER FILL {side_name} {got:.2f}/{req_sh:.2f}sh @ {avg_real:.3f} [{src}] ({oid[:10]}…)"
            if avg_real else f"🎯 TAKER FILL {side_name} {got:.2f}sh [{src}] ({oid[:10]}…)")
        log_fak(side_name, ask, price_cap, req_sh, got, avg_real, status)
        _emit_lat(status, got, t_sent, t_ret, t_conf, ask_ret)
        return (got, cost, ret_bal)
    except Exception as e:
        # A FAK killed for lack of a counterparty within the limit is a CLEAN no-fill (the book moved away during
        # the round trip), not an error: the CLOB returns it as an HTTP 400. Classified as nofill; logged quietly.
        t_ret = time.time()
        ask_ret = _probe()
        if "no orders found to match" in str(e).lower():
            log(f"… taker {side_name} not filled (FAK no-match, book moved)")
            log_fak(side_name, ask, price_cap, req_sh, 0.0, None, "nofill")
            _emit_lat("nofill", 0.0, t_sent, t_ret, t_ret, ask_ret); return None
        log(f"❌ place taker {side_name}@{price_cap}: {e}")
        log_fak(side_name, ask, price_cap, req_sh, 0.0, None, "error")
        _emit_lat("error", 0.0, t_sent, t_ret, t_ret, ask_ret); return None


async def place_taker_sell(client, token_id, side_name, qty, bid, cash_before=None):
    """Near-tie exit (legacy 'sell' mode): market FAK SELL of `qty` shares of a held token, to cut a loser before
    settlement. Price floor = max(bid − EXIT_SELL_SLIP, EXIT_MIN_SELL) — sweeps the bid ladder without dumping the
    position if the bid has collapsed (protects against selling a position that might still resolve as a winner).
    Returns (got, proceeds, new_balance) or None. Cost/proceeds are computed from the balance delta, same as
    place_taker (the order's own price field is the limit, not the executed price)."""
    qty = round(qty, 2)
    if qty < 0.01:
        return None
    floor = round(max((bid or 0.0) - C.EXIT_SELL_SLIP, C.EXIT_MIN_SELL), 2)
    if C.DRY_RUN:
        log(f"🧪[DRY] EXIT SELL {side_name} {qty}sh @≥{floor} (bid {bid})")
        return (qty, qty * floor, None)
    try:
        resp = await asyncio.to_thread(
            client.create_and_post_market_order,
            MarketOrderArgsV2(token_id=token_id, amount=qty, side=SELL, price=floor, order_type=OrderType.FAK),
            None, OrderType.FAK, False)
        oid = resp.get("orderID") if resp else None
        if not oid:
            log(f"⚠️ REJECTED exit-sell {side_name}@≥{floor}: {resp.get('errorMsg', resp) if resp else '?'}")
            log_exit(side_name, qty, 0.0, None, floor, "rejected"); return None
        got = 0.0; deadline = time.time() + C.CONFIRM_MAX
        while True:
            await asyncio.sleep(C.CONFIRM_S)
            try:
                o = await asyncio.wait_for(asyncio.to_thread(client.get_order, oid), C.NET_TIMEOUT_S)
                got = float((o or {}).get("size_matched", 0) or 0)
            except (asyncio.TimeoutError, Exception):
                pass
            if got > 0.01 or time.time() >= deadline:
                break
        new_bal = await aget_balance(client)
        proceeds = (new_bal - cash_before) if (cash_before is not None and new_bal is not None) else None
        avg = (proceeds / got) if (proceeds and proceeds > 0 and got > 0) else None
        status = "filled" if got >= qty * 0.95 else "partial"
        log(f"🔻 EXIT SELL {side_name} {got:.2f}/{qty:.2f}sh @ {avg:.3f} ({oid[:10]}…)" if avg
            else f"🔻 EXIT SELL {side_name} {got:.2f}sh ({oid[:10]}…)")
        log_exit(side_name, qty, got, avg, floor, status)
        return (got, proceeds, new_bal)
    except Exception as e:
        if "no orders found to match" in str(e).lower():
            log(f"… exit-sell {side_name} nofill (no bid ≥ {floor}, book collapsed) — holding to settlement")
            log_exit(side_name, qty, 0.0, None, floor, "nofill"); return None
        log(f"❌ exit-sell {side_name}@≥{floor}: {e}")
        log_exit(side_name, qty, 0.0, None, floor, "error"); return None
