"""Marktmaten op koersreeksen (puur, op pandas)."""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _clean(s: pd.Series) -> pd.Series:
    return s.dropna().astype(float)


def history_days(close: pd.Series) -> int:
    return int(_clean(close).shape[0])


def realized_vol(close: pd.Series, window: int = 60) -> Optional[float]:
    """Gerealiseerde jaarvolatiliteit over de laatste `window` dagrendementen."""
    s = _clean(close)
    if len(s) < window + 1:
        return None
    r = np.log(s / s.shift(1)).dropna().iloc[-window:]
    sd = float(r.std(ddof=1))
    return sd * math.sqrt(TRADING_DAYS) if sd > 0 else None


def z_move(close: pd.Series, window: int, vol_window: int = 60,
           reference: Optional[pd.Series] = None, direction: int = 1) -> Optional[float]:
    """Beweging over `window` handelsdagen in sigma-eenheden, in de verwachte richting.

    Met `reference` wordt het afwijkende rendement (excess log return) gemeten.
    """
    s = _clean(close)
    if reference is not None:
        ref = _clean(reference)
        df = pd.concat([s, ref], axis=1, join="inner").dropna()
        if df.shape[0] < max(window, vol_window) + 1:
            return None
        lr = np.log(df.iloc[:, 0] / df.iloc[:, 0].shift(1)) - np.log(df.iloc[:, 1] / df.iloc[:, 1].shift(1))
    else:
        if len(s) < max(window, vol_window) + 1:
            return None
        lr = np.log(s / s.shift(1))
    lr = lr.dropna()
    move = float(lr.iloc[-window:].sum())
    sd = float(lr.iloc[-vol_window:].std(ddof=1))
    if not sd or sd <= 0:
        return None
    return direction * move / (sd * math.sqrt(window))


def addv(close: pd.Series, volume: pd.Series, window: int = 20) -> Optional[float]:
    """Gemiddelde dagomzet in valuta (koers x volume)."""
    df = pd.concat([close, volume], axis=1, join="inner").dropna()
    if df.empty:
        return None
    dv = (df.iloc[:, 0] * df.iloc[:, 1]).iloc[-window:]
    return float(dv.mean()) if len(dv) else None


def corr_matrix(closes: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    r = np.log(closes / closes.shift(1)).iloc[-window:]
    return r.corr(min_periods=max(20, window // 2))
