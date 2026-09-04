from arb_bot.shadow_intelligence import (
    route_family, empirical_survival_bps, empirical_capture_bps,
    choose_shadow_action, competitor_fingerprint,
)


def test_route_family_is_order_independent():
    assert route_family(["0x2","0x1"]) == route_family(["0x1","0x2"])


def test_calibration_requires_real_minimum_samples():
    value,n=empirical_survival_bps([100,200,300],150,4)
    assert value is None and n == 3
    value,n=empirical_capture_bps([True,False,True],4)
    assert value is None and n == 3


def test_empirical_calibration_uses_only_observed_samples():
    survival,n=empirical_survival_bps([50,100,150,200],100,4)
    assert survival == 7500 and n == 4
    capture,n=empirical_capture_bps([True,True,False,True],4)
    assert capture == 7500 and n == 4


def test_shadow_now_wait_skip():
    now=choose_shadow_action(deterministic_profit_units=100,survival_bps=8000,capture_bps=7000,survival_samples=20,capture_samples=20,min_samples=20,now_capture_bps=6000,now_survival_bps=7000,wait_survival_bps=3500)
    assert now.action == "NOW"
    wait=choose_shadow_action(deterministic_profit_units=100,survival_bps=5000,capture_bps=4000,survival_samples=20,capture_samples=20,min_samples=20,now_capture_bps=6000,now_survival_bps=7000,wait_survival_bps=3500)
    assert wait.action == "WAIT"
    skip=choose_shadow_action(deterministic_profit_units=100,survival_bps=None,capture_bps=None,survival_samples=2,capture_samples=1,min_samples=20,now_capture_bps=6000,now_survival_bps=7000,wait_survival_bps=3500)
    assert skip.action == "SKIP" and skip.confidence_bps == 0


def test_competitor_fingerprint_counts_multi_pool_transactions():
    events=[
        {"source_sender":"0xabc","tx_touched_pools":["0x1","0x2"],"observed_at_unix_ms":100},
        {"source_sender":"0xabc","tx_touched_pools":["0x2"],"observed_at_unix_ms":160},
        {"source_sender":"0xdef","tx_touched_pools":["0x3"],"observed_at_unix_ms":200},
    ]
    rows=competitor_fingerprint(events)
    assert rows[0]["source_sender"] == "0xabc"
    assert rows[0]["transactions"] == 2
    assert rows[0]["multi_pool_transactions"] == 1
    assert rows[0]["unique_pools"] == 2
    assert rows[0]["median_interarrival_ms"] == 60
