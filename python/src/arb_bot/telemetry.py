from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReceiptEconomics:
    gas_used: int
    effective_gas_price_wei: int
    l1_fee_wei: int
    actual_native_fee_wei: int
    asset_balance_before: int
    asset_balance_after: int
    realized_profit_units_ex_gas: int
    realized_net_profit_units: int | None
    net_profit_exact: bool


def reconcile_receipt_economics(*, gas_used: int, effective_gas_price_wei: int, l1_fee_wei: int, asset_balance_before: int, asset_balance_after: int, asset_is_wrapped_native: bool = False) -> ReceiptEconomics:
    if min(gas_used, effective_gas_price_wei, l1_fee_wei, asset_balance_before, asset_balance_after) < 0:
        raise ValueError("receipt economics values must be non-negative")
    actual_native_fee_wei = gas_used * effective_gas_price_wei + l1_fee_wei
    profit = asset_balance_after - asset_balance_before
    # The token balance delta is exact. Gas can only be subtracted without a price conversion
    # when the settlement asset is wrapped native (18 decimals, 1 wei == 1 raw unit).
    exact = asset_is_wrapped_native
    net = profit - actual_native_fee_wei if exact else None
    return ReceiptEconomics(
        gas_used=gas_used,
        effective_gas_price_wei=effective_gas_price_wei,
        l1_fee_wei=l1_fee_wei,
        actual_native_fee_wei=actual_native_fee_wei,
        asset_balance_before=asset_balance_before,
        asset_balance_after=asset_balance_after,
        realized_profit_units_ex_gas=profit,
        realized_net_profit_units=net,
        net_profit_exact=exact,
    )
