"""MULTI-SPORT SOFT-LINE phase item 1 — src/sport_models.py.

Quant brain extensions beyond soccer Poisson, stdlib ``math`` ONLY, and the
module contract is *never raises*: every public estimator is wrapped so any
internal failure degrades to league priors / neutral probabilities instead of
propagating an exception into a hunt.

Models
------
* Normal-margin model (basketball / NFL / rugby):
      margin ~ N(mu, sigma)
      mu     = (off_home - def_away) - (off_away - def_home) + home_adj
      sigma  = sport default (nba 13.5, nfl 13.0, rugby 12.0), shrunk toward a
               wide prior with sample size n (sigma_eff = s*(1-w) + s_wide*w).
  Spread / total / alt-line probabilities come from 0.5*(1+erf(x)) on the
  margin distribution and the sum distribution (sum ~ N(mu_sum, sigma_sum)).

* Poisson extensions (NHL goals, MLB runs): team lambdas built from
  scoring/allowing rates with home/away splits, Bayesian-shrunk with k=5
  toward the league average:
      rate_shrunk = (hits + k*league_avg) / (games + k)

* Push-aware integer lines everywhere they exist (NFL / MLB / NBA team
  totals, soccer TTO 1.0, NHL integer team totals): exact
  p_win / p_push / p_loss triplets; breakeven decimal price =
  1 + p_loss / p_win (a push refunds, so the fair price is lower than the
  naive 1/p_win).

Every estimator returns ``(p_eff, p_loss, ensemble_kind)`` where ``p_eff``
is the push-adjusted effective win probability (continuous markets use
p_win + 0.5*p_push; integer-push markets keep p_push explicit in the triplet
helpers but p_eff remains the refund-adjusted value used for edge math).
"""
from __future__ import annotations
import math
SQRT2 = math.sqrt(2.0)
SIGMA_DEFAULT = {'nba': 13.5, 'basketball': 13.5, 'nfl': 13.0, 'football': 13.0, 'american_football': 13.0, 'rugby': 12.0, 'rugby_union': 12.0, 'rugby_league': 12.0}
SIGMA_WIDE = 21.0
HOME_ADJ_DEFAULT = {'nba': 2.7, 'nfl': 2.5, 'rugby': 3.0}
LEAGUE_PRIORS = {'nhl': {'avg': 3.1, 'home_split': 1.05, 'away_split': 0.95}, 'mlb': {'avg': 4.45, 'home_split': 1.03, 'away_split': 0.97}}
SHRINK_K = 5
MAX_POISSON_GOALS = 30

def _num(x, default=0.0):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default

def _clamp(x, lo, hi):
    try:
        return max(lo, min(hi, x))
    except Exception:
        return lo

def _safe(fn, *args, **kw):
    """Call fn; on ANY exception return kw['fallback'] (default None)."""
    fallback = kw.pop('fallback', None)
    try:
        out = fn(*args, **kw)
        return out
    except Exception:
        return fallback

def norm_cdf(x, mu=0.0, sigma=1.0):
    """P(X <= x) for X ~ N(mu, sigma) via 0.5*(1+erf((x-mu)/(sigma*sqrt2))).

    Monotone non-decreasing in x by construction; degenerate sigma<=0 falls
    back to a step function at mu. Never raises.
    """

    def _go():
        _v_x = _num(x)
        _v_mu = _num(mu)
        _v_sigma = _num(sigma, 1.0)
        if _v_sigma <= 1e-09:
            if _v_x < _v_mu:
                return 0.0
            if _v_x > _v_mu:
                return 1.0
            return 0.5
        z = (_v_x - _v_mu) / (_v_sigma * SQRT2)
        return _clamp(0.5 * (1.0 + math.erf(z)), 0.0, 1.0)
    v = _safe(_go, fallback=0.5)
    return v

