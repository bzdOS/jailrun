import asyncio, sys, time, logging
logging.disable(logging.WARNING)
sys.path.insert(0, "/mnt")
from runtime.engine import _run_async

ITER = 1000
PAR = 8
IMAGE_ID = "706d6345edecb4af082185dff46e47a704f512d7a613577fbbf2a1d53e6fedc6"
OPTS = {"image_id": IMAGE_ID, "rm": True, "timeout": 30,
        "rctl_enabled": True, "rctl_rules": (), "network": "none",
        "allow_raw_sockets": False}

async def run_one(i):
    try:
        await _run_async("alpine:latest", ["echo", f"ok-{i}"], OPTS)
        return True
    except Exception as e:
        print(f"FAIL {i}: {e}")
        return False

async def main():
    t0 = time.time()
    print(f"soak: {ITER} x {PAR} start: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    ok = fail = 0
    sem = asyncio.Semaphore(PAR)
    async def worker(i):
        async with sem:
            return await run_one(i)
    tasks = [worker(i) for i in range(1, ITER + 1)]
    for i, coro in enumerate(asyncio.as_completed(tasks), 1):
        if await coro: ok += 1
        else: fail += 1
        if i % 100 == 0:
            print(f"progress: {i}/{ITER} ok={ok} fail={fail} ({time.time()-t0:.0f}s)")
    elapsed = time.time() - t0
    print(f"result: {ok} ok, {fail} fail in {elapsed:.0f}s")
    import subprocess
    nj = subprocess.run(["jail", "-q", "name"], capture_output=True, text=True)
    jails = [l for l in nj.stdout.splitlines() if "jailrun" in l]
    zo = subprocess.run(["zfs", "list", "-r", "jailrun/runs", "-H", "-o", "name"], capture_output=True, text=True)
    datasets = [l for l in zo.stdout.splitlines() if l != "jailrun/runs"]
    mo = subprocess.run(["mount"], capture_output=True, text=True)
    mounts = [l for l in mo.stdout.splitlines() if "jailrun/runs/" in l]
    print(f"leaks: {len(jails)} jails  {len(datasets)} datasets  {len(mounts)} mounts")

asyncio.run(main())
