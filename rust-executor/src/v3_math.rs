//! Exact Uniswap V3 / Aerodrome Slipstream concentrated-liquidity math.
//!
//! This is a faithful integer port of Uniswap's `TickMath`, `SqrtPriceMath`, and `SwapMath`
//! libraries plus the `UniswapV3Pool.swap` accumulation loop. Every operation is integer-only
//! and must match on-chain results bit-for-bit: a quote that is one wei optimistic is a
//! transaction that reverts on `minOut`.
//!
//! Slipstream shares this math exactly; only the pool key (tickSpacing vs fee) differs, and
//! the fee still arrives as a pips value, so the same code serves both venues.

//! NOTE: this engine is validated (unit tests + a live QuoterV2 differential test) but is
//! not yet on the submission path -- Python still quotes via QuoterV2 eth_call. Wiring it in
//! is the next slice, so some helpers are unused for now.
#![allow(dead_code)]
use primitive_types::{U256, U512};

pub const MIN_TICK: i32 = -887272;
pub const MAX_TICK: i32 = 887272;

/// Q96 fixed point: 2^96.
pub fn q96() -> U256 {
    U256::one() << 96
}

pub fn min_sqrt_ratio() -> U256 {
    U256::from(4295128739u64)
}

pub fn max_sqrt_ratio() -> U256 {
    U256::from_dec_str("1461446703485210103287273052203988822378723970342").unwrap()
}

/// `FullMath.mulDiv`: floor(a * b / denominator) computed over 512 bits so the intermediate
/// product never overflows. Returns None on division by zero or a result exceeding 256 bits.
pub fn mul_div(a: U256, b: U256, denominator: U256) -> Option<U256> {
    if denominator.is_zero() {
        return None;
    }
    let product = U512::from(a).checked_mul(U512::from(b))?;
    let quotient = product / U512::from(denominator);
    if quotient > U512::from(U256::MAX) {
        return None;
    }
    let bytes = quotient.to_big_endian();
    Some(U256::from_big_endian(&bytes[32..]))
}

/// `FullMath.mulDivRoundingUp`.
pub fn mul_div_rounding_up(a: U256, b: U256, denominator: U256) -> Option<U256> {
    let result = mul_div(a, b, denominator)?;
    let product = U512::from(a).checked_mul(U512::from(b))?;
    if product % U512::from(denominator) != U512::zero() {
        result.checked_add(U256::one())
    } else {
        Some(result)
    }
}

/// `TickMath.getSqrtRatioAtTick` — the Q96 square-root price at a tick.
pub fn get_sqrt_ratio_at_tick(tick: i32) -> Option<U256> {
    if !(MIN_TICK..=MAX_TICK).contains(&tick) {
        return None;
    }
    let abs_tick = tick.unsigned_abs() as u64;

    // Magic constants are the fixed-point representations of 1.0001^(-2^i / 2), exactly as in
    // TickMath.sol. Verified against a high-precision computation of sqrt(1.0001^tick) * 2^96.
    const FACTORS: [(u64, &str); 20] = [
        (0x1, "fffcb933bd6fad37aa2d162d1a594001"),
        (0x2, "fff97272373d413259a46990580e213a"),
        (0x4, "fff2e50f5f656932ef12357cf3c7fdcc"),
        (0x8, "ffe5caca7e10e4e61c3624eaa0941cd0"),
        (0x10, "ffcb9843d60f6159c9db58835c926644"),
        (0x20, "ff973b41fa98c081472e6896dfb254c0"),
        (0x40, "ff2ea16466c96a3843ec78b326b52861"),
        (0x80, "fe5dee046a99a2a811c461f1969c3053"),
        (0x100, "fcbe86c7900a88aedcffc83b479aa3a4"),
        (0x200, "f987a7253ac413176f2b074cf7815e54"),
        (0x400, "f3392b0822b70005940c7a398e4b70f3"),
        (0x800, "e7159475a2c29b7443b29c7fa6e889d9"),
        (0x1000, "d097f3bdfd2022b8845ad8f792aa5825"),
        (0x2000, "a9f746462d870fdf8a65dc1f90e061e5"),
        (0x4000, "70d869a156d2a1b890bb3df62baf32f7"),
        (0x8000, "31be135f97d08fd981231505542fcfa6"),
        (0x10000, "9aa508b5b7a84e1c677de54f3e99bc9"),
        (0x20000, "5d6af8dedb81196699c329225ee604"),
        (0x40000, "2216e584f5fa1ea926041bedfe98"),
        (0x80000, "48a170391f7dc42444e8fa2"),
    ];

    let mut ratio = if abs_tick & 0x1 != 0 {
        U256::from_str_radix(FACTORS[0].1, 16).ok()?
    } else {
        U256::one() << 128
    };
    for (mask, hex) in FACTORS.iter().skip(1) {
        if abs_tick & mask != 0 {
            let factor = U256::from_str_radix(hex, 16).ok()?;
            ratio = (ratio.full_mul(factor) >> 128).try_into().ok()?;
        }
    }

    if tick > 0 {
        ratio = U256::MAX / ratio;
    }

    // Round up so the returned tick is always <= the tick of the true price.
    let shifted = ratio >> 32;
    let remainder = ratio % (U256::one() << 32);
    Some(if remainder.is_zero() { shifted } else { shifted + U256::one() })
}

