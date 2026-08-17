use serde::{Deserialize, Serialize};

pub const PACKED_VERSION: u8 = 1;
pub const MAX_PACKED_CANDIDATES: usize = 8;
pub const MAX_PACKED_LEGS: usize = 4;
pub const MAX_PACKED_LEG_DATA: usize = 2048;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PackedLeg {
    pub adapter: String,
    pub token_in: String,
    pub token_out: String,
    pub min_out: String,
    #[serde(default = "default_hex")]
    pub data: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PackedCandidate {
    pub candidate_id: String,
    pub min_profit: String,
    pub legs: Vec<PackedLeg>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PackedBatchRequest {
    pub asset: String,
    pub amount: String,
    pub target_block: u64,
    pub deadline: u64,
    pub candidates: Vec<PackedCandidate>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PackedBatchResponse {
    pub packed: String,
    pub bytes_len: usize,
    pub candidate_count: usize,
}

fn default_hex() -> String { "0x".into() }

fn parse_fixed_hex(value: &str, bytes: usize, label: &str) -> Result<Vec<u8>, String> {
    let raw = value.strip_prefix("0x").ok_or_else(|| format!("{label} must be 0x-prefixed"))?;
    let out = hex::decode(raw).map_err(|e| format!("invalid {label}: {e}"))?;
    if out.len() != bytes {
        return Err(format!("{label} must be {bytes} bytes"));
    }
    Ok(out)
}

fn parse_uint(value: &str, bytes: usize, label: &str) -> Result<Vec<u8>, String> {
    let raw = value.strip_prefix("0x").unwrap_or(value);
    let normalized = if raw.is_empty() { "0" } else { raw };
    let mut decoded = if value.starts_with("0x") {
        let hex_str = if normalized.len() % 2 == 0 { normalized.to_string() } else { format!("0{normalized}") };
        hex::decode(hex_str).map_err(|e| format!("invalid {label}: {e}"))?
    } else {
        decimal_to_be_bytes(normalized, bytes, label)?
    };
    while decoded.first() == Some(&0) && decoded.len() > 1 {
        decoded.remove(0);
    }
    if decoded.len() > bytes {
        return Err(format!("{label} exceeds uint{}", bytes * 8));
    }
    let mut out = vec![0u8; bytes - decoded.len()];
    out.extend(decoded);
    Ok(out)
}

fn decimal_to_be_bytes(value: &str, bytes: usize, label: &str) -> Result<Vec<u8>, String> {
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!("{label} must be an unsigned decimal or 0x hex integer"));
    }
    let mut out = vec![0u8];
    for digit in value.bytes().map(|b| b - b'0') {
        let mut carry = digit as u16;
        for byte in out.iter_mut().rev() {
            let v = (*byte as u16) * 10 + carry;
            *byte = (v & 0xff) as u8;
            carry = v >> 8;
        }
        if carry > 0 { out.insert(0, carry as u8); }
        if out.len() > bytes { return Err(format!("{label} exceeds uint{}", bytes * 8)); }
    }
    Ok(out)
}

fn parse_data(value: &str) -> Result<Vec<u8>, String> {
    let raw = value.strip_prefix("0x").ok_or_else(|| "leg data must be 0x-prefixed".to_string())?;
    let out = hex::decode(raw).map_err(|e| format!("invalid leg data: {e}"))?;
    if out.len() > MAX_PACKED_LEG_DATA { return Err("leg data too large".into()); }
    Ok(out)
}

pub fn encode_batch(req: &PackedBatchRequest) -> Result<Vec<u8>, String> {
    if req.candidates.is_empty() || req.candidates.len() > MAX_PACKED_CANDIDATES {
        return Err("candidate count out of bounds".into());
    }
    let mut out = Vec::with_capacity(128 + req.candidates.len() * 256);
    out.push(PACKED_VERSION);
    out.push(req.candidates.len() as u8);
    out.extend(parse_fixed_hex(&req.asset, 20, "asset")?);
    out.extend(parse_uint(&req.amount, 32, "amount")?);
    out.extend(req.target_block.to_be_bytes());
    out.extend(req.deadline.to_be_bytes());

    for candidate in &req.candidates {
        if candidate.legs.len() < 2 || candidate.legs.len() > MAX_PACKED_LEGS {
            return Err("leg count out of bounds".into());
        }
        out.extend(parse_fixed_hex(&candidate.candidate_id, 32, "candidate_id")?);
        out.extend(parse_uint(&candidate.min_profit, 32, "min_profit")?);
        out.push(candidate.legs.len() as u8);
        for leg in &candidate.legs {
            if leg.token_in.eq_ignore_ascii_case(&leg.token_out) {
                return Err("token_in and token_out must differ".into());
            }
            out.extend(parse_fixed_hex(&leg.adapter, 20, "adapter")?);
            out.extend(parse_fixed_hex(&leg.token_in, 20, "token_in")?);
            out.extend(parse_fixed_hex(&leg.token_out, 20, "token_out")?);
            out.extend(parse_uint(&leg.min_out, 32, "min_out")?);
            let data = parse_data(&leg.data)?;
            out.extend((data.len() as u16).to_be_bytes());
            out.extend(data);
        }
    }
    Ok(out)
}

pub fn validate_batch_bytes(raw: &[u8]) -> Result<(), String> {
    if raw.len() < 70 { return Err("packed batch too short".into()); }
    let mut o = 0usize;
    let take = |o: &mut usize, n: usize| -> Result<&[u8], String> {
        if *o + n > raw.len() { return Err("truncated packed batch".into()); }
        let s = &raw[*o..*o+n];
        *o += n;
        Ok(s)
    };
    if take(&mut o, 1)?[0] != PACKED_VERSION { return Err("unsupported packed version".into()); }
    let count = take(&mut o, 1)?[0] as usize;
    if count == 0 || count > MAX_PACKED_CANDIDATES { return Err("candidate count out of bounds".into()); }
    take(&mut o, 20 + 32 + 8 + 8)?;
    for _ in 0..count {
        take(&mut o, 32 + 32)?;
        let legs = take(&mut o, 1)?[0] as usize;
        if !(2..=MAX_PACKED_LEGS).contains(&legs) { return Err("leg count out of bounds".into()); }
        for _ in 0..legs {
            take(&mut o, 20 + 20 + 20 + 32)?;
            let len_bytes = take(&mut o, 2)?;
            let data_len = u16::from_be_bytes([len_bytes[0], len_bytes[1]]) as usize;
            if data_len > MAX_PACKED_LEG_DATA { return Err("leg data too large".into()); }
            take(&mut o, data_len)?;
        }
    }
    if o != raw.len() { return Err("trailing packed bytes".into()); }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_and_validates() {
        let req = PackedBatchRequest {
            asset: format!("0x{}", "11".repeat(20)), amount: "1000".into(), target_block: 7, deadline: 9,
            candidates: vec![PackedCandidate {
                candidate_id: format!("0x{}", "aa".repeat(32)), min_profit: "10".into(),
                legs: vec![
                    PackedLeg { adapter: format!("0x{}", "33".repeat(20)), token_in: format!("0x{}", "11".repeat(20)), token_out: format!("0x{}", "22".repeat(20)), min_out: "1200".into(), data: "0x0102".into() },
                    PackedLeg { adapter: format!("0x{}", "44".repeat(20)), token_in: format!("0x{}", "22".repeat(20)), token_out: format!("0x{}", "11".repeat(20)), min_out: "1010".into(), data: "0x".into() },
                ],
            }],
        };
        let raw = encode_batch(&req).unwrap();
        validate_batch_bytes(&raw).unwrap();
        assert_eq!(raw[0], PACKED_VERSION);
        assert_eq!(raw[1], 1);
    }
}
