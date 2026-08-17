from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class Quote:
    venue: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    gas_estimate: int | None
    block_number: int | None
    metadata: dict[str, str | int | bool | None]

class QuoteAdapter(Protocol):
    async def quote_exact_input(self, token_in: str, token_out: str, amount_in: int, **kwargs) -> Quote: ...
