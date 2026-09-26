"""Download IMD 0.25 deg daily gridded rainfall to data/raw/imd.

Run:  python -m src.fetch_imd [--start 1990] [--no-realtime] [--force]

Why this does not use imdlib.get_data
-------------------------------------
imdlib.get_data catches HTTPError, prints it and returns None, so a failed download
looks like success; it has no retry; and its "already downloaded" check only tests
size >= 1024, which happily caches an HTML error page as a .grd. This module POSTs to
the same endpoints imdlib uses, validates every file byte-exactly against
days * 129 * 135 * 4, retries with backoff, and reports what is missing.

Archive files land where imdlib.open_data(fn_format='yearwise',
file_dir=data/raw/imd) expects them: data/raw/imd/rain/<year>.grd. Realtime days are
one file each under data/raw/imd/rain_realtime/.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import random
import time
from pathlib import Path

import requests

from src.common import IMD_BYTES_PER_DAY, IMD_RAW
from src.report import DataError, print_list, summarize

ARCHIVE_URL = "https://imdpune.gov.in/cmpg/Griddata/rainfall.php"
REALTIME_URL = "https://imdpune.gov.in/cmpg/Realtimedata/Rainfall/rain.php"

ARCHIVE_START = 1990
RETRY_DELAYS = (5, 15, 45)  # seconds; a 4th attempt follows the last delay
TIMEOUT = 180
# The realtime endpoint answers in 15-30 s per day; keep concurrency modest.
REALTIME_WORKERS = 6


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def expected_archive_bytes(year: int) -> int:
    return (366 if is_leap(year) else 365) * IMD_BYTES_PER_DAY


def archive_path(year: int) -> Path:
    return IMD_RAW / "rain" / f"{year}.grd"


def realtime_path(day: dt.date) -> Path:
    return IMD_RAW / "rain_realtime" / f"{day:%Y-%m-%d}.grd"


def _post(url: str, payload: dict, dest: Path, expected: int) -> tuple[bool, str]:
    """POST once and write `dest` only if the body is exactly `expected` bytes."""
    try:
        response = requests.post(url, data=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    body = response.content
    if len(body) != expected:
        # A short body is usually an HTML error page; a long one means the grid
        # definition changed. Either way it must not be cached as data.
        head = body[:60].decode("latin-1", "replace").replace("\n", " ")
        return False, f"got {len(body):,} bytes, expected {expected:,} ({head!r})"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return True, "ok"


def download(url: str, payload: dict, dest: Path, expected: int, *,
             label: str, attempts: int = len(RETRY_DELAYS) + 1,
             verbose: bool = True) -> bool:
    """Download with exponential backoff. Returns True on a byte-exact file."""
    for attempt in range(1, attempts + 1):
        ok, why = _post(url, payload, dest, expected)
        if ok:
            if verbose:
                print(f"  {label}: downloaded ({expected:,} B)")
            return True
        if attempt == attempts:
            if verbose:
                print(f"  {label}: FAILED after {attempts} attempts - {why}")
            return False
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]
        delay *= 0.75 + 0.5 * random.random()
        if verbose:
            print(f"  {label}: attempt {attempt}/{attempts} failed ({why}); "
                  f"retrying in {delay:.0f}s")
        time.sleep(delay)
    return False


def have_valid(path: Path, expected: int) -> bool:
    return path.is_file() and path.stat().st_size == expected


def find_latest_archive_year(force: bool = False) -> int:
    """Newest year whose archive file downloads at the exact expected size.

    IMD publishes a year's .grd only once that year is complete, and sometimes late,
    so the latest available year is discovered rather than assumed.
    """
    print("--- probing for the latest available archive year ---")
    this_year = dt.date.today().year
    for year in range(this_year, ARCHIVE_START - 1, -1):
        expected = expected_archive_bytes(year)
        path = archive_path(year)
        if not force and have_valid(path, expected):
            print(f"  {year}: already present")
            return year
        # One quiet attempt per candidate: a missing year is the expected answer
        # here, not an error worth three retries.
        ok, why = _post(ARCHIVE_URL, {"rain": year}, path, expected)
        if ok:
            print(f"  {year}: available ({expected:,} B)")
            return year
        print(f"  {year}: not available - {why}")
    raise DataError(
        f"no archive year between {ARCHIVE_START} and {this_year} is available; "
        f"check {ARCHIVE_URL} by hand"
    )


def fetch_archive(start: int, end: int, force: bool) -> tuple[list[int], list[int]]:
    print(f"\n--- archive years {start}-{end} ---")
    got: list[int] = []
    missing: list[int] = []
    for year in range(start, end + 1):
        expected = expected_archive_bytes(year)
        path = archive_path(year)
        if not force and have_valid(path, expected):
            got.append(year)
            continue
        if download(ARCHIVE_URL, {"rain": year}, path, expected, label=str(year)):
            got.append(year)
        else:
            missing.append(year)
            path.unlink(missing_ok=True)
    print(f"  archive: {len(got)} years present, {len(missing)} missing")
    return got, missing


def _fetch_one_realtime_day(day: dt.date, force: bool) -> tuple[dt.date, bool]:
    path = realtime_path(day)
    if not force and have_valid(path, IMD_BYTES_PER_DAY):
        return day, True
    # Two quiet attempts only: the tail of the year is expected to be absent, so a
    # full backoff ladder per missing day would stall the run for no gain.
    ok = download(REALTIME_URL, {"rain": f"{day:%d%m%Y}"}, path, IMD_BYTES_PER_DAY,
                  label=f"{day:%Y-%m-%d}", attempts=2, verbose=False)
    if not ok:
        path.unlink(missing_ok=True)
    return day, ok


def fetch_realtime(year: int, force: bool,
                   workers: int = REALTIME_WORKERS) -> tuple[list[dt.date], list[dt.date]]:
    """Per-day realtime files for the current (incomplete) year.

    Each day is a separate POST and the endpoint takes 15-30 s to answer, so ~270 days
    would be about two hours in series. The files are ~70 KB, so a small thread pool
    gets it down to minutes without hammering the server - keep `workers` modest.

    A ragged tail is normal: IMD publishes realtime grids with a few days of lag, so
    missing days are reported, never fatal.
    """
    today = dt.date.today()
    start = dt.date(year, 1, 1)
    days = [start + dt.timedelta(days=k) for k in range((today - start).days + 1)]
    print(f"\n--- realtime days {start} -> {today} "
          f"({len(days)} days, {workers} workers) ---")

    got: list[dt.date] = []
    missing: list[dt.date] = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one_realtime_day, d, force): d for d in days}
        for future in cf.as_completed(futures):
            day, ok = future.result()
            (got if ok else missing).append(day)
            done += 1
            if done % 25 == 0 or done == len(days):
                print(f"  {done}/{len(days)} days processed "
                      f"({len(got)} present, {len(missing)} absent)")

    got.sort()
    missing.sort()
    print(f"  realtime: {len(got)} days present, {len(missing)} missing")
    return got, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=ARCHIVE_START)
    parser.add_argument("--no-realtime", action="store_true",
                        help="skip the current-year realtime download")
    parser.add_argument("--force", action="store_true",
                        help="re-download even if a valid file is present")
    parser.add_argument("--workers", type=int, default=REALTIME_WORKERS,
                        help="concurrent realtime-day downloads")
    args = parser.parse_args()

    latest = find_latest_archive_year(force=args.force)
    got, missing = fetch_archive(args.start, latest, args.force)

    rt_got: list[dt.date] = []
    rt_missing: list[dt.date] = []
    if not args.no_realtime and latest < dt.date.today().year:
        rt_got, rt_missing = fetch_realtime(latest + 1, args.force,
                                            workers=args.workers)

    if not got:
        raise DataError("no archive years downloaded; refusing to continue")

    print()
    print_list("missing archive years", missing)
    if rt_missing:
        print_list("missing realtime days",
                   [f"{d:%Y-%m-%d}" for d in rt_missing], limit=10)

    files = [IMD_RAW / "rain"]
    if rt_got:
        files.append(IMD_RAW / "rain_realtime")

    end = f"{rt_got[-1]:%Y-%m-%d}" if rt_got else f"{got[-1]}-12-31"
    summarize(
        "fetch_imd",
        rows=sum(366 if is_leap(y) else 365 for y in got) + len(rt_got),
        files=files,
        date_range=(f"{got[0]}-01-01", end),
        extra={
            "archive years": f"{len(got)} ({got[0]}-{got[-1]})",
            "latest archive": latest,
            "realtime days": len(rt_got),
            "missing years": len(missing),
            "missing days": len(rt_missing),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
