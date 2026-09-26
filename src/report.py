"""The run-summary contract from CLAUDE.md §11, in one place.

Every pipeline module ends with `summarize(...)` and guards its outputs with
`require_nonempty(...)`, so "prints a summary / fails loudly on empty output" is
implemented once rather than re-invented per script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np


class DataError(RuntimeError):
    """Raised when a pipeline step produced nothing usable.

    Distinct from a bug: it means the step ran but its output is empty or all-NaN,
    which must abort the run rather than write a silent empty file.
    """


def require_nonempty(obj: Any, what: str) -> Any:
    """Return `obj`, or raise DataError if it is empty / all-NaN / a zero-byte file.

    Handles the shapes this pipeline actually produces: DataFrames and GeoDataFrames,
    numpy arrays, xarray objects, Paths, and plain sized containers.
    """
    if obj is None:
        raise DataError(f"{what}: got None")

    # Path -> the file must exist and be non-empty.
    if isinstance(obj, Path):
        if not obj.exists():
            raise DataError(f"{what}: file was not written: {obj}")
        if obj.is_file() and obj.stat().st_size == 0:
            raise DataError(f"{what}: file is zero bytes: {obj}")
        return obj

    # pandas / geopandas
    if hasattr(obj, "empty"):
        if obj.empty:
            raise DataError(f"{what}: no rows")
        return obj

    # numpy / xarray
    if hasattr(obj, "size"):
        if obj.size == 0:
            raise DataError(f"{what}: zero-size array")
        values = getattr(obj, "values", obj)
        values = np.asarray(values)
        if values.dtype.kind == "f" and not np.isfinite(values).any():
            raise DataError(f"{what}: every value is NaN or infinite")
        return obj

    if isinstance(obj, Sequence) and len(obj) == 0:
        raise DataError(f"{what}: empty")

    return obj


def _fmt_files(files: Iterable[Path] | None) -> list[str]:
    if not files:
        return []
    out = []
    for f in files:
        f = Path(f)
        if f.exists():
            size = f.stat().st_size if f.is_file() else sum(
                p.stat().st_size for p in f.rglob("*") if p.is_file()
            )
            out.append(f"{f} ({_human(size)})")
        else:
            out.append(f"{f} (MISSING)")
    return out


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GiB"


def summarize(
    name: str,
    *,
    rows: int | None = None,
    files: Iterable[Path] | None = None,
    date_range: tuple[Any, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Print the standard end-of-run block: rows, files written, date range."""
    print(f"\n=== {name} - summary ===")
    if rows is not None:
        print(f"  rows          : {rows:,}")
    if date_range is not None:
        start, end = date_range
        print(f"  date range    : {start} -> {end}")
    for i, line in enumerate(_fmt_files(files)):
        label = "files written " if i == 0 else "              "
        print(f"  {label}: {line}")
    for key, value in (extra or {}).items():
        print(f"  {key:<14}: {value}")


def print_list(label: str, items: Sequence[Any], limit: int = 20) -> None:
    """Print a labelled list, truncated, always showing the true count."""
    print(f"  {label}: {len(items)}")
    if not items:
        return
    shown = list(items)[:limit]
    for item in shown:
        print(f"      - {item}")
    if len(items) > limit:
        print(f"      ... and {len(items) - limit} more")
