"""`clockwork.naming`, under the name the window has always imported it by.

The module moved below the window when the owner of the hardware was taken out of
`Worker` (`clockwork.owner`), because the owner names a series' replicates and must not
import `clockwork.app`. Nothing is defined here.
"""

from __future__ import annotations

from ..naming import (
    COUNTER_DIGITS,
    clean_initials,
    next_number,
    next_stem,
    parse_stem,
    stem,
)

__all__ = [
    "COUNTER_DIGITS",
    "clean_initials",
    "next_number",
    "next_stem",
    "parse_stem",
    "stem",
]
