from arb_bot.telemetry import reconcile_receipt_economics


def test_receipt_native_fee_is_exact():
    r=reconcile_receipt_economics(gas_used=100_000,effective_gas_price_wei=2,l1_fee_wei=30,asset_balance_before=1_000,asset_balance_after=1_500)
    assert r.actual_native_fee_wei == 200_030
    assert r.realized_profit_units_ex_gas == 500
    assert r.realized_net_profit_units is None
    assert not r.net_profit_exact


def test_wrapped_native_net_profit_is_exact():
    r=reconcile_receipt_economics(gas_used=100,effective_gas_price_wei=2,l1_fee_wei=5,asset_balance_before=1_000,asset_balance_after=2_000,asset_is_wrapped_native=True)
    assert r.actual_native_fee_wei == 205
    assert r.realized_net_profit_units == 795
    assert r.net_profit_exact
