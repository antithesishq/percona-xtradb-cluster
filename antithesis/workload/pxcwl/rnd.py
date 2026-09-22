"""Randomness, exclusively through the Antithesis SDK.

Every random decision in this workload goes through ``antithesis.random`` so
that timelines replay deterministically. Direct use of ``random``,
``os.urandom``, ``uuid``, or any time-seeded value as a decision input is
banned across this package -- see ``ops.new_wid`` for how unique write keys are
minted without them.

Only two SDK primitives are used, ``get_random`` and ``random_choice``, so this
module does not depend on any richer API existing in a given SDK version.

Where a value has a natural menu, prefer ``choice`` over ``randint``: telling
Antithesis "this is a choice among N options" is what lets it learn which
inputs are fruitful, whereas asking for a 64-bit integer and taking it modulo N
hides the structure.
"""

from __future__ import annotations

from typing import Sequence, TypeVar

from antithesis.random import get_random, random_choice

T = TypeVar("T")

_SCALE = 10_000


def choice(seq: Sequence[T]) -> T:
    """Pick one element. Structured, so Antithesis can learn the choice."""
    return random_choice(list(seq))


def randint(low: int, high: int) -> int:
    """Inclusive integer in [low, high]. Use only where no menu is natural."""
    if high <= low:
        return low
    return low + get_random() % (high - low + 1)


def chance(probability: float) -> bool:
    """True with the given probability."""
    if probability <= 0.0:
        return False
    if probability >= 1.0:
        return True
    return (get_random() % _SCALE) < int(probability * _SCALE)


def weighted_choice(weights: dict[str, float]) -> str | None:
    """Pick a key with probability proportional to its weight.

    Returns None when every weight is zero -- which is a legitimate outcome:
    the swarm deliberately zeroes whole action classes for a timeline.
    """
    live = {k: w for k, w in weights.items() if w > 0}
    if not live:
        return None
    keys = sorted(live)
    total = sum(live[k] for k in keys)
    # get_random() is uniform over u64; scaling it to the weight total keeps the
    # draw proportional without seeding a generator of our own.
    point = (get_random() % _SCALE) / _SCALE * total
    upto = 0.0
    for k in keys:
        upto += live[k]
        if point < upto:
            return k
    return keys[-1]


def shuffled(seq: Sequence[T]) -> list[T]:
    """A shuffled copy, drawn one structured choice at a time."""
    pool = list(seq)
    out: list[T] = []
    while pool:
        pick = random_choice(pool)
        pool.remove(pick)
        out.append(pick)
    return out


def sample_menu(base: int, *, floor: int = 0) -> int:
    """Draw from a configured-limit family around ``base``.

    Boundary values plus the just-under / at / just-over / well-over
    neighbourhood of the configured limit, rather than an arbitrary range, so
    draws land on the corners where behaviour changes instead of in the bulk.
    """
    menu = [floor, floor + 1, base - 1, base, base + 1, 2 * base]
    return max(floor, choice(menu))
