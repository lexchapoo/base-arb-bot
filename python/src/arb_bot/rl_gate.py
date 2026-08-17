from enum import IntEnum
from .models import RouteCandidate, ScoreResult

class SizeAction(IntEnum):
    SKIP=0
    QUARTER=25
    HALF=50
    FULL=100

class DeterministicPolicy:
    """Safe baseline. Replace with a trained policy only after it beats this baseline out of sample."""
    def choose(self, candidate: RouteCandidate, score: ScoreResult) -> SizeAction:
        if not score.approved: return SizeAction.SKIP
        if candidate.success_probability >= 0.90 and score.expected_value_usd >= 2: return SizeAction.FULL
        if candidate.success_probability >= 0.80: return SizeAction.HALF
        return SizeAction.QUARTER
