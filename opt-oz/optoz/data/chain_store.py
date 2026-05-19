"""
Parquet-based option chain store.

Stores EOD chain snapshots partitioned by underlying and date.
Schema is columnar so reading a single underlying's history is fast.

Layout on disk:
  /data/chains/<SYMBOL>/YYYY-MM-DD.parquet
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..models import OptionChain, OptionContract, Right

log = logging.getLogger(__name__)

_SCHEMA = pa.schema([
    pa.field("symbol", pa.string()),
    pa.field("expiry", pa.date32()),
    pa.field("strike", pa.float32()),
    pa.field("right", pa.string()),
    pa.field("bid", pa.float32()),
    pa.field("ask", pa.float32()),
    pa.field("last", pa.float32()),
    pa.field("volume", pa.int32()),
    pa.field("open_interest", pa.int32()),
    pa.field("iv", pa.float32()),
    pa.field("delta", pa.float32()),
    pa.field("gamma", pa.float32()),
    pa.field("theta", pa.float32()),
    pa.field("vega", pa.float32()),
    pa.field("underlying_price", pa.float32()),
    pa.field("snapshot_date", pa.date32()),
])


class ChainStore:
    def __init__(self, data_dir: Optional[str] = None):
        base = data_dir or os.getenv("DATA_DIR", "/data")
        self.root = Path(base) / "chains"
        self.root.mkdir(parents=True, exist_ok=True)

    # ── write ───────────────────────────────────────────────────────────────

    def save(self, chain: OptionChain) -> None:
        sym_dir = self.root / chain.symbol
        sym_dir.mkdir(exist_ok=True)
        path = sym_dir / f"{chain.snapshot_date.isoformat()}.parquet"

        rows = []
        for c in chain.contracts:
            rows.append({
                "symbol": c.symbol,
                "expiry": c.expiry,
                "strike": c.strike,
                "right": c.right.value,
                "bid": c.bid,
                "ask": c.ask,
                "last": c.last,
                "volume": c.volume,
                "open_interest": c.open_interest,
                "iv": c.iv,
                "delta": c.delta,
                "gamma": c.gamma,
                "theta": c.theta,
                "vega": c.vega,
                "underlying_price": c.underlying_price,
                "snapshot_date": chain.snapshot_date,
            })

        if not rows:
            log.warning("ChainStore.save: no contracts for %s %s", chain.symbol, chain.snapshot_date)
            return

        df = pd.DataFrame(rows)
        table = pa.Table.from_pandas(df, schema=_SCHEMA)
        pq.write_table(table, path, compression="snappy")
        log.info("ChainStore: saved %d contracts → %s", len(rows), path)

    # ── read ────────────────────────────────────────────────────────────────

    def load(self, symbol: str, snapshot_date: date) -> Optional[OptionChain]:
        path = self.root / symbol / f"{snapshot_date.isoformat()}.parquet"
        if not path.exists():
            return None

        df = pd.read_parquet(path)
        return _df_to_chain(df, symbol, snapshot_date)

    def load_range(self, symbol: str, start: date, end: date) -> list[OptionChain]:
        sym_dir = self.root / symbol
        if not sym_dir.exists():
            return []

        chains = []
        for p in sorted(sym_dir.glob("*.parquet")):
            d = date.fromisoformat(p.stem)
            if start <= d <= end:
                df = pd.read_parquet(p)
                chains.append(_df_to_chain(df, symbol, d))
        return chains

    def available_dates(self, symbol: str) -> list[date]:
        sym_dir = self.root / symbol
        if not sym_dir.exists():
            return []
        return sorted(
            date.fromisoformat(p.stem)
            for p in sym_dir.glob("*.parquet")
        )

    def latest(self, symbol: str) -> Optional[OptionChain]:
        dates = self.available_dates(symbol)
        return self.load(symbol, dates[-1]) if dates else None


def _df_to_chain(df: pd.DataFrame, symbol: str, snapshot_date: date) -> OptionChain:
    underlying_price = float(df["underlying_price"].iloc[0]) if not df.empty else 0.0
    contracts = []
    for _, row in df.iterrows():
        contracts.append(OptionContract(
            symbol=row["symbol"],
            expiry=row["expiry"].item() if hasattr(row["expiry"], "item") else row["expiry"],
            strike=float(row["strike"]),
            right=Right(row["right"]),
            bid=float(row["bid"]),
            ask=float(row["ask"]),
            last=float(row["last"]),
            volume=int(row["volume"]),
            open_interest=int(row["open_interest"]),
            iv=float(row["iv"]),
            delta=float(row["delta"]),
            gamma=float(row["gamma"]),
            theta=float(row["theta"]),
            vega=float(row["vega"]),
            underlying_price=underlying_price,
        ))
    return OptionChain(
        symbol=symbol,
        snapshot_date=snapshot_date,
        underlying_price=underlying_price,
        contracts=contracts,
    )
