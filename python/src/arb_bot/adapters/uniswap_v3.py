from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from .base import Quote

QUOTER_V2_ABI = [{
  "inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],
  "name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"sqrtPriceX96After","type":"uint160"},{"name":"initializedTicksCrossed","type":"uint32"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"
}]

class UniswapV3Quoter:
    def __init__(self, rpc_url: str, quoter_address: str, venue: str = "uniswap-v3"):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.contract = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(quoter_address), abi=QUOTER_V2_ABI)
        self.venue = venue

    async def quote_exact_input(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote:
        fee = kwargs.get("fee")
        if fee is None:
            raise ValueError("live Uniswap quote requires the discovered pool fee")
        block_identifier = kwargs.get("block_identifier", "pending")
        params = (AsyncWeb3.to_checksum_address(token_in), AsyncWeb3.to_checksum_address(token_out), amount_in, int(fee), 0)
        amount_out, sqrt_after, ticks, gas = await self.contract.functions.quoteExactInputSingle(params).call(block_identifier=block_identifier)
        latest = await self.w3.eth.get_block(block_identifier)
        block_number = latest.get("number") if isinstance(latest, dict) else getattr(latest, "number", None)
        return Quote(self.venue, token_in, token_out, amount_in, int(amount_out), int(gas), block_number, {"fee": int(fee), "ticks_crossed": int(ticks), "sqrt_after": int(sqrt_after), "block_identifier": str(block_identifier)})
