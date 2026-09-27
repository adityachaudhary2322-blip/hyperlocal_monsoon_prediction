"""Push trained artifacts to the private Hugging Face model repo the Space reads.

Run:  python scripts/upload_models.py [--chronos] [--dry-run]

Pairs with `src/artifacts.py`, which downloads exactly what this uploads. The repo is
named by HF_MODEL_REPO and written with HF_TOKEN (a token with **write** scope - the
read token the Space uses is not enough).

The repo is created **private** if it does not exist. That is the default rather than an
option because these are forecast models for named districts, and a public repo is a
decision someone should make deliberately, not inherit from a script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.artifacts import CHRONOS_PATTERN, TABLE_PATH, repo_id, token  # noqa: E402
from src.report import DataError  # noqa: E402

# Uploaded at the same repo-relative paths src/artifacts.py expects to find them.
LGBM_DIR = ROOT / "models" / "lgbm"
TABLE = ROOT / TABLE_PATH
CHRONOS_DIR = ROOT / "models" / "chronos"


def collect(chronos: bool) -> list[Path]:
    files = sorted(LGBM_DIR.glob("*"))
    if not files:
        raise DataError(
            f"no model files in {LGBM_DIR}; run python -m src.train_baselines first")
    if not TABLE.is_file():
        raise DataError(
            f"missing {TABLE}; run python -m src.build_features first")
    files.append(TABLE)
    if chronos:
        chronos_files = [p for p in CHRONOS_DIR.rglob("*") if p.is_file()]
        if not chronos_files:
            raise DataError(f"--chronos given but {CHRONOS_DIR} is empty")
        files.extend(sorted(chronos_files))
    return [p for p in files if p.is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chronos", action="store_true",
                        help=f"also upload {CHRONOS_PATTERN} (~25 MB)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be uploaded and stop")
    parser.add_argument("--repo", default=None,
                        help="override HF_MODEL_REPO, e.g. user/monsoon-models")
    args = parser.parse_args()

    print("--- upload_models ---")
    repo = args.repo or repo_id()
    if not repo:
        raise DataError(
            "HF_MODEL_REPO is not set. Add it to .env (e.g. "
            "HF_MODEL_REPO=your-user/monsoon-models) or pass --repo.")

    files = collect(args.chronos)
    total = sum(p.stat().st_size for p in files)
    print(f"  repo : {repo}")
    print(f"  token: {'set' if token() else 'MISSING'}")
    print(f"  files: {len(files)}  ({total / 1e6:.1f} MB)")
    for path in files[:6]:
        print(f"    {path.relative_to(ROOT).as_posix()}  "
              f"{path.stat().st_size / 1e3:.0f} kB")
    if len(files) > 6:
        print(f"    ... and {len(files) - 6} more")

    if args.dry_run:
        print("  dry run, nothing uploaded")
        return 0

    if not token():
        raise DataError(
            "HF_TOKEN is not set, and uploading needs a token with write scope. "
            "Create one at https://huggingface.co/settings/tokens")

    from huggingface_hub import HfApi

    api = HfApi(token=token())
    api.create_repo(repo_id=repo, repo_type="model", private=True, exist_ok=True)
    print(f"  repo ready (private)")

    patterns = ["models/lgbm/*", TABLE_PATH]
    if args.chronos:
        patterns.append(CHRONOS_PATTERN)

    api.upload_folder(
        repo_id=repo,
        repo_type="model",
        folder_path=str(ROOT),
        allow_patterns=patterns,
        commit_message=f"Upload {len(files)} artifacts for the Space",
    )
    print(f"  uploaded {len(files)} files to {repo}")
    print("  the Space needs HF_MODEL_REPO and a READ token as HF_TOKEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