def shrink_sigma(sport, n=0):
    """Sport sigma default shrunk toward SIGMA_WIDE with small samples.

    w = SHRINK_N/(SHRINK_N + n) with SHRINK_N=10: n=0 -> fully wide prior,
    n>=40 -> essentially the raw sport sigma.
    """

    def _go():
        s = SIGMA_DEFAULT.get(str(sport or '').lower(), 13.0)
        _v_n = max(0.0, _num(n))
        w = 10.0 / (10.0 + _v_n)
        return s * (1.0 - w) + SIGMA_WIDE * w
    return _safe(_go, fallback=SIGMA_WIDE)
SHRINK_N = 10.0

def margin_params(off_home, def_away, off_away, def_home, home_adj=None, sport='nba', n=0):
    """Return (mu, sigma_eff) for the home-minus-away margin distribution."""

    def _go():
        mu = _num(off_home) - _num(def_away) - (_num(off_away) - _num(def_home))
        ha = home_adj
        if ha is None:
            ha = HOME_ADJ_DEFAULT.get(str(sport or '').lower(), 2.5)
        mu += _num(ha)
        sig = shrink_sigma(sport, n)
        return (mu, sig)
    return _safe(_go, fallback=(0.0, SIGMA_WIDE))

def poisson_pmf(k, lam):

    def _go():
        _v_k = int(max(0, _num(k)))
        _v_lam = _clamp(_num(lam, 0.1), 1e-06, 60.0)
        return math.exp(-_v_lam) * _v_lam ** _v_k / math.factorial(_v_k)
    return _safe(_go, fallback=0.0)

def poisson_cdf(k, lam):
    """P(X <= k) by direct summation (truncated tail is negligible)."""

    def _go():
        _v_k = int(max(0, _num(k)))
        _v_lam = _clamp(_num(lam, 0.1), 1e-06, 60.0)
        acc = 0.0
        for i in range(min(_v_k, MAX_POISSON_GOALS) + 1):
            acc += poisson_pmf(i, _v_lam)
        return _clamp(acc, 0.0, 1.0)
    return _safe(_go, fallback=0.5)

def poisson_p_exact(k, lam):
    return _safe(lambda: poisson_pmf(k, lam), fallback=0.0)

def shrink_rate(hits, games, league_avg, k=SHRINK_K):
    """Bayesian shrinkage: (hits + k*league_avg) / (games + k), k=5."""

    def _go():
        _v_hits = max(0.0, _num(hits))
        _v_games = max(0.0, _num(games))
        la = _clamp(_num(league_avg, 3.0), 0.01, 60.0)
        return (_v_hits + k * la) / (_v_games + k)
    return _safe(_go, fallback=_num(league_avg, 3.0))

def team_lambdas(home_for, home_against, away_for, away_against, h_games, a_games, league='nhl'):
    """Home/away-split team lambdas from scoring/allowing rates.

    Each argument is (count, games) based; rates are shrunk k=5 toward the
    league average, then combined attack/defense style with the league's
    home/away splits. Returns (lambda_home, lambda_away).
    """

    def _go():
        pr = LEAGUE_PRIORS.get(str(league or '').lower(), LEAGUE_PRIORS['nhl'])
        la = pr['avg']
        rh_sf = shrink_rate(home_for, h_games, la)
        rh_sa = shrink_rate(home_against, h_games, la)
        ra_sf = shrink_rate(away_for, a_games, la)
        ra_sa = shrink_rate(away_against, a_games, la)
        lh = 0.5 * (rh_sf + ra_sa) * pr['home_split']
        lw = 0.5 * (ra_sf + rh_sa) * pr['away_split']
        return (_clamp(lh, 0.1, 12.0), _clamp(lw, 0.1, 12.0))
    return _safe(_go, fallback=(LEAGUE_PRIORS['nhl']['avg'],) * 2)

