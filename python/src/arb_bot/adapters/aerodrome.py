from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from .base import Quote

ROUTER_ABI = [{"inputs":[{"name":"amountIn","type":"uint256"},{"name":"routes","type":"tuple[]","components":[{"name":"from","type":"address"},{"name":"to","type":"address"},{"name":"stable","type":"bool"},{"name":"factory","type":"address"}]}],"name":"getAmountsOut","outputs":[{"name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}]

class AerodromeRouter:
    def __init__(self, rpc_url: str, router_address: str, factory_address: str, venue: str = "aerodrome"):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.contract = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(router_address), abi=ROUTER_ABI)
        self.factory = AsyncWeb3.to_checksum_address(factory_address)
        self.venue = venue

    async def quote_exact_input(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote:
        if "stable" not in kwargs or kwargs.get("stable") is None:
            raise ValueError("live Aerodrome quote requires the discovered pool stable flag")
        stable = bool(kwargs["stable"])
        block_identifier = kwargs.get("block_identifier", "pending")
        route = [(AsyncWeb3.to_checksum_address(token_in), AsyncWeb3.to_checksum_address(token_out), stable, self.factory)]
        amounts = await self.contract.functions.getAmountsOut(amount_in, route).call(block_identifier=block_identifier)
        latest = await self.w3.eth.get_block(block_identifier)
        block_number = latest.get("number") if isinstance(latest, dict) else getattr(latest, "number", None)
        # getAmountsOut is a quote call and does not reveal actual swap execution gas. Do not fabricate it.
        return Quote(self.venue, token_in, token_out, amount_in, int(amounts[-1]), None, block_number, {"stable": stable, "block_identifier": str(block_identifier)})
