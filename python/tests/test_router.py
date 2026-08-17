from decimal import Decimal
from arb_bot.router import gas_aware_profit

def test_gas_aware_profit():
    value=gas_aware_profit(2_000_000, 200_000, 1_000_000_000, Decimal("3000"), 6, Decimal("1"))
    assert value == Decimal("1.4")