def push_triplet_normal(line, mu, sigma, side='over'):
    """Integer/half push-aware triplet on a normal sum/margin distribution.

    For a line L with margin/total T ~ N(mu, sigma):
      over: p_win=P(T>L), p_push=P(T==L)=0 (continuous), p_loss=P(T<L)
      integer lines observed discretely get a point mass estimate via the
      density at L scaled by an assumed unit lattice width (used by callers
      that model discrete totals; continuous callers see p_push=0).
    Returns (p_win, p_push, p_loss).
    """

    def _go():
        _v_line = _num(line)
        _v_mu = _num(mu)
        _v_sigma = max(1e-06, _num(sigma, 13.0))
        p_le = norm_cdf(_v_line, _v_mu, _v_sigma)
        over = str(side or 'over').lower().startswith('o')
        if over:
            p_win = _clamp(1.0 - p_le, 0.0, 1.0)
            p_loss = _clamp(p_le, 0.0, 1.0)
        else:
            p_win = _clamp(p_le, 0.0, 1.0)
            p_loss = _clamp(1.0 - p_le, 0.0, 1.0)
        p_push = 0.0
        try:
            if abs(_v_line - round(_v_line)) < 1e-09:
                dens = math.exp(-0.5 * ((_v_line - _v_mu) / _v_sigma) ** 2) / (_v_sigma * math.sqrt(2.0 * math.pi))
                p_push = _clamp(dens, 0.0, 0.35)
        except Exception:
            p_push = 0.0
        total = p_win + p_push + p_loss
        if total > 0:
            p_win /= total
            p_push /= total
            p_loss /= total
        return (p_win, p_push, p_loss)
    return _safe(_go, fallback=(0.5, 0.0, 0.5))

def push_triplet_poisson_total(line, lam_a, lam_b, side='over'):
    """Exact triplet for Over/Under `line` on X+Y, X,Y independent Poisson.

    Handles integer lines exactly (push = P(total == line)), half-lines
    (push = 0), and negative lines. Sums the convolution pmf up to
    MAX_POISSON_GOALS*2. Returns (p_win, p_push, p_loss) summing to 1.
    """

    def _go():
        _v_line = _num(line)
        lam = max(0.05, _num(lam_a, 1.0) + _num(lam_b, 1.0))
        nmax = int(MAX_POISSON_GOALS * 2)
        pmf = [poisson_pmf(i, lam) for i in range(nmax + 1)]
        pmf[nmax] = max(0.0, 1.0 - sum(pmf[:nmax]))
        is_int = abs(_v_line - round(_v_line)) < 1e-09
        p_push = pmf[int(round(_v_line))] if is_int and 0 <= round(_v_line) <= nmax else 0.0
        over = str(side or 'over').lower().startswith('o')
        if over:
            p_win = sum((p for i, p in enumerate(pmf) if i > _v_line))
            p_loss = sum((p for i, p in enumerate(pmf) if i < _v_line))
        else:
            p_win = sum((p for i, p in enumerate(pmf) if i < _v_line))
            p_loss = sum((p for i, p in enumerate(pmf) if i > _v_line))
        total = p_win + p_push + p_loss
        if total > 0:
            p_win /= total
            p_push /= total
            p_loss /= total
        return (p_win, p_push, p_loss)
    return _safe(_go, fallback=(0.5, 0.0, 0.5))

def push_triplet_poisson_team(line, lam, side='over'):
    """Exact triplet for a single-team total (soccer TTO 1.0, NHL/MLB/NFL
    integer team totals) under one Poisson lambda."""

    def _go():
        _v_line = _num(line)
        _v_lam = _clamp(_num(lam, 1.0), 1e-06, 60.0)
        is_int = abs(_v_line - round(_v_line)) < 1e-09
        p_push = poisson_pmf(int(round(_v_line)), _v_lam) if is_int and round(_v_line) >= 0 else 0.0
        over = str(side or 'over').lower().startswith('o')
        cdf_below = poisson_cdf(math.floor(_v_line - 1e-09), _v_lam) if _v_line > 0 else 0.0
        cdf_le = poisson_cdf(int(math.floor(_v_line + 1e-09)), _v_lam)
        if over:
            p_win = _clamp(1.0 - cdf_le, 0.0, 1.0)
            p_loss = _clamp(cdf_below, 0.0, 1.0)
        else:
            p_win = _clamp(cdf_below, 0.0, 1.0)
            p_loss = _clamp(1.0 - cdf_le, 0.0, 1.0)
        total = p_win + p_push + p_loss
        if total > 0:
            p_win /= total
            p_push /= total
            p_loss /= total
        return (p_win, p_push, p_loss)
    return _safe(_go, fallback=(0.5, 0.0, 0.5))