/// `TickMath.getTickAtSqrtRatio` — inverse of the above, via a binary search that is exact
/// because `get_sqrt_ratio_at_tick` is monotonic over the valid tick range.
pub fn get_tick_at_sqrt_ratio(sqrt_price_x96: U256) -> Option<i32> {
    if sqrt_price_x96 < min_sqrt_ratio() || sqrt_price_x96 >= max_sqrt_ratio() {
        return None;
    }
    let (mut lo, mut hi) = (MIN_TICK, MAX_TICK);
    while lo < hi {
        let mid = lo + (hi - lo + 1) / 2;
        if get_sqrt_ratio_at_tick(mid)? <= sqrt_price_x96 {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    Some(lo)
}

/// `SqrtPriceMath.getAmount0Delta`.
pub fn get_amount0_delta(a: U256, b: U256, liquidity: u128, round_up: bool) -> Option<U256> {
    let (lower, upper) = if a > b { (b, a) } else { (a, b) };
    if lower.is_zero() {
        return None;
    }
    let numerator1 = U256::from(liquidity) << 96;
    let numerator2 = upper.checked_sub(lower)?;
    if round_up {
        let inner = mul_div_rounding_up(numerator1, numerator2, upper)?;
        // divRoundingUp
        Some(if (inner % lower).is_zero() { inner / lower } else { inner / lower + U256::one() })
    } else {
        Some(mul_div(numerator1, numerator2, upper)? / lower)
    }
}

/// `SqrtPriceMath.getAmount1Delta`.
pub fn get_amount1_delta(a: U256, b: U256, liquidity: u128, round_up: bool) -> Option<U256> {
    let (lower, upper) = if a > b { (b, a) } else { (a, b) };
    let diff = upper.checked_sub(lower)?;
    if round_up {
        mul_div_rounding_up(U256::from(liquidity), diff, q96())
    } else {
        mul_div(U256::from(liquidity), diff, q96())
    }
}

fn next_sqrt_price_from_amount0_rounding_up(
    sqrt_p: U256,
    liquidity: u128,
    amount: U256,
    add: bool,
) -> Option<U256> {
    if amount.is_zero() {
        return Some(sqrt_p);
    }
    let numerator1 = U256::from(liquidity) << 96;
    if add {
        let product = amount.checked_mul(sqrt_p);
        if let Some(product) = product {
            if product / amount == sqrt_p {
                let denominator = numerator1.checked_add(product)?;
                if denominator >= numerator1 {
                    return mul_div_rounding_up(numerator1, sqrt_p, denominator);
                }
            }
        }
        let denom = (numerator1 / sqrt_p).checked_add(amount)?;
        // divRoundingUp(numerator1, denom)
        Some(if (numerator1 % denom).is_zero() {
            numerator1 / denom
        } else {
            numerator1 / denom + U256::one()
        })
    } else {
        let product = amount.checked_mul(sqrt_p)?;
        if product / amount != sqrt_p || numerator1 <= product {
            return None;
        }
        let denominator = numerator1 - product;
        mul_div_rounding_up(numerator1, sqrt_p, denominator)
    }
}

fn next_sqrt_price_from_amount1_rounding_down(
    sqrt_p: U256,
    liquidity: u128,
    amount: U256,
    add: bool,
) -> Option<U256> {
    let l = U256::from(liquidity);
    if add {
        let quotient = if amount <= U256::MAX >> 160 {
            (amount << 96) / l
        } else {
            mul_div(amount, q96(), l)?
        };
        sqrt_p.checked_add(quotient)
    } else {
        let quotient = if amount <= U256::MAX >> 160 {
            // divRoundingUp((amount << 96), l)
            let n = amount << 96;
            if (n % l).is_zero() { n / l } else { n / l + U256::one() }
        } else {
            mul_div_rounding_up(amount, q96(), l)?
        };
        if sqrt_p <= quotient {
            return None;
        }
        Some(sqrt_p - quotient)
    }
}

/// `SqrtPriceMath.getNextSqrtPriceFromInput`.
pub fn get_next_sqrt_price_from_input(
    sqrt_p: U256,
    liquidity: u128,
    amount_in: U256,
    zero_for_one: bool,
) -> Option<U256> {
    if sqrt_p.is_zero() || liquidity == 0 {
        return None;
    }
    if zero_for_one {
        next_sqrt_price_from_amount0_rounding_up(sqrt_p, liquidity, amount_in, true)
    } else {
        next_sqrt_price_from_amount1_rounding_down(sqrt_p, liquidity, amount_in, true)
    }
}

/// `SqrtPriceMath.getNextSqrtPriceFromOutput`.
pub fn get_next_sqrt_price_from_output(
    sqrt_p: U256,
    liquidity: u128,
    amount_out: U256,
    zero_for_one: bool,
) -> Option<U256> {
    if sqrt_p.is_zero() || liquidity == 0 {
        return None;
    }
    if zero_for_one {
        next_sqrt_price_from_amount1_rounding_down(sqrt_p, liquidity, amount_out, false)
    } else {
        next_sqrt_price_from_amount0_rounding_up(sqrt_p, liquidity, amount_out, false)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SwapStep {
    pub sqrt_ratio_next_x96: U256,
    pub amount_in: U256,
    pub amount_out: U256,
    pub fee_amount: U256,
}

/// `SwapMath.computeSwapStep` — one step within a single liquidity range.
pub fn compute_swap_step(
    sqrt_ratio_current_x96: U256,
    sqrt_ratio_target_x96: U256,
    liquidity: u128,
    amount_remaining: i128,
    fee_pips: u32,
) -> Option<SwapStep> {
    let zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96;
    let exact_in = amount_remaining >= 0;
    let million = U256::from(1_000_000u64);
    let fee = U256::from(fee_pips);

    let mut sqrt_ratio_next_x96;
    let mut amount_in = U256::zero();
    let mut amount_out = U256::zero();

    if exact_in {
        let remaining = U256::from(amount_remaining as u128);
        let amount_remaining_less_fee = mul_div(remaining, million.checked_sub(fee)?, million)?;
        amount_in = if zero_for_one {
            get_amount0_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, true)?
        } else {
            get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, true)?
        };
        if amount_remaining_less_fee >= amount_in {
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96;
        } else {
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_input(
                sqrt_ratio_current_x96, liquidity, amount_remaining_less_fee, zero_for_one,
            )?;
        }
    } else {
        let remaining = U256::from(amount_remaining.unsigned_abs());
        amount_out = if zero_for_one {
            get_amount1_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, false)?
        } else {
            get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, false)?
        };
        if remaining >= amount_out {
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96;
        } else {
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_output(
                sqrt_ratio_current_x96, liquidity, remaining, zero_for_one,
            )?;
        }
    }

    let max = sqrt_ratio_target_x96 == sqrt_ratio_next_x96;

    if zero_for_one {
        if !(max && exact_in) {
            amount_in = get_amount0_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, true)?;
        }
        if !max || exact_in {
            amount_out = get_amount1_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, false)?;
        }
    } else {
        if !(max && exact_in) {
            amount_in = get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, true)?;
        }
        if !max || exact_in {
            amount_out = get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, false)?;
        }
    }

    // Cap the output at the requested amount for exact-output swaps.
    if !exact_in {
        let requested = U256::from(amount_remaining.unsigned_abs());
        if amount_out > requested {
            amount_out = requested;
        }
    }

    let fee_amount = if exact_in && sqrt_ratio_next_x96 != sqrt_ratio_target_x96 {
        // The whole remainder that was not consumed as input becomes fee.
        U256::from(amount_remaining as u128).checked_sub(amount_in)?
    } else {
        mul_div_rounding_up(amount_in, fee, million.checked_sub(fee)?)?
    };

    // `sqrt_ratio_next_x96` is assigned in every branch above; silence the unused-assignment
    // lint that fires on the declaration-site initialiser.
    let _ = &mut sqrt_ratio_next_x96;
    Some(SwapStep { sqrt_ratio_next_x96, amount_in, amount_out, fee_amount })
}

