"""EWMA control chart for a nightly pass-rate series. Stdlib only.

    z_i = lam*x_i + (1-lam)*z_{i-1},  z_0 = center
    limits: center +/- L * sigma * sqrt( lam/(2-lam) * (1 - (1-lam)^(2i)) )
    steady state: the factor collapses to sqrt(lam/(2-lam))  (= 1/3 at lam=0.2)

sigma is estimated EMPIRICALLY from trailing history, never from the binomial
formula sqrt(p(1-p)/n) -- on a FIXED golden set the nights are not independent
draws, so the binomial number is wildly too wide (see __main__, claim 5).
"""

import math
import random
from statistics import fmean

D2 = 1.128  # E[moving range] / sigma for a normal parent, n=2


def ss_factor(lam: float) -> float:
    """Steady-state EWMA limit factor sqrt(lam / (2 - lam))."""
    return math.sqrt(lam / (2 - lam))


def limit_factor(lam: float, i: int | None = None) -> float:
    """Exact factor at step i (1-based); steady state when i is None."""
    if i is None:
        return ss_factor(lam)
    return ss_factor(lam) * math.sqrt(1 - (1 - lam) ** (2 * i))


def sigma_mr(values) -> float:
    """Empirical sigma from the mean moving range of consecutive points.

    Preferred over stdev(): a slow drift inflates stdev (and so widens the very
    limits meant to catch the drift) but barely touches successive differences.
    """
    v = list(values)
    if len(v) < 2:
        return 0.0
    return fmean(abs(b - a) for a, b in zip(v, v[1:])) / D2


def ewma_chart(series, lam: float = 0.2, L: float = 3.0, warmup: int = 20,
               window: int = 30, steady_state: bool = True,
               sigma_floor: float = 0.0):
    """Yield one dict per point after `warmup`.

    center and sigma are re-estimated each night from the trailing `window`
    in-control points (the points already seen, alarms included -- keep the
    baseline honest by dropping alarmed nights upstream if you prefer).
    """
    s = list(series)
    if len(s) <= warmup:
        return
    z = fmean(s[:warmup])
    for i, x in enumerate(s[warmup:], start=1):
        hist = s[max(0, warmup + i - 1 - window): warmup + i - 1]
        center = fmean(hist)
        sigma = max(sigma_mr(hist), sigma_floor)
        z = lam * x + (1 - lam) * z
        hw = L * sigma * limit_factor(lam, None if steady_state else i)
        yield {
            "i": warmup + i - 1, "x": x, "z": z, "center": center,
            "sigma": sigma, "ucl": center + hw, "lcl": center - hw,
            # +1e-12: a perfectly flat history gives sigma=0 and hw=0, where float
            # noise in the recursion alone would otherwise alarm every night.
            "alarm": abs(z - center) > hw + 1e-12,
            "low_alarm": z - center < -(hw + 1e-12),
        }


# --------------------------------------------------------------------------- #
# ARL simulation (in-control average run length)
# --------------------------------------------------------------------------- #
def arl_known_sigma(lam=0.2, L=3.0, runs=20000, cap=20000, seed=20250921,
                    steady_state=True):
    """ARL0 with mu=0, sigma=1 KNOWN. Standard textbook setting."""
    rng = random.Random(seed)
    total = 0
    for _ in range(runs):
        z, i = 0.0, 0
        while i < cap:
            i += 1
            z = lam * rng.gauss(0, 1) + (1 - lam) * z
            if abs(z) > L * limit_factor(lam, None if steady_state else i):
                break
        total += i
    return total / runs


def arl_empirical_sigma(lam=0.2, L=3.0, window=30, runs=4000, cap=20000,
                        seed=20250921):
    """ARL0 when center and sigma are re-estimated nightly from a rolling window."""
    rng = random.Random(seed)
    k = L * ss_factor(lam)
    total = 0
    for _ in range(runs):
        hist = [rng.gauss(0, 1) for _ in range(window)]
        z, i = fmean(hist), 0
        while i < cap:
            i += 1
            x = rng.gauss(0, 1)
            center, sigma = fmean(hist), sigma_mr(hist)
            z = lam * x + (1 - lam) * z
            if abs(z - center) > k * sigma:
                break
            hist = hist[1:] + [x]
        total += i
    return total / runs


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # ---- self-check ------------------------------------------------------- #
    assert abs(ss_factor(0.2) - 1 / 3) < 1e-15          # lam=.2 -> exactly 1/3
    assert abs(limit_factor(0.2, 1) - (1 / 3) * math.sqrt(1 - 0.8**2)) < 1e-15
    assert limit_factor(0.2, 1) < limit_factor(0.2, 50) <= ss_factor(0.2)
    assert sigma_mr([1.0]) == 0.0 and sigma_mr([]) == 0.0
    assert abs(sigma_mr([0, 1, 0, 1, 0]) - 1 / D2) < 1e-12
    flat = [0.9] * 40
    assert not any(p["alarm"] for p in ewma_chart(flat))          # sigma 0, x==center
    noisy = [0.9 + 0.005 * math.sin(i) for i in range(60)]
    assert not any(p["alarm"] for p in ewma_chart(noisy)), "in-control must not alarm"
    stepped = noisy[:40] + [0.83 + 0.005 * math.sin(i) for i in range(40, 60)]
    pts = list(ewma_chart(stepped))
    assert any(p["low_alarm"] for p in pts), "a 7pp step must alarm"
    first = next(p["i"] for p in pts if p["alarm"])
    assert first - 40 <= 5, f"alarm took {first-40} nights"
    print("self-check OK\n")

    print("=== CLAIM 5: nightly SD, 300-case fixed golden set, p=0.90, 1% flip rate ===")
    n, p, f = 300, 0.90, 0.01
    binom = math.sqrt(p * (1 - p) / n)
    flip = math.sqrt(n * f * (1 - f)) / n
    print(f"  binomial formula sqrt(p(1-p)/n)      = {100*binom:.4f} pp   (claimed 1.73)")
    print(f"  fixed-set flip model sqrt(n f(1-f))/n= {100*flip:.4f} pp   (claimed 0.57)")
    rng = random.Random(20250921)
    stable = [True] * 270 + [False] * 30
    sim = []
    for _ in range(20000):
        sim.append(sum((s != (rng.random() < f)) for s in stable) / n)
    m = fmean(sim)
    sd = math.sqrt(fmean((v - m) ** 2 for v in sim))
    print(f"  simulated (20000 nights, seed 20250921): mean {100*m:.3f}pp"
          f"  SD {100*sd:.4f} pp")
    print(f"  -> binomial is {binom/flip:.2f}x too wide; a real 3pp break sits"
          f" {0.03/flip:.1f} flip-SDs out but only {0.03/binom:.1f} binomial-SDs out.\n")

    print("=== CLAIM 4: EWMA lam=0.2 L=3.0 -> 'a false alarm every 2.4 years' ===")
    print(f"  steady-state factor sqrt(lam/(2-lam)) = {ss_factor(0.2):.6f}"
          f"  -> limits = center +/- {3*ss_factor(0.2):.3f} sigma")
    for ss, label in ((True, "steady-state limits"), (False, "exact time-varying")):
        a = arl_known_sigma(steady_state=ss)
        print(f"  ARL0, sigma KNOWN, {label:22s}: {a:8.1f} nights = {a/365:.2f} years")
    a = arl_empirical_sigma()
    print(f"  ARL0, sigma EMPIRICAL (MR, window=30)     : {a:8.1f} nights = {a/365:.2f} years")
    print(f"  claimed 2.4 years = {2.4*365:.0f} nights")
