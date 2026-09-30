"""Fair-value model helpers shared by backtest and (later) live paper trader."""
import numpy as np
from scipy import stats


def fee_per_contract(price, rounded=False, coef=0.07):
    """Kalshi taker fee: ceil_to_cent(coef * C * P * (1-P)) per ORDER.
    rounded=False -> amortized per-contract (large orders); rounded=True -> 1-lot worst case."""
    f = coef * price * (1 - price)
    if rounded:
        return np.ceil(np.round(f * 100, 9)) / 100
    return f


def unit_t_cdf(z, nu):
    """CDF of Student-t rescaled to unit variance."""
    if nu is None or nu == np.inf:
        return stats.norm.cdf(z)
    scale = np.sqrt((nu - 2) / nu)
    return stats.t.cdf(z / scale, df=nu)


def prob_above(S, K, sigma_tot, nu=None, basis=0.0):
    """P(S_T + basis > K) with ln(S_T/S) ~ sigma_tot * Z (zero drift). sigma_tot is total
    (horizon) log-vol. Vectorized."""
    S = np.asarray(S, float)
    K = np.asarray(K, float)
    sig = np.maximum(np.asarray(sigma_tot, float), 1e-9)
    z = (np.log(np.maximum(K - basis, 1e-9)) - np.log(S)) / sig
    return 1.0 - unit_t_cdf(z, nu)


def prob_between(S, K1, K2, sigma_tot, nu=None, basis=0.0):
    return prob_above(S, K1, sigma_tot, nu, basis) - prob_above(S, K2, sigma_tot, nu, basis)


def horizon_minutes_eff(lag_min):
    """Settlement is the 60s average ending at close; variance of that average over the
    final minute is 1/3 of a point-in-time price, so effective horizon = lag - 2/3 min."""
    return np.maximum(np.asarray(lag_min, float) - 2.0 / 3.0, 1.0 / 3.0)
