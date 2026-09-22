"""Multi-asset launcher — starts one `main_taker.py` process per asset, from a single command.

WHY THIS DESIGN (the safest option): each asset gets its OWN process, own CLOB client, own SpotVel instance, own
memory, own crash domain, own `state/{asset}/`. NO CLOB client is ever shared -> zero thread-safety risk on order
submission (the real danger of a shared single-process design: two concurrent orders on the same client can
corrupt nonces/signatures on live funds). This is functionally identical to running N separate containers, just
started with one command. The ONLY shared resource is the account itself (as today): sizing is throttled
approximately via each process's own MAX_EXPOSURE_PCT/contract; cancel_all is harmless under FAK-only execution.
The trading logic itself (main_taker/strategy/market) is untouched by this launcher.

Robustness: staggered boot (avoids N simultaneous build_client/cancel_all calls); per-asset restart with
exponential backoff (a crash in one asset doesn't stop the others); SIGTERM/SIGINT -> graceful shutdown (each
child gets SIGTERM, runs its own cancel_all, then a grace period before SIGKILL for stragglers).

Usage:  ASSETS=btc,eth,xrp python launch.py   |   ASSET=btc python launch.py (single asset)   |   +DRY_RUN=1 to paper-trade.
"""
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STAGGER_S   = float(os.getenv("LAUNCH_STAGGER_S", "2.0"))   # delay between each asset's boot
GRACE_S     = float(os.getenv("LAUNCH_GRACE_S", "25"))      # time given to each child for its own cancel_all before SIGKILL
BACKOFF_MAX = 60.0
STABLE_S    = 120.0                                          # a child that ran longer than STABLE_S resets its backoff to 1s


def parse_assets():
    raw = os.getenv("ASSETS") or os.getenv("ASSET") or "btc"
    out = []
    for a in raw.split(","):
        a = a.strip().lower()
        if a and a not in out:
            out.append(a)
    return out


def spawn(asset):
    env = dict(os.environ)
    env["ASSET"] = asset
    env.pop("ASSETS", None)                     # the child only ever sees ONE asset
    p = subprocess.Popen([sys.executable, "-u", "main_taker.py"], cwd=HERE, env=env)
    print(f"[LAUNCH] ▶ {asset} started (pid {p.pid})", flush=True)
    return p


class Supervisor:
    def __init__(self, assets):
        self.assets = assets
        self.procs = {}          # asset -> Popen | None (None = waiting to restart)
        self.started = {}        # asset -> time of last start
        self.backoff = {a: 1.0 for a in assets}
        self.restart_at = {}     # asset -> time to restart at
        self.running = True

    def start(self, a):
        self.procs[a] = spawn(a)
        self.started[a] = time.time()
        self.restart_at.pop(a, None)

    def shutdown(self, signum, _frame):
        if not self.running:
            return
        self.running = False
        print(f"[LAUNCH] 🛑 signal {signum} -> graceful shutdown (≤{GRACE_S:.0f}s/child)", flush=True)
        alive = [(a, p) for a, p in self.procs.items() if p and p.poll() is None]
        for _a, p in alive:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass
        t0 = time.time()
        for a, p in alive:
            rem = GRACE_S - (time.time() - t0)
            try:
                p.wait(timeout=max(1.0, rem))
                print(f"[LAUNCH] ✅ {a} stopped cleanly", flush=True)
            except subprocess.TimeoutExpired:
                print(f"[LAUNCH] ⏱ {a} took too long -> SIGKILL", flush=True)
                try:
                    p.kill()
                except Exception:
                    pass
        sys.exit(0)

    def loop(self):
        for a in self.assets:
            self.start(a)
            time.sleep(STAGGER_S)
        while self.running:
            time.sleep(1.0)
            now = time.time()
            for a in self.assets:
                if not self.running:
                    break
                p = self.procs.get(a)
                if p is not None and p.poll() is None:
                    continue                                     # alive
                if p is not None:                                # just died -> schedule a restart
                    rc = p.poll()
                    ran = now - self.started.get(a, now)
                    if ran > STABLE_S:
                        self.backoff[a] = 1.0
                    wait = min(BACKOFF_MAX, self.backoff[a])
                    self.backoff[a] = min(BACKOFF_MAX, self.backoff[a] * 2)
                    self.restart_at[a] = now + wait
                    self.procs[a] = None
                    print(f"[LAUNCH] ⚠ {a} exited (rc={rc}, ran {ran:.0f}s) -> restart in {wait:.0f}s", flush=True)
                elif now >= self.restart_at.get(a, 0):           # waiting and ready
                    self.start(a)


def main():
    assets = parse_assets()
    print(f"[LAUNCH] {len(assets)} asset(s): {assets} | stagger={STAGGER_S}s grace={GRACE_S}s", flush=True)
    sup = Supervisor(assets)
    signal.signal(signal.SIGINT, sup.shutdown)
    signal.signal(signal.SIGTERM, sup.shutdown)
    sup.loop()


if __name__ == "__main__":
    main()
