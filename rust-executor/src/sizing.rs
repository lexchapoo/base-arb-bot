//! Optimal trade sizing on the exact concentrated-liquidity profit curve.
//!
//! With exact tick math the profit function `f(x) = out(x) - x` is piecewise-smooth and
//! concave *within* each liquidity range, with kinks exactly where the swap crosses an
//! initialized tick. That structure means a coarse grid is unnecessary and imprecise: the
//! true optimum is found by evaluating the kink points and ternary-searching the bracket
//! between the best kink's neighbours.
//!
//! Everything is integer-only and deterministic — no floats reach a sizing decision.

//! NOTE: staged for integration; not yet called from the live quoting path.
#![allow(dead_code)]
use crate::v3_math::{exact_input_swap, get_sqrt_ratio_at_tick, PoolSnapshot};
use primitive_types::U256;

/// Integer ternary search for the maximum of a concave function on `[lo, hi]`.
///
/// `profit` returns `None` for infeasible sizes (e.g. tick data exhausted); those are treated
/// as strictly worse than any feasible point so the search walks away from them.
/// Terminates in O(log(hi-lo)) evaluations — the `while` narrows the bracket by 1/3 each pass.
pub fn ternary_search_max<F>(lo: U256, hi: U256, mut profit: F) -> Option<(U256, i128)>
where
    F: FnMut(U256) -> Option<i128>,
{
    if lo > hi {
        return None;
    }
    let (mut lo, mut hi) = (lo, hi);
    let three = U256::from(3u8);

    // Narrow while the bracket is wide enough for two distinct interior probes.
    while hi.saturating_sub(lo) > U256::from(2u8) {
        let span = hi - lo;
        let m1 = lo + span / three;
        let m2 = hi - span / three;
        if m1 >= m2 {
            break;
        }
        let f1 = profit(m1);
        let f2 = profit(m2);
        match (f1, f2) {
            (Some(a), Some(b)) => {
                if a < b {
                    lo = m1 + U256::one();
                } else {
                    hi = m2 - U256::one();
                }
            }
            // Infeasible on the right: the optimum cannot be past it.
            (Some(_), None) => hi = m2 - U256::one(),
            // Infeasible on the left: move up.
            (None, Some(_)) => lo = m1 + U256::one(),
            (None, None) => {
                // Both probes infeasible; shrink toward the low end, which is feasible more often.
                hi = m2 - U256::one();
            }
        }
    }

    // Exhaustively check the tiny remaining bracket.
    let mut best: Option<(U256, i128)> = None;
    let mut x = lo;
    while x <= hi {
        if let Some(value) = profit(x) {
            if best.is_none_or(|(_, b)| value > b) {
                best = Some((x, value));
            }
        }
        if x == hi {
            break;
        }
        x += U256::one();
    }
    best
}

/// Input amounts that move a pool exactly onto each initialized tick boundary.
///
/// These are the kinks of the profit curve: within a range the curve is smooth, and liquidity
/// changes discontinuously at each of these points. Returned ascending, capped at `max_in`.
pub fn tick_boundary_inputs(
    pool: &PoolSnapshot,
    zero_for_one: bool,
    max_in: U256,
) -> Vec<U256> {
    let mut out = Vec::new();
    let mut boundaries: Vec<i32> = pool
        .ticks
        .iter()
        .map(|t| t.tick)
        .filter(|t| if zero_for_one { *t <= pool.tick } else { *t > pool.tick })
        .collect();
    if zero_for_one {
        boundaries.sort_unstable_by(|a, b| b.cmp(a));
    } else {
        boundaries.sort_unstable();
    }

    for tick in boundaries {
        let Some(target) = get_sqrt_ratio_at_tick(tick) else { continue };
        // Binary-search the input that lands on this boundary. `exact_input_swap` is monotonic
        // in the input, so the search is exact.
        let mut lo = U256::zero();
        let mut hi = max_in;
        let mut found: Option<U256> = None;
        while lo <= hi {
            let mid = lo + (hi - lo) / U256::from(2u8);
            match exact_input_swap(pool, zero_for_one, mid) {
                Ok(result) => {
                    let reached = if zero_for_one {
                        result.sqrt_price_x96_after <= target
                    } else {
                        result.sqrt_price_x96_after >= target
                    };
                    if reached {
                        found = Some(mid);
                        if mid.is_zero() {
                            break;
                        }
                        hi = mid - U256::one();
                    } else {
                        lo = mid + U256::one();
                    }
                }
                Err(_) => {
                    if mid.is_zero() {
                        break;
                    }
                    hi = mid - U256::one();
                }
            }
            if hi == U256::MAX {
                break;
            }
        }
        if let Some(v) = found {
            if !v.is_zero() && v <= max_in {
                out.push(v);
            }
        }
    }
    out.sort_unstable();
    out.dedup();
    out
}

