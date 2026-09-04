//! Loads real concentrated-liquidity pool state over JSON-RPC into a [`PoolSnapshot`].
//!
//! Uniswap V3 and Aerodrome Slipstream expose the same storage getters, so one loader serves
//! both. The tick ladder is reconstructed from `tickBitmap` words (which ticks are
//! initialized) plus `ticks(tick)` (each tick's `liquidityNet`), exactly the data the pool
//! itself consults during a swap.

//! NOTE: staged for integration; not yet called from the live quoting path.
#![allow(dead_code)]
use crate::v3_math::{PoolSnapshot, TickDelta};
use primitive_types::U256;
use serde_json::Value;

const SEL_SLOT0: &str = "3850c7bd";
const SEL_LIQUIDITY: &str = "1a686502";
const SEL_FEE: &str = "ddca3f43";
const SEL_TICK_SPACING: &str = "d0c93a7c";
const SEL_TICK_BITMAP: &str = "5339c296";
const SEL_TICKS: &str = "f30dba93";

/// ABI-encode a signed integer into a 32-byte two's-complement word.
fn encode_int(value: i64) -> String {
    if value >= 0 {
        format!("{:064x}", value as u128)
    } else {
        // Two's complement over 256 bits.
        let magnitude = U256::from(value.unsigned_abs());
        let encoded = U256::MAX - magnitude + U256::one();
        format!("{encoded:064x}")
    }
}

/// Decode a 32-byte word at `index` as an unsigned integer.
fn word(data: &str, index: usize) -> Option<U256> {
    let hex = data.trim_start_matches("0x");
    let start = index * 64;
    let slice = hex.get(start..start + 64)?;
    U256::from_str_radix(slice, 16).ok()
}

/// Decode a 32-byte word as a signed integer of `bits` width (int24, int128, ...).
fn word_signed(data: &str, index: usize, bits: u32) -> Option<i128> {
    let raw = word(data, index)?;
    let sign_bit = U256::one() << (bits - 1);
    let modulus = U256::one() << bits;
    // The word is sign-extended to 256 bits; mask back down to `bits` first.
    let masked = raw % modulus;
    if masked >= sign_bit {
        let negative = modulus - masked;
        Some(-(negative.as_u128() as i128))
    } else {
        Some(masked.as_u128() as i128)
    }
}

/// Position of a tick inside the pool's bitmap: (word index, bit index).
fn tick_position(compressed: i32) -> (i16, u8) {
    ((compressed >> 8) as i16, (compressed & 0xff) as u8)
}

pub struct RpcCaller<'a> {
    pub url: &'a str,
    pub block_tag: &'a str,
}

/// Shared RPC endpoints throttle aggressively, and loading a tick ladder is many sequential
/// `eth_call`s. Retry rate-limit responses with exponential backoff rather than failing a
/// quote outright; a genuine revert is returned immediately.
fn is_rate_limited(error: &str) -> bool {
    let lower = error.to_ascii_lowercase();
    lower.contains("rate limit")
        || lower.contains("too many requests")
        || lower.contains("-32016")
        || lower.contains("429")
}

impl RpcCaller<'_> {
    async fn call(&self, to: &str, data: String) -> Result<String, String> {
        let params =
            serde_json::json!([{"to": to, "data": format!("0x{data}")}, self.block_tag]);
        let mut backoff_ms = 200u64;
        let mut last_error = String::new();
        for attempt in 0..6 {
            match crate::rpc_call(self.url, "eth_call", params.clone()).await {
                Ok(body) => match crate::rpc_result_value(&body) {
                    Ok(value) => {
                        return value
                            .as_str()
                            .map(str::to_string)
                            .ok_or_else(|| "eth_call result is not a hex string".to_string());
                    }
                    Err(e) => {
                        if !is_rate_limited(&e) {
                            return Err(e);
                        }
                        last_error = e;
                    }
                },
                Err(e) => {
                    if !is_rate_limited(&e) {
                        return Err(e);
                    }
                    last_error = e;
                }
            }
            if attempt < 5 {
                tokio::time::sleep(std::time::Duration::from_millis(backoff_ms)).await;
                backoff_ms = (backoff_ms * 2).min(3_000);
            }
        }
        Err(format!("rate limited after retries: {last_error}"))
    }
}

/// How many bitmap words to scan either side of the current tick.
/// Each word covers 256 ticks * tickSpacing of price range.
pub const DEFAULT_BITMAP_WORD_RADIUS: i16 = 8;

