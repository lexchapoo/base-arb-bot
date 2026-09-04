from __future__ import annotations

from dataclasses import dataclass

BPS = 10_000


@dataclass(frozen=True, slots=True)
class AdaptiveSubmissionDecision:
    eligible: bool
    age_ms: int
    total_latency_ms: int
    survival_probability_bps: int
    inclusion_probability_bps: int
    expected_capture_profit_units: int
    threshold_units: int
    blocker: str | None = None

    def to_dict(self) -> dict[str, int | bool | str | None]:
        return {
            "eligible": self.eligible,
            "age_ms": self.age_ms,
            "total_latency_ms": self.total_latency_ms,
            "survival_probability_bps": self.survival_probability_bps,
            "inclusion_probability_bps": self.inclusion_probability_bps,
            "expected_capture_profit_units": self.expected_capture_profit_units,
            "threshold_units": self.threshold_units,
            "blocker": self.blocker,
        }


def linear_survival_probability_bps(total_latency_ms: int, survival_window_ms: int) -> int:
    """Integer-only survival model.

    The configured survival window is calibration input from real observations. Probability
    decays linearly to zero at the window boundary. This deliberately avoids floating-point
    math in the execution decision path.
    """
    if survival_window_ms <= 0:
        raise ValueError("survival_window_ms must be positive")
    if total_latency_ms <= 0:
        return BPS
    if total_latency_ms >= survival_window_ms:
        return 0
    return ((survival_window_ms - total_latency_ms) * BPS) // survival_window_ms


def evaluate_adaptive_submission(
    *,
    deterministic_net_profit_units: int,
    observed_at_unix_ms: int,
    now_unix_ms: int,
    expected_execution_latency_ms: int,
    survival_window_ms: int,
    inclusion_probability_bps: int,
    expected_failure_cost_units: int,
    safety_margin_units: int,
) -> AdaptiveSubmissionDecision:
    if deterministic_net_profit_units <= 0:
        return AdaptiveSubmissionDecision(False, 0, 0, 0, inclusion_probability_bps, 0, safety_margin_units, "non_positive_deterministic_profit")
    if not 0 <= inclusion_probability_bps <= BPS:
        raise ValueError("inclusion_probability_bps must be between 0 and 10000")
    if expected_execution_latency_ms < 0 or expected_failure_cost_units < 0 or safety_margin_units < 0:
        raise ValueError("latency, failure cost, and safety margin must be non-negative")

    age_ms = max(0, now_unix_ms - observed_at_unix_ms)
    total_latency_ms = age_ms + expected_execution_latency_ms
    survival_bps = linear_survival_probability_bps(total_latency_ms, survival_window_ms)
    capture = deterministic_net_profit_units
    capture = (capture * survival_bps) // BPS
    capture = (capture * inclusion_probability_bps) // BPS
    capture -= expected_failure_cost_units
    threshold = safety_margin_units
    eligible = capture > threshold
    blocker = None if eligible else "adaptive_expected_capture_below_threshold"
    return AdaptiveSubmissionDecision(
        eligible=eligible,
        age_ms=age_ms,
        total_latency_ms=total_latency_ms,
        survival_probability_bps=survival_bps,
        inclusion_probability_bps=inclusion_probability_bps,
        expected_capture_profit_units=capture,
        threshold_units=threshold,
        blocker=blocker,
    )
