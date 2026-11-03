"""Shared pytest setup for forecast/ tests.

The forecast scripts import each other as top-level modules (they run as
`python forecast/engine.py`), so the test session needs forecast/ on sys.path.
"""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@pytest.fixture
def write_climo():
    """Write a climo.parquet with the engine's schema (icao, mon, hr, n, r10, r05, r025, rsub)."""
    def _write(path: Path, rows: list[dict]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows, columns=["icao", "mon", "hr", "n", "r10", "r05", "r025", "rsub"])
        con = duckdb.connect()
        con.register("df", df)
        con.execute(f"COPY (SELECT icao, mon::BIGINT AS mon, hr::BIGINT AS hr, n::BIGINT AS n, "
                    f"r10, r05, r025, rsub FROM df) TO '{path}' (FORMAT PARQUET)")
        return path
    return _write
