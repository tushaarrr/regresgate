"""Paired regression stats for a fixed golden set: exact McNemar + delta CI + sizing.

Stdlib only (math, statistics). No scipy.

Model: the SAME test cases are run on baseline and candidate, so every case is a
paired observation. Only the discordant pairs carry information:
    b = fixed   (failed on baseline -> passes on candidate)
    c = broken  (passed on baseline -> fails on candidate)
    N = total paired cases
"""

import math
from statistics import NormalDist

_N = NormalDist()


# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #
def mcnemar_exact(b: int, c: int, two_sided: bool = True) -> float:
    """Exact binomial McNemar on the discordant pairs, under H0: b ~ Bin(b+c, 0.5).

    two_sided=True  -> P(|X - n/2| >= |b - n/2|), which for the symmetric p=0.5
                       null is exactly 2 * one-sided tail, clipped at 1.0.
    two_sided=False -> one-sided tail toward the smaller count (i.e. the
                       'candidate is worse' direction when c > b).
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * 0.5**n
    return min(1.0, 2.0 * tail) if two_sided else tail


def mcnemar_chi2(b: int, c: int, continuity: bool = False) -> tuple[float, float]:
    """Asymptotic McNemar chi-square (df=1) and its two-sided p-value.

    Provided for comparison only -- it is the test that lies on small discordance.
    """
    n = b + c
    if n == 0:
        return 0.0, 1.0
    num = abs(b - c) - (1.0 if continuity else 0.0)
    if num < 0:
        num = 0.0
    chi2 = num * num / n
    p = 2.0 * (1.0 - _N.cdf(math.sqrt(chi2)))  # chi2_1 survival == 2*Phi tail
    return chi2, p


# --------------------------------------------------------------------------- #
# effect size
# --------------------------------------------------------------------------- #
def paired_delta(b: int, c: int, n_cases: int, conf: float = 0.95) -> dict:
    """Closed-form paired pass-rate delta and its Wald CI.

        delta_hat = (b - c) / N
        SE        = (1/N) * sqrt(b + c - (b-c)^2 / N)
    """
    if n_cases <= 0:
        raise ValueError("n_cases must be > 0")
    d = (b - c) / n_cases
    var = b + c - (b - c) ** 2 / n_cases
    se = math.sqrt(max(var, 0.0)) / n_cases
    z = _N.inv_cdf(0.5 + conf / 2)
    return {
        "delta": d,
        "se": se,
        "lo": d - z * se,
        "hi": d + z * se,
        "b": b,
        "c": c,
        "n": n_cases,
        "p_exact_two_sided": mcnemar_exact(b, c, True),
        "p_exact_one_sided": mcnemar_exact(b, c, False),
    }


# --------------------------------------------------------------------------- #
# sizing
# --------------------------------------------------------------------------- #
def n_two_proportion(p1: float, p2: float, alpha: float = 0.05,
                     power: float = 0.80, pooled: bool = True) -> int:
    """UNPAIRED two-proportion sample size, PER GROUP (total = 2x this)."""
    za = _N.inv_cdf(1 - alpha / 2)
    zb = _N.inv_cdf(power)
    d = abs(p1 - p2)
    v1, v2 = p1 * (1 - p1), p2 * (1 - p2)
    if pooled:
        pbar = (p1 + p2) / 2
        a = za * math.sqrt(2 * pbar * (1 - pbar))
    else:
        a = za * math.sqrt(v1 + v2)
    return math.ceil((a + zb * math.sqrt(v1 + v2)) ** 2 / d**2)


def n_mcnemar(delta: float, p_disc: float, alpha: float = 0.05,
              power: float = 0.80) -> int:
    """PAIRED (McNemar) sample size in CASES, Connor (1987) normal approximation:

        N = ( z_{a/2} * sqrt(p_disc) + z_b * sqrt(p_disc - delta^2) )^2 / delta^2

    p_disc = P(discordant pair) = (b + c) / N.  delta = (b - c) / N.
    Requires p_disc >= |delta|.
    """
    if p_disc < abs(delta):
        raise ValueError(f"p_disc {p_disc} < |delta| {delta}")
    za = _N.inv_cdf(1 - alpha / 2)
    zb = _N.inv_cdf(power)
    return math.ceil(
        (za * math.sqrt(p_disc) + zb * math.sqrt(p_disc - delta**2)) ** 2 / delta**2
    )


def disc_rate(delta: float, churn: float, symmetric: bool = True) -> float:
    """Discordance rate from an effect + a churn assumption.

    symmetric=True : `churn` is the per-direction flip rate -> p_disc = delta + 2*churn
    symmetric=False: `churn` is the TOTAL unrelated flip rate -> p_disc = delta + churn
    """
    return delta + (2 * churn if symmetric else churn)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # ---- self-check ------------------------------------------------------- #
    assert mcnemar_exact(0, 5, False) == 1 / 32
    assert mcnemar_exact(0, 5, True) == 2 / 32
    assert mcnemar_exact(3, 3, True) == 1.0
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 1, True) == 1.0           # 2 * 0.5 -> clipped
    r = paired_delta(0, 5, 300)
    assert abs(r["delta"] + 5 / 300) < 1e-12
    assert abs(r["se"] - math.sqrt(5 - 25 / 300) / 300) < 1e-15
    assert r["lo"] < r["delta"] < r["hi"]
    assert paired_delta(7, 7, 100)["delta"] == 0.0
    assert n_mcnemar(0.05, 0.05) < n_mcnemar(0.05, 0.09)   # churn costs cases
    assert n_two_proportion(0.90, 0.85) > n_mcnemar(0.05, 0.09)
    print("self-check OK\n")

    def pct(x):
        return f"{100*x:.4f}%"

    print("=== CLAIM 1: chi-square vs exact for b=0, c=5 ===")
    chi2, p_chi = mcnemar_chi2(0, 5)
    chi2c, p_chic = mcnemar_chi2(0, 5, continuity=True)
    print(f"  uncorrected chi2 = {chi2:.4f}  p = {p_chi:.6f}   (claimed 0.025)")
    print(f"  continuity-corr  = {chi2c:.4f}  p = {p_chic:.6f}")
    print(f"  exact one-sided  p = {mcnemar_exact(0,5,False):.6f}")
    print(f"  exact two-sided  p = {mcnemar_exact(0,5,True):.6f}   (claimed 0.0625)")
    print("  -> 0.0625 is the TWO-SIDED exact value (one-sided is 0.03125).")
    print(f"  -> at alpha=.05 chi2 SIGNALS (p={p_chi:.4f}), exact does NOT (p=0.0625).\n")

    print("=== CLAIM 2: 1,362 unpaired vs 281 paired, 90% -> 85%, a=0.05 pw=0.80 ===")
    per = n_two_proportion(0.90, 0.85, pooled=True)
    per_u = n_two_proportion(0.90, 0.85, pooled=False)
    print(f"  unpaired, pooled-SE   : {per} per group, {2*per} total  (claimed 1362)")
    print(f"  unpaired, unpooled-SE : {per_u} per group, {2*per_u} total")
    for churn in (0.0, 0.01, 0.02, 0.05):
        pd = disc_rate(0.05, churn)
        print(f"  paired, churn={churn:.0%} (p_disc={pd:.3f}) : N = {n_mcnemar(0.05, pd)}")
    # what discordance would 281 imply?
    lo, hi = 0.0501, 0.5
    for _ in range(200):
        mid = (lo + hi) / 2
        if n_mcnemar(0.05, mid) < 281:
            lo = mid
        else:
            hi = mid
    print(f"  -> N=281 requires p_disc ~= {hi:.4f}"
          f"  (= 5pp drop + {(hi-0.05)/2:.2%} per-direction churn,"
          f" or + {hi-0.05:.2%} total churn)\n")

    print("=== CLAIM 3: sizing table (Connor, p_disc = delta + 2*churn) ===")
    claimed = {
        (0.05, 0.00): 178, (0.05, 0.01): 251, (0.05, 0.02): 323, (0.05, 0.05): 539,
        (0.03, 0.02): 700,
    }
    print(f"  {'delta':>6} {'churn':>6} {'p_disc':>7} {'computed':>9} {'claimed':>8} {'ratio':>7}")
    for d in (0.03, 0.05, 0.10):
        for ch in (0.00, 0.01, 0.02, 0.05):
            pd = disc_rate(d, ch)
            n = n_mcnemar(d, pd)
            cl = claimed.get((d, ch))
            rat = f"{cl/n:.3f}" if cl else ""
            print(f"  {d:>6.0%} {ch:>6.0%} {pd:>7.3f} {n:>9} {str(cl or '-'):>8} {rat:>7}")
    print()

    print("=== CLAIM 6: alpha=0.05 every night, 30 nights ===")
    print(f"  1 - 0.95^30 = {1 - 0.95**30:.6f}  ({pct(1-0.95**30)})   (claimed 78.5%)")
    print(f"  1 - 0.95^365 = {1 - 0.95**365:.8f}")

    print("\n=== CLAIM 3b: what (alpha, power) would reproduce the claimed table? ===")
    cells = [(0.05, 0.00, 178), (0.05, 0.01, 251), (0.05, 0.02, 323),
             (0.05, 0.05, 539), (0.03, 0.02, 700)]
    print(f"  {'power':>6} " + " ".join(f"{d:.0%}/{c:.0%}={v:<5}" for d, c, v in cells))
    for pw in (0.80, 0.85, 0.90):
        got = [n_mcnemar(d, disc_rate(d, c), power=pw) for d, c, _ in cells]
        print(f"  {pw:>6.2f} " + " ".join(f"{'':>9}{g:<5}" for g in got))
    worst = max(abs(n_mcnemar(d, disc_rate(d, c), power=0.85) / v - 1) for d, c, v in cells)
    print(f"  -> power=0.85 reproduces every claimed cell to within {worst:.2%};"
          f" power=0.80 is ~13% low across the board.")