/// One initialized tick and the net liquidity delta crossing it in the increasing direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TickDelta {
    pub tick: i32,
    pub liquidity_net: i128,
}

/// The pool state needed to price a swap exactly.
#[derive(Debug, Clone)]
pub struct PoolSnapshot {
    pub sqrt_price_x96: U256,
    pub tick: i32,
    pub liquidity: u128,
    pub fee_pips: u32,
    pub tick_spacing: i32,
    /// Initialized ticks sorted ascending. Must span far enough either side of `tick` to
    /// cover the swap, or `exact_input_amount_out` reports `tick_data_exhausted`.
    pub ticks: Vec<TickDelta>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SwapError {
    /// The supplied tick window ran out before the swap finished. The quote would be a guess.
    TickDataExhausted,
    /// Price would leave the representable range.
    PriceLimitReached,
    Overflow,
    ZeroLiquidity,
}

#[derive(Debug, Clone)]
pub struct SwapResult {
    pub amount_in: U256,
    pub amount_out: U256,
    pub sqrt_price_x96_after: U256,
    pub tick_after: i32,
    pub liquidity_after: u128,
    pub initialized_ticks_crossed: u32,
}

/// Exact `exactInputSingle` pricing: walks the real tick ladder, crossing initialized ticks
/// and applying their liquidity deltas, exactly as `UniswapV3Pool.swap` does.
///
/// Returns `TickDataExhausted` rather than a wrong number when the swap would move beyond the
/// supplied tick window — a missing tick is a silent mispricing, so it must fail loudly.
pub fn exact_input_swap(
    pool: &PoolSnapshot,
    zero_for_one: bool,
    amount_in: U256,
) -> Result<SwapResult, SwapError> {
    if amount_in.is_zero() {
        return Ok(SwapResult {
            amount_in: U256::zero(),
            amount_out: U256::zero(),
            sqrt_price_x96_after: pool.sqrt_price_x96,
            tick_after: pool.tick,
            liquidity_after: pool.liquidity,
            initialized_ticks_crossed: 0,
        });
    }
    if amount_in > U256::from(i128::MAX as u128) {
        return Err(SwapError::Overflow);
    }

    let mut amount_specified_remaining = amount_in.as_u128() as i128;
    let mut amount_calculated = U256::zero();
    let mut sqrt_price_x96 = pool.sqrt_price_x96;
    let mut tick = pool.tick;
    let mut liquidity = pool.liquidity;
    let mut crossed = 0u32;

    let limit = if zero_for_one {
        min_sqrt_ratio() + U256::one()
    } else {
        max_sqrt_ratio() - U256::one()
    };

    while amount_specified_remaining > 0 && sqrt_price_x96 != limit {
        // Next initialized tick in the direction of travel.
        let next = if zero_for_one {
            pool.ticks.iter().rev().find(|t| t.tick <= tick).copied()
        } else {
            pool.ticks.iter().find(|t| t.tick > tick).copied()
        };
        let Some(next) = next else {
            return Err(SwapError::TickDataExhausted);
        };

        let sqrt_price_next = get_sqrt_ratio_at_tick(next.tick).ok_or(SwapError::Overflow)?;
        let sqrt_price_target = if zero_for_one {
            if sqrt_price_next < limit { limit } else { sqrt_price_next }
        } else if sqrt_price_next > limit {
            limit
        } else {
            sqrt_price_next
        };

        if liquidity == 0 {
            // No liquidity in this range: jump straight to the boundary and cross.
            sqrt_price_x96 = sqrt_price_target;
        } else {
            let step = compute_swap_step(
                sqrt_price_x96, sqrt_price_target, liquidity, amount_specified_remaining, pool.fee_pips,
            )
            .ok_or(SwapError::Overflow)?;

            let consumed = step
                .amount_in
                .checked_add(step.fee_amount)
                .ok_or(SwapError::Overflow)?;
            if consumed > U256::from(i128::MAX as u128) {
                return Err(SwapError::Overflow);
            }
            amount_specified_remaining -= consumed.as_u128() as i128;
            amount_calculated = amount_calculated
                .checked_add(step.amount_out)
                .ok_or(SwapError::Overflow)?;
            sqrt_price_x96 = step.sqrt_ratio_next_x96;
        }

        if sqrt_price_x96 == sqrt_price_next {
            // Crossing an initialized tick flips the sign of its liquidity delta when moving down.
            let delta = if zero_for_one { -next.liquidity_net } else { next.liquidity_net };
            liquidity = if delta < 0 {
                liquidity
                    .checked_sub(delta.unsigned_abs())
                    .ok_or(SwapError::ZeroLiquidity)?
            } else {
                liquidity
                    .checked_add(delta as u128)
                    .ok_or(SwapError::Overflow)?
            };
            crossed += 1;
            tick = if zero_for_one { next.tick - 1 } else { next.tick };
        } else {
            tick = get_tick_at_sqrt_ratio(sqrt_price_x96).ok_or(SwapError::PriceLimitReached)?;
        }
    }

    if amount_specified_remaining > 0 {
        // We hit the representable price bound without consuming the input.
        return Err(SwapError::PriceLimitReached);
    }

    Ok(SwapResult {
        amount_in,
        amount_out: amount_calculated,
        sqrt_price_x96_after: sqrt_price_x96,
        tick_after: tick,
        liquidity_after: liquidity,
        initialized_ticks_crossed: crossed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reference table shared with `python/tests/test_tick_math_reference.py`, which proves
    /// each value equals sqrt(1.0001^tick) * 2^96 at 140-digit precision. That test is the
    /// independent oracle; this one pins the Rust implementation to it.
    const REFERENCE: &str = include_str!("../tests/fixtures/tick_math_reference.txt");

    fn reference_rows() -> Vec<(i32, U256)> {
        REFERENCE
            .lines()
            .filter(|l| !l.trim().is_empty() && !l.starts_with('#'))
            .map(|l| {
                let mut parts = l.split_whitespace();
                let tick: i32 = parts.next().unwrap().parse().unwrap();
                let value = U256::from_dec_str(parts.next().unwrap()).unwrap();
                (tick, value)
            })
            .collect()
    }

    #[test]
    fn sqrt_ratio_matches_verified_reference_table() {
        let rows = reference_rows();
        assert!(rows.len() >= 20, "reference table looks truncated");
        for (tick, expected) in rows {
            assert_eq!(get_sqrt_ratio_at_tick(tick).unwrap(), expected, "tick {tick}");
        }
    }

    #[test]
    fn sqrt_ratio_anchors_match_uniswap_constants() {
        assert_eq!(get_sqrt_ratio_at_tick(0).unwrap(), q96());
        assert_eq!(get_sqrt_ratio_at_tick(MIN_TICK).unwrap(), min_sqrt_ratio());
        assert_eq!(get_sqrt_ratio_at_tick(MAX_TICK).unwrap(), max_sqrt_ratio());
        assert!(get_sqrt_ratio_at_tick(MIN_TICK - 1).is_none());
        assert!(get_sqrt_ratio_at_tick(MAX_TICK + 1).is_none());
    }

    #[test]
    fn sqrt_ratio_is_strictly_monotonic() {
        for tick in [-500000i32, -60000, -5000, -60, -1, 0, 1, 60, 5000, 60000, 500000] {
            let a = get_sqrt_ratio_at_tick(tick).unwrap();
            let b = get_sqrt_ratio_at_tick(tick + 1).unwrap();
            assert!(b > a, "not monotonic at tick {tick}");
        }
    }

    #[test]
    fn tick_at_sqrt_ratio_inverts_sqrt_ratio_at_tick() {
        for tick in [-887271i32, -500000, -60000, -5000, -60, -1, 0, 1, 60, 5000, 60000, 500000, 887271] {
            let sqrt = get_sqrt_ratio_at_tick(tick).unwrap();
            assert_eq!(get_tick_at_sqrt_ratio(sqrt).unwrap(), tick, "round trip at {tick}");
        }
    }

    #[test]
    fn mul_div_uses_full_512_bit_intermediate() {
        // (2^255 * 2) / 2^128 would overflow a 256-bit intermediate product.
        let a = U256::one() << 255;
        let got = mul_div(a, U256::from(2u8), U256::one() << 128).unwrap();
        assert_eq!(got, U256::one() << 128);
        assert!(mul_div(a, a, U256::one()).is_none(), "must reject >256-bit results");
        assert!(mul_div(a, a, U256::zero()).is_none(), "must reject zero denominator");
    }

    #[test]
    fn mul_div_rounding_up_only_rounds_on_a_remainder() {
        let seven = U256::from(7u8);
        let two = U256::from(2u8);
        assert_eq!(mul_div(seven, U256::one(), two).unwrap(), U256::from(3u8));
        assert_eq!(mul_div_rounding_up(seven, U256::one(), two).unwrap(), U256::from(4u8));
        // Exact division must not round up.
        let eight = U256::from(8u8);
        assert_eq!(mul_div_rounding_up(eight, U256::one(), two).unwrap(), U256::from(4u8));
    }

    #[test]
    fn amount_deltas_round_in_the_protocol_favouring_direction() {
        let lower = get_sqrt_ratio_at_tick(-60).unwrap();
        let upper = get_sqrt_ratio_at_tick(60).unwrap();
        let l = 10_000_000_000_000u128;
        let a0_up = get_amount0_delta(lower, upper, l, true).unwrap();
        let a0_down = get_amount0_delta(lower, upper, l, false).unwrap();
        let a1_up = get_amount1_delta(lower, upper, l, true).unwrap();
        let a1_down = get_amount1_delta(lower, upper, l, false).unwrap();
        assert!(a0_up >= a0_down && a0_up - a0_down <= U256::one());
        assert!(a1_up >= a1_down && a1_up - a1_down <= U256::one());
        // Deltas are symmetric in argument order.
        assert_eq!(get_amount0_delta(upper, lower, l, true).unwrap(), a0_up);
    }

    fn snapshot() -> PoolSnapshot {
        // A symmetric book: one liquidity range spanning [-600, 600] around the 1:1 price.
        PoolSnapshot {
            sqrt_price_x96: get_sqrt_ratio_at_tick(0).unwrap(),
            tick: 0,
            liquidity: 50_000_000_000_000_000u128,
            fee_pips: 3000,
            tick_spacing: 60,
            ticks: vec![
                TickDelta { tick: -600, liquidity_net: 50_000_000_000_000_000i128 },
                TickDelta { tick: 600, liquidity_net: -50_000_000_000_000_000i128 },
            ],
        }
    }

    #[test]
    fn exact_input_swap_consumes_all_input_and_moves_price_down_for_zero_for_one() {
        let pool = snapshot();
        let amount_in = U256::from(1_000_000_000_000u64);
        let out = exact_input_swap(&pool, true, amount_in).unwrap();
        assert_eq!(out.amount_in, amount_in);
        assert!(!out.amount_out.is_zero());
        assert!(out.sqrt_price_x96_after < pool.sqrt_price_x96, "0->1 must lower the price");
        assert!(out.amount_out < amount_in, "fee + slippage must make output < input at 1:1");
    }

    #[test]
    fn exact_input_swap_is_directionally_symmetric() {
        let pool = snapshot();
        let amount_in = U256::from(1_000_000_000_000u64);
        let down = exact_input_swap(&pool, true, amount_in).unwrap();
        let up = exact_input_swap(&pool, false, amount_in).unwrap();
        assert!(up.sqrt_price_x96_after > pool.sqrt_price_x96);
        // A symmetric book at the 1:1 price gives near-identical output either way.
        let (a, b) = (down.amount_out, up.amount_out);
        let (hi, lo) = if a > b { (a, b) } else { (b, a) };
        assert!((hi - lo) * U256::from(1000u32) < hi, "asymmetry above 0.1%");
    }

    #[test]
    fn output_grows_monotonically_but_sublinearly_with_input() {
        let pool = snapshot();
        let small = exact_input_swap(&pool, true, U256::from(1_000_000_000u64)).unwrap();
        let large = exact_input_swap(&pool, true, U256::from(10_000_000_000u64)).unwrap();
        assert!(large.amount_out > small.amount_out, "more input must give more output");
        // Price impact: 10x the input must give strictly less than 10x the output.
        assert!(large.amount_out < small.amount_out * U256::from(10u32), "no price impact applied");
    }

    #[test]
    fn higher_fee_tier_returns_strictly_less_output() {
        let mut cheap = snapshot();
        cheap.fee_pips = 500;
        let mut dear = snapshot();
        dear.fee_pips = 10_000;
        let amount = U256::from(1_000_000_000_000u64);
        let a = exact_input_swap(&cheap, true, amount).unwrap();
        let b = exact_input_swap(&dear, true, amount).unwrap();
        assert!(a.amount_out > b.amount_out, "fee tier must change the quote");
    }

    #[test]
    fn swap_reports_tick_data_exhausted_instead_of_guessing() {
        // A book with no ticks below the current price: a 0->1 swap cannot be priced.
        let mut pool = snapshot();
        pool.ticks = vec![TickDelta { tick: 600, liquidity_net: -50_000_000_000_000_000i128 }];
        let err = exact_input_swap(&pool, true, U256::from(1_000_000_000_000u64)).unwrap_err();
        assert_eq!(err, SwapError::TickDataExhausted);
    }

    #[test]
    fn crossing_an_initialized_tick_applies_its_liquidity_delta() {
        // Two overlapping positions:
        //   A: [-600, 600] with L=50e15  ->  -600:+50e15, 600:-50e15
        //   B: [-600,  -60] with L=25e15 ->  -600:+25e15, -60:-25e15
        // `liquidity_net` is the delta applied when crossing UPWARD, so crossing -60
        // downward applies -(-25e15) = +25e15 and deepens the book.
        let mut pool = snapshot();
        pool.ticks = vec![
            TickDelta { tick: -600, liquidity_net: 75_000_000_000_000_000i128 },
            TickDelta { tick: -60, liquidity_net: -25_000_000_000_000_000i128 },
            TickDelta { tick: 600, liquidity_net: -50_000_000_000_000_000i128 },
        ];
        let out = exact_input_swap(&pool, true, U256::from(500_000_000_000_000u64)).unwrap();
        assert!(out.initialized_ticks_crossed >= 1, "expected to cross tick -60");
        assert_eq!(out.liquidity_after, 75_000_000_000_000_000u128);
        assert!(out.tick_after < -60);
    }

    #[test]
    fn crossing_upward_applies_liquidity_net_with_its_stated_sign() {
        // Mirror of the above: B' spans [60, 600], so crossing 60 upward adds its liquidity.
        let mut pool = snapshot();
        pool.ticks = vec![
            TickDelta { tick: -600, liquidity_net: 50_000_000_000_000_000i128 },
            TickDelta { tick: 60, liquidity_net: 25_000_000_000_000_000i128 },
            TickDelta { tick: 600, liquidity_net: -75_000_000_000_000_000i128 },
        ];
        let out = exact_input_swap(&pool, false, U256::from(500_000_000_000_000u64)).unwrap();
        assert!(out.initialized_ticks_crossed >= 1, "expected to cross tick 60");
        assert_eq!(out.liquidity_after, 75_000_000_000_000_000u128);
        assert!(out.tick_after >= 60);
    }

    #[test]
    fn zero_input_is_a_no_op() {
        let pool = snapshot();
        let out = exact_input_swap(&pool, true, U256::zero()).unwrap();
        assert!(out.amount_out.is_zero());
        assert_eq!(out.sqrt_price_x96_after, pool.sqrt_price_x96);
        assert_eq!(out.initialized_ticks_crossed, 0);
    }
}