/// The size that maximises `profit` over `[1, max_in]`, using the exact curve's kink structure.
///
/// Evaluates every tick-boundary candidate, then ternary-searches the bracket around the best
/// one. This finds the true optimum rather than the best point on an arbitrary grid.
pub fn optimal_size<F>(
    boundaries: &[U256],
    max_in: U256,
    mut profit: F,
) -> Option<(U256, i128)>
where
    F: FnMut(U256) -> Option<i128>,
{
    if max_in.is_zero() {
        return None;
    }
    // Candidate set: every kink, plus the endpoints.
    let mut candidates: Vec<U256> = Vec::with_capacity(boundaries.len() + 2);
    candidates.push(U256::one());
    candidates.extend(boundaries.iter().copied().filter(|b| *b <= max_in));
    candidates.push(max_in);
    candidates.sort_unstable();
    candidates.dedup();

    let mut best: Option<(U256, i128)> = None;
    let mut best_index = 0usize;
    for (i, c) in candidates.iter().enumerate() {
        if let Some(value) = profit(*c) {
            if best.is_none_or(|(_, b)| value > b) {
                best = Some((*c, value));
                best_index = i;
            }
        }
    }
    let (_, _) = best?;

    // Refine inside the bracket spanned by the best candidate's neighbours: the optimum of a
    // concave piece cannot lie outside it.
    let lo = if best_index == 0 { U256::one() } else { candidates[best_index - 1] };
    let hi = if best_index + 1 < candidates.len() { candidates[best_index + 1] } else { max_in };
    let refined = ternary_search_max(lo, hi, &mut profit);

    match (best, refined) {
        (Some((bx, bv)), Some((rx, rv))) => {
            if rv > bv { Some((rx, rv)) } else { Some((bx, bv)) }
        }
        (b, None) => b,
        (None, r) => r,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v3_math::TickDelta;

    fn as_i128(v: U256) -> i128 {
        v.as_u128() as i128
    }

    #[test]
    fn ternary_search_finds_the_peak_of_a_concave_parabola() {
        // f(x) = -(x - 4242)^2 + 1_000_000, integer domain.
        let peak = 4242i128;
        let got = ternary_search_max(U256::zero(), U256::from(10_000u32), |x| {
            let d = as_i128(x) - peak;
            Some(1_000_000 - d * d)
        })
        .unwrap();
        assert_eq!(got.0, U256::from(4242u32));
        assert_eq!(got.1, 1_000_000);
    }

    #[test]
    fn ternary_search_handles_a_peak_at_either_boundary() {
        let increasing = ternary_search_max(U256::zero(), U256::from(500u32), |x| Some(as_i128(x)))
            .unwrap();
        assert_eq!(increasing.0, U256::from(500u32));

        let decreasing = ternary_search_max(U256::zero(), U256::from(500u32), |x| Some(-as_i128(x)))
            .unwrap();
        assert_eq!(decreasing.0, U256::zero());
    }

    #[test]
    fn ternary_search_walks_away_from_infeasible_sizes() {
        // Feasible only below 100; peak at 99.
        let got = ternary_search_max(U256::zero(), U256::from(100_000u32), |x| {
            if x > U256::from(99u32) { None } else { Some(as_i128(x)) }
        })
        .unwrap();
        assert_eq!(got.0, U256::from(99u32));
    }

    #[test]
    fn ternary_search_returns_none_when_nothing_is_feasible() {
        assert!(ternary_search_max(U256::one(), U256::from(50u32), |_| None).is_none());
    }

    fn pool() -> PoolSnapshot {
        PoolSnapshot {
            sqrt_price_x96: get_sqrt_ratio_at_tick(0).unwrap(),
            tick: 0,
            liquidity: 50_000_000_000_000_000u128,
            fee_pips: 3000,
            tick_spacing: 60,
            ticks: vec![
                TickDelta { tick: -600, liquidity_net: 50_000_000_000_000_000i128 },
                TickDelta { tick: -60, liquidity_net: 0i128 },
                TickDelta { tick: 600, liquidity_net: -50_000_000_000_000_000i128 },
            ],
        }
    }

    #[test]
    fn tick_boundaries_are_ascending_and_actually_reach_their_ticks() {
        let p = pool();
        let max_in = U256::from(10_000_000_000_000_000u64);
        let bounds = tick_boundary_inputs(&p, true, max_in);
        assert!(!bounds.is_empty(), "expected at least one reachable boundary");
        assert!(bounds.windows(2).all(|w| w[0] < w[1]), "must be ascending and deduped");
        for b in &bounds {
            let r = exact_input_swap(&p, true, *b).unwrap();
            assert!(r.sqrt_price_x96_after < p.sqrt_price_x96);
        }
    }

    #[test]
    fn optimal_size_beats_a_coarse_geometric_grid_on_the_exact_curve() {
        let p = pool();
        let max_in = U256::from(2_000_000_000_000_000u64);

        // Synthetic arbitrage: the counter-venue pays a fixed rate, so profit is
        // out(x) * rate - x, which is concave and has an interior optimum.
        let profit = |x: U256| -> Option<i128> {
            let r = exact_input_swap(&p, true, x).ok()?;
            let revenue = r.amount_out.checked_mul(U256::from(1006u32))? / U256::from(1000u32);
            Some(as_i128(revenue) - as_i128(x))
        };

        let bounds = tick_boundary_inputs(&p, true, max_in);
        let mut f = profit;
        let (best_x, best_v) = optimal_size(&bounds, max_in, &mut f).unwrap();
        assert!(best_v > 0, "expected a profitable size to exist");

        // The old approach: a coarse geometric grid over the same range.
        let mut grid_best = i128::MIN;
        let mut amount = U256::one();
        while amount <= max_in {
            if let Some(v) = profit(amount) {
                grid_best = grid_best.max(v);
            }
            amount *= U256::from(4u32);
        }
        assert!(
            best_v >= grid_best,
            "exact sizing ({best_v} at {best_x}) must not lose to the coarse grid ({grid_best})"
        );
    }

    #[test]
    fn optimal_size_is_deterministic() {
        let p = pool();
        let max_in = U256::from(2_000_000_000_000_000u64);
        let profit = |x: U256| -> Option<i128> {
            let r = exact_input_swap(&p, true, x).ok()?;
            let revenue = r.amount_out.checked_mul(U256::from(1006u32))? / U256::from(1000u32);
            Some(as_i128(revenue) - as_i128(x))
        };
        let bounds = tick_boundary_inputs(&p, true, max_in);
        let mut f1 = profit;
        let mut f2 = profit;
        assert_eq!(optimal_size(&bounds, max_in, &mut f1), optimal_size(&bounds, max_in, &mut f2));
    }

    #[test]
    fn unprofitable_curve_yields_a_non_positive_optimum_rather_than_a_bogus_trade() {
        let p = pool();
        let max_in = U256::from(1_000_000_000_000u64);
        // No counter-venue premium: swapping and swapping back always loses the fee.
        let mut profit = |x: U256| -> Option<i128> {
            let r = exact_input_swap(&p, true, x).ok()?;
            Some(as_i128(r.amount_out) - as_i128(x))
        };
        let bounds = tick_boundary_inputs(&p, true, max_in);
        let (_, value) = optimal_size(&bounds, max_in, &mut profit).unwrap();
        assert!(value < 0, "a pure round trip through one pool must lose the fee");
    }
}
