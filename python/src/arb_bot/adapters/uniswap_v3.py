from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from .base import Quote

QUOTER_V2_ABI = [{
  "inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],
  "name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"sqrtPriceX96After","type":"uint160"},{"name":"initializedTicksCrossed","type":"uint32"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"
}]

# Output tuple of quoteExactInputSingle, used to decode raw multicall returndata.
QUOTE_OUTPUT_TYPES = ["uint256", "uint160", "uint32", "uint256"]

class UniswapV3Quoter:
    def __init__(self, rpc_url: str, quoter_address: str, venue: str = "uniswap-v3"):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.contract = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(quoter_address), abi=QUOTER_V2_ABI)
        self.venue = venue

    def _params(self, token_in: str, token_out: str, amount_in: int, fee) -> tuple:
        if fee is None:
            raise ValueError("live Uniswap quote requires the discovered pool fee")
        return (AsyncWeb3.to_checksum_address(token_in), AsyncWeb3.to_checksum_address(token_out), amount_in, int(fee), 0)

    def encode_quote(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> tuple[str, bytes]:
        """Return (target, calldata) for batching this quote through Multicall3."""
        params = self._params(token_in, token_out, amount_in, kwargs.get("fee"))
        data = self.contract.functions.quoteExactInputSingle(params)._encode_transaction_data()
        return self.contract.address, bytes.fromhex(data[2:])

    def decode_quote(self, return_data: bytes, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote:
        """Decode raw Multicall3 returndata into a Quote (no extra RPC)."""
        fee = kwargs.get("fee")
        amount_out, sqrt_after, ticks, gas = self.w3.codec.decode(QUOTE_OUTPUT_TYPES, return_data)
        return Quote(
            self.venue, token_in, token_out, amount_in, int(amount_out), int(gas), kwargs.get("block_number"),
            {"fee": int(fee) if fee is not None else None, "ticks_crossed": int(ticks), "sqrt_after": int(sqrt_after), "block_identifier": str(kwargs.get("block_identifier", "pending"))},
        )

    async def quote_exact_input(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote:
        fee = kwargs.get("fee")
        block_identifier = kwargs.get("block_identifier", "pending")
        params = self._params(token_in, token_out, amount_in, fee)
        amount_out, sqrt_after, ticks, gas = await self.contract.functions.quoteExactInputSingle(params).call(block_identifier=block_identifier)
        block_number = kwargs.get("block_number")
        if block_number is None:
            latest = await self.w3.eth.get_block(block_identifier)
            block_number = latest.get("number") if isinstance(latest, dict) else getattr(latest, "number", None)
        return Quote(self.venue, token_in, token_out, amount_in, int(amount_out), int(gas), block_number, {"fee": int(fee) if fee is not None else None, "ticks_crossed": int(ticks), "sqrt_after": int(sqrt_after), "block_identifier": str(block_identifier)})
