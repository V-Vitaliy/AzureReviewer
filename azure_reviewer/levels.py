from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    name: str
    context_chars: int
    max_output_tokens: int
    max_diff_chars: int


LEVELS = {level.name: level for level in (
    Level("easy", context_chars=12_000, max_output_tokens=3000, max_diff_chars=60_000),
    Level("medium", context_chars=16_000, max_output_tokens=4000, max_diff_chars=60_000),
    Level("hard", context_chars=24_000, max_output_tokens=6000, max_diff_chars=120_000),
)}
DEFAULT_LEVEL = "medium"
