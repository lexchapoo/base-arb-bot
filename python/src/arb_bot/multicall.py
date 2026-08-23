"""Multicall3 batching for read-only quote calls.

Multicall3 is deployed at the same canonical address on every EVM chain
(Base included). `aggregate3` lets us collapse many independent `eth_call`
quotes into a single JSON-RPC request, which is the difference between one
network round-trip per candidate size and one per hop level.

Only read/quote calls are routed through here. Execution simulation and the
final profit/repayment authority remain unchanged (Rust re-simulates the exact
calldata; Solidity enforces min-profit and flash-loan repayment).
"""
from __future__ import annotations

import asyncio
import itertools

import aiohttp
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider


def endpoint_list(primary: str, extra: str) -> list[str]:
    """Ordered, de-duplicated read endpoints.

    `extra` is the comma-separated BASE_HTTP_RPCS list. The primary endpoint stays first so a
    single-endpoint deployment behaves exactly as before.
    """
    urls = [primary.strip()] if primary and primary.strip() else []
    for candidate in extra.split(","):
        candidate = candidate.strip()
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


class RotatingProviders:
    """Round-robins read calls across endpoints and fails over on error.

    Quote fan-out is bursty: one trigger issues a multicall and a balance query per cycle, all
    concurrently. Against a single endpoint that reliably trips provider rate limits, and a 429
    on the balance query kills the route outright (`live_balance_query_failed`). Spreading the
    calls and retrying the next endpoint turns a hard route failure into a slower one.
    """

    def __init__(self, urls: list[str], attempt_timeout_seconds: float = 1.5) -> None:
        if not urls:
            raise ValueError("at least one RPC endpoint is required")
        self.urls = urls
        # web3 leaves AsyncHTTPProvider without a request timeout, so a stalled endpoint blocks
        # far past the caller's deadline. With failover that cost multiplies: N unbounded
        # attempts in series. Bound each attempt so the whole chain still fits inside
        # route_evaluation_budget_seconds, or the budget guard kills the pass and every route in
        # it is discarded -- which is strictly worse than one endpoint failing fast.
        self.attempt_timeout_seconds = max(0.25, float(attempt_timeout_seconds))
        timeout = aiohttp.ClientTimeout(total=self.attempt_timeout_seconds)
        self._clients = [
            AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": timeout}))
            for url in urls
        ]
        self._counter = itertools.count()

    async def attempt(self, coro_factory):
        """Run `coro_factory(client)` against each endpoint in turn, bounded per attempt."""
        last_error: Exception | None = None
        for client in self.ordered():
            try:
                return await asyncio.wait_for(
                    coro_factory(client), timeout=self.attempt_timeout_seconds
                )
            except Exception as exc:  # timeout, rate limit, 5xx
                last_error = exc
        raise last_error if last_error else RuntimeError("no RPC endpoint available")

    def __len__(self) -> int:
        return len(self._clients)

    def ordered(self) -> list[AsyncWeb3]:
        """Every client once, starting at the next position in the rotation."""
        start = next(self._counter) % len(self._clients)
        return self._clients[start:] + self._clients[:start]

# Canonical Multicall3 deployment (identical address across chains).
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

AGGREGATE3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]


class MulticallClient:
    def __init__(
        self,
        rpc_url: str | list[str],
        address: str = MULTICALL3_ADDRESS,
        attempt_timeout_seconds: float = 1.5,
    ) -> None:
        urls = [rpc_url] if isinstance(rpc_url, str) else list(rpc_url)
        self.providers = RotatingProviders(urls, attempt_timeout_seconds)
        self.address = AsyncWeb3.to_checksum_address(address)
        # Preserved for callers/tests that reach for a single client.
        self.w3 = self.providers._clients[0]
        self.contract = self.w3.eth.contract(address=self.address, abi=AGGREGATE3_ABI)

    async def aggregate3(
        self,
        calls: list[tuple[str, bytes]],
        block_identifier: str = "pending",
    ) -> list[tuple[bool, bytes]]:
        """Execute `calls` (target, calldata) in one request.

        Every sub-call uses allowFailure=True so a single reverting quote (for
        example an over-large candidate size) never aborts the whole batch; the
        caller inspects the per-call success flag.
        """
        if not calls:
            return []
        payload = [
            (AsyncWeb3.to_checksum_address(target), True, call_data)
            for target, call_data in calls
        ]
        async def _call(client):
            contract = client.eth.contract(address=self.address, abi=AGGREGATE3_ABI)
            return await contract.functions.aggregate3(payload).call(
                block_identifier=block_identifier
            )

        results = await self.providers.attempt(_call)
        return [(bool(success), bytes(return_data)) for success, return_data in results]
