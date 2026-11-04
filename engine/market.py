"""Koersdata via yfinance, met de database als cache/terugval (yfinance is onofficieel)."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


class MarketData:
    """Interface: closes(symbols) -> DataFrame, volumes(symbols) -> DataFrame, currency(symbol), fx_to_eur(cur)."""

    def __init__(self, repo=None, lookback_days: int = 400):
        self.repo = repo
        self.lookback_days = lookback_days
        self._close = pd.DataFrame()
        self._volume = pd.DataFrame()
        self._currency: dict[str, str] = {}
        self.stale_symbols: set[str] = set()

    def load(self, symbols: list[str], today: date) -> None:
        symbols = sorted({s for s in symbols if s} - set(self._close.columns))
        if not symbols:
            return
        close, volume = self._download(symbols)
        missing = [s for s in symbols if s not in close.columns or close[s].dropna().empty]
        if missing and self.repo is not None:
            c2, v2 = self._from_cache(missing, today)
            close = pd.concat([close, c2], axis=1)
            volume = pd.concat([volume, v2], axis=1)
            self.stale_symbols |= set(c2.columns)
        self._close = pd.concat([self._close, close], axis=1).sort_index()
        self._volume = pd.concat([self._volume, volume], axis=1).sort_index()
        if self.repo is not None and not close.empty:
            self._save_cache(close.drop(columns=list(self.stale_symbols & set(close.columns)), errors="ignore"),
                             volume)

    def _download(self, symbols: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
        try:
            import yfinance as yf
            df = yf.download(symbols, period="2y", interval="1d", auto_adjust=True, progress=False,
                             group_by="column", threads=True)
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance download faalde: %s", e)
            return pd.DataFrame(), pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame(), pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            close = df["Close"].copy()
            volume = df["Volume"].copy() if "Volume" in df.columns.get_level_values(0) else pd.DataFrame()
        else:
            close = df[["Close"]].rename(columns={"Close": symbols[0]})
            volume = df[["Volume"]].rename(columns={"Volume": symbols[0]})
        for frame in (close, volume):
            idx = pd.to_datetime(frame.index)
            frame.index = idx.tz_localize(None) if idx.tz is not None else idx
        return close.dropna(how="all"), volume

    def _from_cache(self, symbols, today):
        rows = self.repo.load_prices(symbols, today - timedelta(days=self.lookback_days))
        if not rows:
            return pd.DataFrame(), pd.DataFrame()
        df = pd.DataFrame(rows)
        df["day"] = pd.to_datetime(df["day"])
        close = df.pivot(index="day", columns="symbol", values="close")
        volume = df.pivot(index="day", columns="symbol", values="volume")
        return close, volume

    def _save_cache(self, close: pd.DataFrame, volume: pd.DataFrame) -> None:
        recent = close.iloc[-260:]
        rows = []
        for sym in recent.columns:
            for ts, val in recent[sym].dropna().items():
                vol = volume[sym].get(ts) if sym in volume.columns else None
                rows.append({"symbol": sym, "day": ts.date(), "close": float(val),
                             "volume": float(vol) if vol is not None and pd.notna(vol) else None})
        try:
            self.repo.save_prices(rows)
        except Exception as e:  # noqa: BLE001
            log.warning("Koerscache opslaan faalde: %s", e)

    def closes(self, symbols: list[str]) -> pd.DataFrame:
        cols = [s for s in symbols if s in self._close.columns]
        return self._close[cols]

    def close(self, symbol: str) -> Optional[pd.Series]:
        return self._close[symbol] if symbol in self._close.columns else None

    def volume(self, symbol: str) -> Optional[pd.Series]:
        return self._volume[symbol] if symbol in self._volume.columns else None

    def last_price(self, symbol: str) -> Optional[float]:
        s = self.close(symbol)
        if s is None:
            return None
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else None

    def currency(self, symbol: str) -> str:
        if symbol in self._currency:
            return self._currency[symbol]
        cur = "USD"
        try:
            import yfinance as yf
            cur = yf.Ticker(symbol).fast_info.get("currency") or "USD"
        except Exception:  # noqa: BLE001
            pass
        self._currency[symbol] = cur
        return cur

    def fx_to_eur(self, currency: str) -> Optional[float]:
        """EUR per 1 eenheid valuta (GBp = Britse pence)."""
        currency = currency or "USD"
        if currency == "GBp":
            gbp = self.fx_to_eur("GBP")
            return gbp / 100 if gbp else None
        currency = currency.upper()
        if currency == "EUR":
            return 1.0
        pair = f"EUR{currency}=X"
        self.load([pair], date.today())
        p = self.last_price(pair)
        return 1.0 / p if p else None
