"""Glue the gate expects but the stats module does not expose.

gate.py imports `delta_ci` and calls `mcnemar_exact(b, c)` positionally.
stats/paired.py exposes `paired_delta` and defaults mcnemar_exact to TWO-sided.
"""
from stats.paired import paired_delta, mcnemar_exact as _mx


def delta_ci(b, c, n, conf=0.95):
    d = paired_delta(b, c, n, conf)
    return (100.0 * d["lo"], 100.0 * d["hi"])


def mcnemar_one_sided_worse(b, c):
    """Directional: P(X >= c | X ~ Bin(b+c, .5)). paired.mcnemar_exact(...,False)
    keys off min(b,c) and is therefore direction-BLIND."""
    from math import comb
    n = b + c
    return 1.0 if n == 0 else sum(comb(n, k) for k in range(c, n + 1)) / 2.0**n


if __name__ == "__main__":
    assert abs(mcnemar_one_sided_worse(0, 6) - 0.015625) < 1e-12
    # direction-blindness of the real module's one-sided flag:
    assert _mx(17, 4, False) == _mx(4, 17, False)
    assert mcnemar_one_sided_worse(17, 4) != mcnemar_one_sided_worse(4, 17)
    lo, hi = delta_ci(4, 17, 300)
    assert lo < -4.33 < hi, (lo, hi)
    print("adapter self-check OK")
