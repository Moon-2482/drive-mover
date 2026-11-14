"""Download a public MEGA folder. Usage: mega_dl.py <handle> <key> <outdir>.
Uses async-mega (proven route, Nov 2026) with the proxy trust_env patch.
Prints TOTAL files downloaded. Exit non-zero on failure.
"""
import asyncio, os, sys
# On the local VM, async-mega lives in .pydeps2; on a host it is pip-installed.
if os.path.isdir("/home/hatch/workspace/.pydeps2"):
    sys.path.insert(0, "/home/hatch/workspace/.pydeps2")
import aiohttp
_orig_init = aiohttp.ClientSession.__init__
def _patched_init(self, *a, **kw):
    kw.setdefault("trust_env", True)
    return _orig_init(self, *a, **kw)
aiohttp.ClientSession.__init__ = _patched_init
from mega.client import MegaNzClient

async def main(handle, key, out):
    mega = MegaNzClient()
    results = await mega.download_public_folder(handle, key, out, None)
    ok = sum(1 for v in results.values() if v)
    print("TOTAL nodes:", len(results), "ok:", ok)
    if ok == 0 and results:
        raise SystemExit("MEGA download returned no files")
    if not results:
        raise SystemExit("MEGA folder empty or not accessible")

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