def breakeven_odds(p_win, p_push, p_loss):
    """Fair decimal price with push refunds: 1 + p_loss/p_win.

    (A stake is refunded on push, so EV=0 => price = (p_win+p_loss)/p_win.)
    Never raises; degenerate p_win<=0 returns a large sentinel (1000.0).
    """

    def _go():
        pw = _num(p_win)
        pl = _num(p_loss)
        if pw <= 1e-09:
            return 1000.0
        return _clamp(1.0 + pl / pw, 1.0, 1000.0)
    return _safe(_go, fallback=1000.0)

def estimate_spread(line, ratings, sport='nba', home=True, n=0, kind='model'):
    """Handicap/spread cover probability.

    line  : book spread from the picked side's perspective (e.g. -4.5 means
            pick must win by >4.5; positive = receiving points).
    ratings: dict {off_home, def_away, off_away, def_home, home_adj?}
    Returns (p_eff, p_loss, ensemble_kind). Continuous lines -> p_push=0,
    p_eff=p_win. Integer cushion spreads route through the margin lattice.
    """

    def _go():
        r = ratings or {}
        mu, sig = margin_params(r.get('off_home', 0), r.get('def_away', 0), r.get('off_away', 0), r.get('def_home', 0), r.get('home_adj'), sport, n)
        ln = _num(line)
        if not home:
            mu = -mu
        need = -ln
        p_le = norm_cdf(need, mu, sig)
        p_win = _clamp(1.0 - p_le, 0.0, 1.0)
        p_push = 0.0
        if abs(need - round(need)) < 1e-09:
            dens = math.exp(-0.5 * ((need - mu) / sig) ** 2) / (sig * math.sqrt(2 * math.pi))
            p_push = _clamp(dens, 0.0, 0.3)
            p_win = _clamp(1.0 - p_le - 0.5 * p_push, 0.0, 1.0)
        p_loss = _clamp(1.0 - p_win - p_push, 0.0, 1.0)
        p_eff = _clamp(p_win + 0.5 * p_push, 0.0, 1.0)
        return (p_eff, p_loss, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate_total(line, ratings, sport='nba', side='over', n=0, kind='model'):
    """Over/Under (or alt-total) probability on the normal sum distribution.

    ratings needs {total_mu} (model projected total) or off/def pairs whose
    sum-of-predictions approximates the total; sigma_sum = sigma*sqrt(2)*0.85
    (margin and total share offense variance only partially).
    """

    def _go():
        r = ratings or {}
        mu_t = _num(r.get('total_mu'), 0.0)
        if mu_t <= 0:
            mu_t = (_num(r.get('off_home', 0)) + _num(r.get('off_away', 0))) / 2.0 * 2.0
        sig = shrink_sigma(sport, n) * math.sqrt(2.0) * 0.85
        pw, pp, pl = push_triplet_normal(_num(line), mu_t, sig, side)
        p_eff = _clamp(pw + 0.5 * pp, 0.0, 1.0)
        return (p_eff, pl, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate_alt_total(line, model_total, sport='nba', side='over', n=0, kind='model'):
    """Alt-line cushion total: same normal total engine, but requires the
    |line - model_total| cushion to be respected by the caller; here we just
    price it and tag derived ensembles when the cushion is extreme.
    """

    def _go():
        ratings = {'total_mu': _num(model_total)}
        return estimate_total(line, ratings, sport, side, n, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate_alt_spread(line, model_margin, sport='nba', home=True, n=0, kind='model'):
    """Alt spread priced against a model *expected margin* directly."""

    def _go():
        mu = _num(model_margin)
        if not home:
            mu = -mu
        sig = shrink_sigma(sport, n)
        need = -_num(line)
        p_le = norm_cdf(need, mu, sig)
        p_win = _clamp(1.0 - p_le, 0.0, 1.0)
        p_push = 0.0
        if abs(need - round(need)) < 1e-09:
            dens = math.exp(-0.5 * ((need - mu) / sig) ** 2) / (sig * math.sqrt(2 * math.pi))
            p_push = _clamp(dens, 0.0, 0.3)
            p_win = _clamp(p_win - 0.5 * p_push, 0.0, 1.0)
        p_loss = _clamp(1.0 - p_win - p_push, 0.0, 1.0)
        return (_clamp(p_win + 0.5 * p_push, 0.0, 1.0), p_loss, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate_team_total(line, lam, side='over', kind='model'):
    """Push-aware integer team total (NFL/MLB/NHL/NBA points & soccer TTO).

    lam is the modelled team rate (goals/runs/points-as-Poisson proxy).
    Returns (p_eff, p_loss, ensemble_kind) with p_eff refund-adjusted.
    """

    def _go():
        pw, pp, pl = push_triplet_poisson_team(_num(line), lam, side)
        p_eff = _clamp(pw + 0.5 * pp, 0.0, 1.0)
        return (p_eff, pl, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate_team_total_triplet(line, lam, side='over'):
    """Full (p_win, p_push, p_loss) for betsellers/breakeven math."""
    return _safe(lambda: push_triplet_poisson_team(_num(line), lam, side), fallback=(0.5, 0.0, 0.5))

def estimate_game_total(line, lam_a, lam_b, side='over', kind='model'):
    """Poisson game total (NHL goals / MLB runs incl. first-5 innings)."""

    def _go():
        pw, pp, pl = push_triplet_poisson_total(_num(line), lam_a, lam_b, side)
        p_eff = _clamp(pw + 0.5 * pp, 0.0, 1.0)
        return (p_eff, pl, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate_moneyline(ratings, sport='nba', home=True, n=0, kind='model'):
    """Straight-up winner prob from the margin model (draw mass treated as
    loss for moneyline purposes in NFL/basketball; rugby draws kept as push
    half-credit only when explicitly requested by caller kind)."""

    def _go():
        r = ratings or {}
        mu, sig = margin_params(r.get('off_home', 0), r.get('def_away', 0), r.get('off_away', 0), r.get('def_home', 0), r.get('home_adj'), sport, n)
        if not home:
            mu = -mu
        p_win = _clamp(norm_cdf(0.0, -mu, sig), 0.0, 1.0)
        p_loss = _clamp(1.0 - p_win, 0.0, 1.0)
        return (p_win, p_loss, kind)
    return _safe(_go, fallback=(0.5, 0.5, kind))

def estimate(market_class, params):
    """Dispatch helper: (class_name, dict-of-params) -> triplet tuple."""

    def _go():
        p = params or {}
        mc = str(market_class or '').lower()
        if 'team_total' in mc:
            return estimate_team_total(p.get('line', 1.0), p.get('lam', 1.0), p.get('side', 'over'), p.get('kind', 'model'))
        if 'game_total' in mc or 'alt_total' in mc:
            if 'lam_a' in p:
                return estimate_game_total(p.get('line', 6.5), p.get('lam_a', 3.0), p.get('lam_b', 3.0), p.get('side', 'over'), p.get('kind', 'model'))
            return estimate_alt_total(p.get('line', 220.0), p.get('model_total', 220.0), p.get('sport', 'nba'), p.get('side', 'over'), p.get('n', 0), p.get('kind', 'model'))
        if 'spread' in mc or 'alt_spread' in mc:
            if 'model_margin' in p:
                return estimate_alt_spread(p.get('line', 0.0), p.get('model_margin', 0.0), p.get('sport', 'nba'), p.get('home', True), p.get('n', 0), p.get('kind', 'model'))
            return estimate_spread(p.get('line', 0.0), p.get('ratings', {}), p.get('sport', 'nba'), p.get('home', True), p.get('n', 0), p.get('kind', 'model'))
        if 'moneyline' in mc:
            return estimate_moneyline(p.get('ratings', {}), p.get('sport', 'nba'), p.get('home', True), p.get('n', 0), p.get('kind', 'model'))
        return (0.5, 0.5, p.get('kind', 'model'))
    return _safe(_go, fallback=(0.5, 0.5, 'model'))