/// Read a pool's live state and enough of its tick ladder to price a swap exactly.
pub async fn load_pool_snapshot(
    rpc: &RpcCaller<'_>,
    pool: &str,
    word_radius: i16,
) -> Result<PoolSnapshot, String> {
    let slot0 = rpc.call(pool, SEL_SLOT0.into()).await?;
    let sqrt_price_x96 = word(&slot0, 0).ok_or("slot0: missing sqrtPriceX96")?;
    let tick = word_signed(&slot0, 1, 24).ok_or("slot0: missing tick")? as i32;

    let liquidity_raw = rpc.call(pool, SEL_LIQUIDITY.into()).await?;
    let liquidity = word(&liquidity_raw, 0).ok_or("liquidity: empty")?.as_u128();

    let spacing_raw = rpc.call(pool, SEL_TICK_SPACING.into()).await?;
    let tick_spacing = word_signed(&spacing_raw, 0, 24).ok_or("tickSpacing: empty")? as i32;
    if tick_spacing <= 0 {
        return Err(format!("invalid tickSpacing {tick_spacing}"));
    }

    let fee_raw = rpc.call(pool, SEL_FEE.into()).await?;
    let fee_pips = word(&fee_raw, 0).ok_or("fee: empty")?.as_u32();

    // Which ticks are initialized, from the bitmap.
    let compressed = if tick < 0 && tick % tick_spacing != 0 {
        tick / tick_spacing - 1
    } else {
        tick / tick_spacing
    };
    let (centre_word, _) = tick_position(compressed);

    let mut initialized: Vec<i32> = Vec::new();
    for offset in -word_radius..=word_radius {
        let word_pos = centre_word.saturating_add(offset);
        let data = format!("{SEL_TICK_BITMAP}{}", encode_int(word_pos as i64));
        let raw = rpc.call(pool, data).await?;
        let bitmap = word(&raw, 0).ok_or("tickBitmap: empty")?;
        if bitmap.is_zero() {
            continue;
        }
        for bit in 0..256u32 {
            if bitmap.bit(bit as usize) {
                let compressed_tick = (word_pos as i32) * 256 + bit as i32;
                initialized.push(compressed_tick * tick_spacing);
            }
        }
    }
    initialized.sort_unstable();
    initialized.dedup();

    // liquidityNet for each initialized tick.
    let mut ticks = Vec::with_capacity(initialized.len());
    for t in initialized {
        let data = format!("{SEL_TICKS}{}", encode_int(t as i64));
        let raw = rpc.call(pool, data).await?;
        // ticks() returns (liquidityGross, liquidityNet, ...): liquidityNet is word 1.
        let liquidity_net = word_signed(&raw, 1, 128).ok_or("ticks: missing liquidityNet")?;
        ticks.push(TickDelta { tick: t, liquidity_net });
    }

    Ok(PoolSnapshot { sqrt_price_x96, tick, liquidity, fee_pips, tick_spacing, ticks })
}

/// QuoterV2 `quoteExactInputSingle` — the on-chain oracle the local math is checked against.
pub async fn quoter_v2_exact_input_single(
    rpc: &RpcCaller<'_>,
    quoter: &str,
    token_in: &str,
    token_out: &str,
    amount_in: U256,
    fee_pips: u32,
) -> Result<U256, String> {
    let mut data = String::from("c6a5026a");
    data.push_str(&format!("{:0>64}", token_in.trim_start_matches("0x")));
    data.push_str(&format!("{:0>64}", token_out.trim_start_matches("0x")));
    data.push_str(&format!("{amount_in:064x}"));
    data.push_str(&format!("{fee_pips:064x}"));
    data.push_str(&"0".repeat(64)); // sqrtPriceLimitX96 = 0 (no limit)
    let raw = rpc.call(quoter, data).await?;
    word(&raw, 0).ok_or_else(|| "quoter returned no amountOut".to_string())
}

/// Convenience wrapper used by the differential harness.
pub fn decode_hex_word(data: &str, index: usize) -> Option<U256> {
    word(data, index)
}

pub fn decode_hex_word_signed(data: &str, index: usize, bits: u32) -> Option<i128> {
    word_signed(data, index, bits)
}

pub fn abi_encode_int(value: i64) -> String {
    encode_int(value)
}

#[allow(dead_code)]
fn unused(_: &Value) {}

#[cfg(test)]
mod tests {
    use super::*;

    /// Differential test: exact local math vs the on-chain QuoterV2, over real pool state.
    ///
    /// This is the only oracle that validates the *whole* pipeline -- bitmap decoding, tick
    /// loading, liquidity-net signs, and the swap loop -- rather than TickMath alone.
    /// Requires network, so it is ignored by default:
    ///
    ///   BASE_HTTP_RPC=... V3_DIFF_POOL=0x... V3_DIFF_TOKEN_IN=0x... V3_DIFF_TOKEN_OUT=0x... \
    ///   UNISWAP_V3_QUOTER_V2=0x... cargo test differential_matches_quoter_v2 -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn differential_matches_quoter_v2() {
        let rpc_url = std::env::var("BASE_HTTP_RPC").expect("BASE_HTTP_RPC");
        let pool = std::env::var("V3_DIFF_POOL").expect("V3_DIFF_POOL");
        let token_in = std::env::var("V3_DIFF_TOKEN_IN").expect("V3_DIFF_TOKEN_IN");
        let token_out = std::env::var("V3_DIFF_TOKEN_OUT").expect("V3_DIFF_TOKEN_OUT");
        let quoter = std::env::var("UNISWAP_V3_QUOTER_V2").expect("UNISWAP_V3_QUOTER_V2");

