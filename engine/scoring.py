"""Rekenkern voor de narratiefscore, de componenten N/M/D/P, de I-score en de harde gates.

Alles hier is puur (geen IO) zodat het los te testen en te backtesten is.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence


# --- Narratief (R) -----------------------------------------------------------------

def ema_lambda(half_life_days: float) -> float:
    """Gewicht van de nieuwe waarneming bij een gegeven halfwaardetijd."""
    return 1.0 - 0.5 ** (1.0 / half_life_days)


def narrative_input(articles: Optional[int], alignment: Optional[float]) -> Optional[float]:
    """Dagelijkse input: on-thesis aandacht. None = geen meting (niet hetzelfde als nul)."""
    if articles is None or alignment is None:
        return None
    return max(0.0, float(alignment)) * math.log1p(max(0, int(articles)))


def update_r(prev: Optional[float], x: Optional[float], lam: float) -> tuple[Optional[float], bool]:
    """Genormaliseerde EMA: het evenwicht is gelijk aan de input, onafhankelijk van lambda.

    Geeft (nieuwe R, stale). Ontbrekende data houdt R vast in plaats van naar nul te trekken.
    """
    if x is None:
        return prev, True
    if prev is None:
        return x, False
    return (1.0 - lam) * prev + lam * x, False


def volume_z(today: Optional[int], history: Sequence[Optional[int]], min_days: int,
             std_floor: float) -> Optional[float]:
    """Spike-maat voor de shock-radar: log-volume tegen de eigen basislijn."""
    if today is None:
        return None
    hist = [math.log1p(h) for h in history if h is not None]
    if len(hist) < min_days:
        return None
    mean = sum(hist) / len(hist)
    var = sum((h - mean) ** 2 for h in hist) / max(1, len(hist) - 1)
    std = max(math.sqrt(var), std_floor)
    return (math.log1p(today) - mean) / std


# --- Componenten --------------------------------------------------------------------

def percentile_rank(value: Optional[float], population: Iterable[Optional[float]]) -> Optional[float]:
    """Aandeel van de populatie onder de waarde (gelijke waarden tellen half)."""
    if value is None:
        return None
    pop = [p for p in population if p is not None]
    if not pop:
        return None
    below = sum(1 for p in pop if p < value)
    equal = sum(1 for p in pop if p == value)
    return (below + 0.5 * equal) / len(pop)


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def m_component(z20s: Sequence[Optional[float]], divisor: float) -> Optional[float]:
    """Marktbevestiging: gemiddelde van z20/2, per benchmark begrensd tot 0-1."""
    vals = [clip01(z / divisor) for z in z20s if z is not None]
    return sum(vals) / len(vals) if vals else None


def p_component(z_longs: Sequence[Optional[float]], z_low: float, z_high: float) -> Optional[float]:
    """Ingeprijsd-meter: hoeveel van de beweging in de verwachte richting al heeft plaatsgevonden."""
    vals = [clip01((z - z_low) / (z_high - z_low)) for z in z_longs if z is not None]
    return sum(vals) / len(vals) if vals else None


def d_component(horizon_class: Optional[str], values: dict) -> Optional[float]:
    if horizon_class is None:
        return None
    return float(values.get(horizon_class, 0.0))


def i_score(n: Optional[float], m: Optional[float], d: Optional[float], p: Optional[float],
            floor: float) -> Optional[float]:
    """I = 100 x (N x M x D x (1-P))^1/4, met ondergrens per component.

    Een ontbrekende component geeft None: dan kan een topic niet promoveren.
    """
    if n is None or m is None or d is None or p is None:
        return None
    parts = [max(floor, clip01(n)), max(floor, clip01(m)), max(floor, clip01(d)),
             max(floor, clip01(1.0 - p))]
    prod = 1.0
    for x in parts:
        prod *= x
    return 100.0 * prod ** 0.25


# --- Harde gates --------------------------------------------------------------------

def evaluate_gates(*, alignment_window: Sequence[Optional[float]], min_alignment: float,
                   horizon_class: Optional[str], horizon_exempt: bool,
                   investable: Optional[bool], resolved: bool) -> dict:
    """De vier gates. investable=None betekent 'nog niet getoetst' (On Deck): telt als geslaagd
    totdat Stock Search bij promotie het echte antwoord geeft."""
    vals = [a for a in alignment_window if a is not None]
    thesis = (sum(vals) / len(vals) >= min_alignment) if vals else False
    if horizon_exempt:
        horizon = True
    else:
        horizon = horizon_class is not None and horizon_class != "lt_3m"
    gates = {
        "thesis": thesis,
        "horizon": horizon,
        "investable": investable,
        "unresolved": not resolved,
    }
    gates["pass"] = thesis and horizon and (investable is not False) and not resolved
    return gates
