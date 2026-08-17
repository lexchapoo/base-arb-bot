from dataclasses import dataclass
from decimal import Decimal
from .adapters.base import Quote

@dataclass(frozen=True)
class RoundTrip:
    buy: Quote
    sell: Quote
    gross_profit_units: int
    estimated_gas: int

async def evaluate_round_trip(buy_adapter, sell_adapter, token_a: str, token_b: str, amount_in: int, buy_kwargs=None, sell_kwargs=None) -> RoundTrip:
    buy = await buy_adapter.quote_exact_input(token_a, token_b, amount_in, **(buy_kwargs or {}))
    sell = await sell_adapter.quote_exact_input(token_b, token_a, buy.amount_out, **(sell_kwargs or {}))
    return RoundTrip(buy, sell, sell.amount_out - amount_in, buy.gas_estimate + sell.gas_estimate)

def gas_aware_profit(gross_units: int, gas_units: int, gas_price_wei: int, native_price_usd: Decimal, token_decimals: int, token_price_usd: Decimal) -> Decimal:
    gross_usd = (Decimal(gross_units) / (Decimal(10) ** token_decimals)) * token_price_usd
    gas_usd = (Decimal(gas_units * gas_price_wei) / Decimal(10**18)) * native_price_usd
    return gross_usd - gas_usd
