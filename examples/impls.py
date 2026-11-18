"""Demo callables used by the example specs and by ``evidence/run.py``.

Two of the candidates here are deliberately *bad* optimisations.  They exist to
show what speedgap catches, not to recommend the pattern:

* ``candidate_stats`` memoises the last parsed document (``lru_cache(maxsize=1)``),
  so it only wins when the same document is fed in over and over (exactly what
  a synthetic loop does);
* ``candidate_normalize_nbsp`` looks identical on ASCII synthetic text but
  changes the output on real text containing U+00A0.

``candidate_scan`` is the honest one: ``str.count`` is faster than a Python
character loop on every input, synthetic or real.
"""

from __future__ import annotations

import functools
import random
from typing import List

WORDS = (
    "alpha", "box", "cache", "delta", "extra", "fox", "gamma", "index", "jazz",
    "kappa", "linux", "matrix", "next", "orbit", "pixel", "quartz", "relax",
    "syntax", "tax", "unit", "vortex", "wax", "xenon", "yield", "zebra",
)

# One module-level string: ``make_repeated`` puts this same document in every
# slot, which is what the one-entry cache keys on.
REPEATED_DOC = " ".join(WORDS[i % len(WORDS)] for i in range(120))


def baseline_scan(text: str) -> int:
    """Count 'x' with an explicit Python loop."""
    total = 0
    for ch in text:
        if ch == "x":
            total += 1
    return total


def candidate_scan(text: str) -> int:
    """Same answer via the C-level str.count."""
    return text.count("x")


def baseline_stats(text: str) -> List[int]:
    words = text.split()
    return [len(words), sum(len(word) for word in words), sum(ord(ch) for ch in text[:200])]


@functools.lru_cache(maxsize=1)
def _stats_cached(text: str) -> List[int]:
    return baseline_stats(text)


def candidate_stats(text: str) -> List[int]:
    """One-entry memo: wins only when the same text repeats back-to-back."""
    return _stats_cached(text)


def baseline_passthrough(text: str) -> str:
    return text


def candidate_normalize_nbsp(text: str) -> str:
    """Semantically different on real text: NBSP becomes a normal space."""
    return text.replace("\u00a0", " ")


def make_docs(seed: int, count: int) -> List[str]:
    """Varied ASCII documents: no repeated object, no NBSP."""
    rng = random.Random(seed)
    docs = []
    for _ in range(count):
        length = rng.randint(40, 160)
        docs.append(" ".join(rng.choice(WORDS) for _ in range(length)))
    return docs


def make_repeated(seed: int, count: int) -> List[str]:
    """The synthetic trap: `count` slots, one shared object."""
    del seed  # kept for the factory signature
    return [REPEATED_DOC] * count
