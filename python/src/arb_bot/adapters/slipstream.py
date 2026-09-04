from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from .base import Quote

# Aerodrome Slipstream is a concentrated-liquidity Uniswap V3 fork whose pool key is
# `tickSpacing` (int24) instead of `fee` (uint24). Its QuoterV2 mirrors Uniswap's, with
# that one field swapped, so the struct ordering below is otherwise identical.
SLIPSTREAM_QUOTER_ABI = [{
    "inputs": [{"components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "tickSpacing", "type": "int24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ], "name": "params", "type": "tuple"}],
    "name": "quoteExactInputSingle",
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]

# Output tuple of quoteExactInputSingle, used to decode raw multicall returndata.
QUOTE_OUTPUT_TYPES = ["uint256", "uint160", "uint32", "uint256"]


class SlipstreamQuoter:
    def __init__(self, rpc_url: str, quoter_address: str, venue: str = "aerodrome-slipstream"):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.contract = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(quoter_address), abi=SLIPSTREAM_QUOTER_ABI
        )
        self.venue = venue

    def _params(self, token_in: str, token_out: str, amount_in: int, tick_spacing) -> tuple:
        if tick_spacing is None:
            raise ValueError("live Slipstream quote requires the discovered pool tick spacing")
        return (
            AsyncWeb3.to_checksum_address(token_in),
            AsyncWeb3.to_checksum_address(token_out),
            amount_in,
            int(tick_spacing),
            0,
        )

    def encode_quote(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> tuple[str, bytes]:
        """Return (target, calldata) for batching this quote through Multicall3."""
        params = self._params(token_in, token_out, amount_in, kwargs.get("tick_spacing"))
        data = self.contract.functions.quoteExactInputSingle(params)._encode_transaction_data()
        return self.contract.address, bytes.fromhex(data[2:])

    def decode_quote(self, return_data: bytes, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote:
        """Decode raw Multicall3 returndata into a Quote (no extra RPC)."""
        tick_spacing = kwargs.get("tick_spacing")
        amount_out, sqrt_after, ticks, gas = self.w3.codec.decode(QUOTE_OUTPUT_TYPES, return_data)
        return Quote(
            self.venue, token_in, token_out, amount_in, int(amount_out), int(gas), kwargs.get("block_number"),
            {
                "tick_spacing": int(tick_spacing) if tick_spacing is not None else None,
                "ticks_crossed": int(ticks),
                "sqrt_after": int(sqrt_after),
                "block_identifier": str(kwargs.get("block_identifier", "pending")),
            },
        )

    async def quote_exact_input(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote:
        tick_spacing = kwargs.get("tick_spacing")
        block_identifier = kwargs.get("block_identifier", "pending")
        params = self._params(token_in, token_out, amount_in, tick_spacing)
        amount_out, sqrt_after, ticks, gas = await self.contract.functions.quoteExactInputSingle(
            params
        ).call(block_identifier=block_identifier)
        block_number = kwargs.get("block_number")
        if block_number is None:
            latest = await self.w3.eth.get_block(block_identifier)
            block_number = latest.get("number") if isinstance(latest, dict) else getattr(latest, "number", None)
        return Quote(
            self.venue, token_in, token_out, amount_in, int(amount_out), int(gas), block_number,
            {
                "tick_spacing": int(tick_spacing),
                "ticks_crossed": int(ticks),
                "sqrt_after": int(sqrt_after),
                "block_identifier": str(block_identifier),
            },
        )
