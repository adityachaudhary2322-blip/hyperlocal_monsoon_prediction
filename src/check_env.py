"""Environment smoke check.

Run:  python -m src.check_env
Prints torch version, CUDA availability, GPU name and free VRAM, then verifies that
every package the project depends on imports. Exits non-zero if anything is missing.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import logging
import platform
import sys

# (import name, friendly name) — keep in sync with requirements.txt
PACKAGES: list[tuple[str, str]] = [
    ("numpy", "numpy"),
    ("pandas", "pandas"),
    ("pyarrow", "pyarrow"),
    ("scipy", "scipy"),
    ("xarray", "xarray"),
    ("netCDF4", "netCDF4"),
    ("geopandas", "geopandas"),
    ("shapely", "shapely"),
    ("imdlib", "imdlib"),
    ("cdsapi", "cdsapi"),
    ("lightgbm", "lightgbm"),
    ("sklearn", "scikit-learn"),
    ("autogluon.timeseries", "autogluon.timeseries"),
    ("matplotlib", "matplotlib"),
    ("folium", "folium"),
    ("streamlit", "streamlit"),
    ("streamlit_folium", "streamlit-folium"),
    ("dotenv", "python-dotenv"),
    ("twilio", "twilio"),
    ("yaml", "pyyaml"),
    ("rapidfuzz", "rapidfuzz"),
    ("pytest", "pytest"),
]


def _version(mod, distribution: str) -> str:
    """Module attribute if it has one, else the installed distribution version."""
    for attr in ("__version__", "version", "VERSION"):
        v = getattr(mod, attr, None)
        if isinstance(v, str):
            return v
    try:
        return md.version(distribution)
    except md.PackageNotFoundError:
        return "?"


def report_torch() -> list[str]:
    """Print the torch / CUDA block. Returns a list of problems found."""
    problems: list[str] = []
    print("--- torch / GPU ---")
    try:
        import torch
    except Exception as exc:  # pragma: no cover - environment failure path
        print(f"  torch              : IMPORT FAILED ({exc})")
        return ["torch failed to import"]

    print(f"  torch version      : {torch.__version__}")
    print(f"  built with CUDA    : {torch.version.cuda}")
    available = torch.cuda.is_available()
    print(f"  cuda available     : {available}")

    if not available:
        problems.append("CUDA is not available to torch")
        return problems

    print(f"  device count       : {torch.cuda.device_count()}")
    for i in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(i)
        free_b, total_b = torch.cuda.mem_get_info(i)
        gib = 1024**3
        print(f"  GPU {i} name       : {props.name}")
        print(f"  GPU {i} capability : sm_{props.major}{props.minor}")
        print(f"  GPU {i} total VRAM : {props.total_memory / gib:.2f} GiB")
        print(f"  GPU {i} free VRAM  : {free_b / gib:.2f} GiB "
              f"(of {total_b / gib:.2f} GiB)")

    # Tiny float32 round-trip: proves kernels actually launch, not just that the
    # driver reports a device.
    x = torch.randn(256, 256, device="cuda", dtype=torch.float32)
    y = (x @ x).sum().item()
    print(f"  matmul smoke test  : ok (checksum {y:.4f})")
    return problems


_SCRIPT_RUN_CONTEXT_LOGGER = "streamlit.runtime.scriptrunner_utils.script_run_context"


def _quiet_streamlit() -> None:
    """Drop streamlit's bare-mode "missing ScriptRunContext" warning.

    streamlit-folium triggers it at import time. It is meaningless outside
    `streamlit run` and only clutters this report. A filter is used rather than
    setLevel because streamlit re-configures its own logger levels on import,
    which would undo a level change made beforehand.
    """
    if "streamlit" not in sys.modules:
        return
    logger = logging.getLogger(_SCRIPT_RUN_CONTEXT_LOGGER)
    if not any(getattr(f, "_monsoon_ai", False) for f in logger.filters):
        drop = lambda record: "missing ScriptRunContext" not in record.getMessage()
        drop._monsoon_ai = True  # type: ignore[attr-defined]
        logger.addFilter(drop)


def report_packages() -> list[str]:
    print("\n--- packages ---")
    missing: list[str] = []
    width = max(len(name) for _, name in PACKAGES)
    for import_name, friendly in PACKAGES:
        try:
            mod = importlib.import_module(import_name)
        except Exception as exc:
            print(f"  {friendly:<{width}} : MISSING ({type(exc).__name__}: {exc})")
            missing.append(friendly)
        else:
            print(f"  {friendly:<{width}} : {_version(mod, friendly)}")
            _quiet_streamlit()
    return missing


def main() -> int:
    print("--- interpreter ---")
    print(f"  python             : {sys.version.split()[0]} ({platform.machine()})")
    print(f"  executable         : {sys.executable}")
    print(f"  platform           : {platform.platform()}")
    print()

    problems = report_torch()
    missing = report_packages()

    print("\n--- summary ---")
    print(f"  packages checked   : {len(PACKAGES)}")
    print(f"  packages missing   : {len(missing)}")
    print(f"  gpu problems       : {len(problems)}")

    if missing or problems:
        for p in problems:
            print(f"  FAIL: {p}")
        if missing:
            print(f"  FAIL: missing packages -> {', '.join(missing)}")
        print("  RESULT             : FAIL")
        return 1

    print("  RESULT             : OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
