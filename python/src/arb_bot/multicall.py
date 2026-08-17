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

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

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
    def __init__(self, rpc_url: str, address: str = MULTICALL3_ADDRESS) -> None:
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.contract = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(address), abi=AGGREGATE3_ABI
        )

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
        results = await self.contract.functions.aggregate3(payload).call(
            block_identifier=block_identifier
        )
        return [(bool(success), bytes(return_data)) for success, return_data in results]
