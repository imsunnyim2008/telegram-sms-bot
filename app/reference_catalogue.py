"""Read-only reference names imported from a customer-supplied catalogue.

These names are not evidence of stock, pricing, purchase approval or provider IDs.
"""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def reference_names():
    return tuple(name.strip() for name in
                 Path(__file__).with_name('reference_catalogue.txt').read_text(
                     encoding='utf-8').splitlines() if name.strip())
