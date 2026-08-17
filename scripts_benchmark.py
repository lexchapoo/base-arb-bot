import asyncio, hashlib, os, time
import httpx

async def probe(name,url):
    body={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}
    t=time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5) as c:r=await c.post(url,json=body);r.raise_for_status();ok="result" in r.json()
    except Exception:ok=False
    return {"provider":name,"url_hash":hashlib.sha256(url.encode()).hexdigest()[:12],"latency_ms":round((time.perf_counter()-t)*1000),"success":ok}
async def main():
    endpoints=[x.split("=",1) for x in os.getenv("RPC_BENCHMARK_ENDPOINTS","").split(",") if "=" in x]
    print(await asyncio.gather(*(probe(n,u) for n,u in endpoints)))
if __name__=="__main__":asyncio.run(main())