        // Pin both sides to the SAME block. Against `pending` the pool state and the quoter
        // could observe different views and any mismatch would be meaningless.
        let head = crate::rpc_call(&rpc_url, "eth_blockNumber", serde_json::json!([]))
            .await
            .expect("eth_blockNumber");
        let block = crate::rpc_hex_u128(&head).expect("block number");
        let tag = format!("0x{block:x}");
        let rpc = RpcCaller { url: &rpc_url, block_tag: &tag };

        let radius: i16 = std::env::var("V3_DIFF_WORD_RADIUS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_BITMAP_WORD_RADIUS);
        let snapshot = load_pool_snapshot(&rpc, &pool, radius)
            .await
            .expect("load pool snapshot");
        println!(
            "block={block} tick={} liquidity={} fee={} spacing={} initialized_ticks={}",
            snapshot.tick, snapshot.liquidity, snapshot.fee_pips, snapshot.tick_spacing,
            snapshot.ticks.len()
        );
        assert!(!snapshot.ticks.is_empty(), "no initialized ticks loaded");

        // token0 decides swap direction.
        let token0_raw = rpc.call(&pool, "0dfe1681".into()).await.expect("token0");
        let token0 = format!("0x{}", &token0_raw.trim_start_matches("0x")[24..]);
        let zero_for_one = token_in.to_lowercase() == token0.to_lowercase();

        let mut checked = 0;
        for exponent in [12u32, 14, 16, 18] {
            let amount_in = U256::from(10u64).pow(U256::from(exponent));
            let expected = match quoter_v2_exact_input_single(
                &rpc, &quoter, &token_in, &token_out, amount_in, snapshot.fee_pips,
            )
            .await
            {
                Ok(v) => v,
                Err(e) => {
                    println!("size 1e{exponent}: quoter reverted ({e}); skipping");
                    continue;
                }
            };
            let local = match crate::v3_math::exact_input_swap(&snapshot, zero_for_one, amount_in) {
                Ok(v) => v,
                Err(e) => panic!("size 1e{exponent}: local math failed: {e:?}"),
            };
            println!(
                "size 1e{exponent}: quoter={expected} local={} ticks_crossed={}",
                local.amount_out, local.initialized_ticks_crossed
            );
            assert_eq!(
                local.amount_out, expected,
                "size 1e{exponent}: local math must match QuoterV2 exactly"
            );
            checked += 1;
        }
        assert!(checked > 0, "quoter reverted at every size; nothing was verified");
    }

    #[test]
    fn encodes_negative_ints_as_twos_complement() {
        assert_eq!(encode_int(0), "0".repeat(64));
        assert_eq!(
            encode_int(1),
            "0000000000000000000000000000000000000000000000000000000000000001"
        );
        assert_eq!(
            encode_int(-1),
            "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        );
        assert_eq!(
            encode_int(-887272),
            "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff27618"
        );
    }

    #[test]
    fn decodes_signed_words_at_their_declared_width() {
        // int24 tick = -60, sign-extended to 256 bits by the ABI.
        let w = format!("0x{}", "f".repeat(62) + "c4");
        assert_eq!(word_signed(&w, 0, 24).unwrap(), -60);

        // int128 liquidityNet = -25e15.
        let neg = U256::MAX - U256::from(25_000_000_000_000_000u128) + U256::one();
        let w2 = format!("0x{neg:064x}");
        assert_eq!(word_signed(&w2, 0, 128).unwrap(), -25_000_000_000_000_000i128);

        // Positive values are unaffected.
        let w3 = format!("0x{:064x}", 12345u64);
        assert_eq!(word_signed(&w3, 0, 24).unwrap(), 12345);
    }

    #[test]
    fn tick_position_splits_into_word_and_bit() {
        assert_eq!(tick_position(0), (0, 0));
        assert_eq!(tick_position(255), (0, 255));
        assert_eq!(tick_position(256), (1, 0));
        assert_eq!(tick_position(-1), (-1, 255));
        assert_eq!(tick_position(-256), (-1, 0));
        assert_eq!(tick_position(-257), (-2, 255));
    }

    #[test]
    fn word_indexing_reads_successive_abi_slots() {
        let data = format!("0x{:064x}{:064x}", 7u8, 9u8);
        assert_eq!(word(&data, 0).unwrap(), U256::from(7u8));
        assert_eq!(word(&data, 1).unwrap(), U256::from(9u8));
        assert!(word(&data, 2).is_none());
    }
}
