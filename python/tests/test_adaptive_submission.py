from arb_bot.adaptive_submission import evaluate_adaptive_submission, linear_survival_probability_bps


def test_linear_survival_probability_is_integer_and_monotonic():
    assert linear_survival_probability_bps(0, 1000) == 10_000
    assert linear_survival_probability_bps(250, 1000) == 7_500
    assert linear_survival_probability_bps(1000, 1000) == 0
    assert linear_survival_probability_bps(1500, 1000) == 0


def test_adaptive_gate_uses_survival_inclusion_failure_and_margin():
    decision = evaluate_adaptive_submission(
        deterministic_net_profit_units=1_000,
        observed_at_unix_ms=1_000,
        now_unix_ms=1_100,
        expected_execution_latency_ms=100,
        survival_window_ms=1_000,
        inclusion_probability_bps=9_000,
        expected_failure_cost_units=10,
        safety_margin_units=700,
    )
    assert decision.total_latency_ms == 200
    assert decision.survival_probability_bps == 8_000
    assert decision.expected_capture_profit_units == 710
    assert decision.eligible is True


def test_stale_opportunity_is_rejected():
    decision = evaluate_adaptive_submission(
        deterministic_net_profit_units=1_000_000,
        observed_at_unix_ms=1_000,
        now_unix_ms=2_000,
        expected_execution_latency_ms=1,
        survival_window_ms=500,
        inclusion_probability_bps=10_000,
        expected_failure_cost_units=0,
        safety_margin_units=0,
    )
    assert decision.survival_probability_bps == 0
    assert decision.expected_capture_profit_units == 0
    assert decision.eligible is False
    assert decision.blocker == "adaptive_expected_capture_below_threshold"
