"""Soak test: run jailrun many times in parallel, verify zero leaks.

Bypasses the CLI resolve() step to avoid Docker Hub rate limits.
Uses the already-unpacked Alpine image (image_id known) directly.

Usage: sudo PYTHONPATH=/mnt python3 scripts/soak_runner.py [iterations] [parallelism]
"""

import asyncio, logging, sys, time

sys.path.insert(0, "/mnt")
from runtime.engine import _run_async

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")

ITER = int(sys.argv[1]) if len(sys.argv) > 1 else 200
PAR = int(sys.argv[2]) if len(sys.argv) > 2 else 8
IMAGE_ID = "706d6345edecb4af082185dff46e47a704f512d7a613577fbbf2a1d53e6fedc6"
OPTS = {
    "image_id": IMAGE_ID, "rm": True, "timeout": 30,
    "rctl_enabled": True, "rctl_rules": (), "network": "none",
    "allow_raw_sockets": False,
}

async def run_one(i: int) -> bool:
    try:
        await _run_async("alpine:latest", ["echo", f"ok-{i}"], OPTS)
        return True
    except Exception as e:
        log.error("run %d: %s", i, e)
        return False

async def main():
    print(f"soak: {ITER} x {PAR}  image_id={IMAGE_ID}")
    print(f"start: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    ok = fail = 0
    sem = asyncio.Semaphore(PAR)

    async def worker(i):
        async with sem:
            return await run_one(i)

    tasks = [worker(i) for i in range(1, ITER + 1)]
    for i, coro in enumerate(asyncio.as_completed(tasks), 1):
        if await coro:
            ok += 1
        else:
            fail += 1
        if i % max(1, ITER // 20) == 0 or i == ITER:
            print(f"progress: {i}/{ITER} ok={ok} fail={fail}")

    print(f"result: {ok} ok, {fail} fail")
    print(f"end: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")

    import subprocess
    nj = subprocess.run(["jail", "-q", "name"], capture_output=True, text=True)
    jails = [l for l in nj.stdout.splitlines() if "jailrun" in l]
    zo = subprocess.run(["zfs", "list", "-r", "jailrun/runs", "-H", "-o", "name"], capture_output=True, text=True)
    datasets = [l for l in zo.stdout.splitlines() if l != "jailrun/runs"]
    mo = subprocess.run(["mount"], capture_output=True, text=True)
    mounts = [l for l in mo.stdout.splitlines() if "jailrun/runs/" in l]
    print(f"leaks: {len(jails)} jails  {len(datasets)} datasets  {len(mounts)} mounts")
    if jails or datasets or mounts:
        for j in jails: print(f"  jail: {j}")
        for d in datasets: print(f"  dataset: {d}")
        for m in mounts: print(f"  mount: {m}")

if __name__ == "__main__":
    asyncio.run(main())
