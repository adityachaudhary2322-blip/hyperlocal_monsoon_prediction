"""Fetch trained model artifacts from a private Hugging Face repo at startup.

Why the models are not in git: `models/lgbm` is ~2.8 MB of boosters and calibrators and
`train_table.parquet` is ~2.7 MB, they are rebuilt whenever the pipeline is retrained,
and a Space image should not be rebuilt to ship a new model. They live in a private
model repo instead, named by **HF_MODEL_REPO** and read with **HF_TOKEN**.

The artifact set is deliberately wider than "the models". A forecast run needs the
feature table too - `src.pipeline.run` predicts from the row for the requested start
date - so shipping only the boosters produces a Space whose "Run forecast" button
raises `missing train_table.parquet`. Both are listed in `ARTIFACTS`.

Locally this is a no-op: the files are already on disk from training, and nothing is
downloaded or overwritten unless `--force`. That is what keeps one code path for both
environments.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from src.common import DATA_PROCESSED, MODELS_DIR, ROOT
from src.report import DataError, summarize

# Files the app cannot run without, as repo-relative paths. The repo mirrors this
# layout, so a snapshot download lands them exactly where the code already looks.
LGBM_PATTERN = "models/lgbm/*"
TABLE_PATH = "data/processed/train_table.parquet"
ARTIFACTS = (LGBM_PATTERN, TABLE_PATH)

# Chronos-2's fine-tuned predictor is ~25 MB and only used when Chronos is enabled,
# so it is fetched on request rather than at every cold start.
CHRONOS_PATTERN = "models/chronos/*"


@dataclass(frozen=True)
class ArtifactStatus:
    present: bool
    boosters: int
    table: bool
    detail: str


def repo_id() -> str | None:
    from src.runtime import env
    return (env("HF_MODEL_REPO", "") or "").strip() or None


def token() -> str | None:
    from src.runtime import env
    return (env("HF_TOKEN", "") or "").strip() or None


def status() -> ArtifactStatus:
    """What is on local disk right now. Never touches the network."""
    boosters = sorted((MODELS_DIR / "lgbm").glob("*.txt"))
    table = (DATA_PROCESSED / "train_table.parquet").is_file()
    # 3 hazards x 4 horizons. Fewer means a partial download, which would fail
    # mid-run rather than at startup.
    complete = len(boosters) == 12 and table
    if complete:
        detail = f"{len(boosters)} boosters and the feature table are on disk"
    else:
        missing = []
        if len(boosters) != 12:
            missing.append(f"{len(boosters)}/12 boosters")
        if not table:
            missing.append("train_table.parquet")
        detail = "missing " + ", ".join(missing)
    return ArtifactStatus(complete, len(boosters), table, detail)


def ensure_artifacts(*, force: bool = False, chronos: bool = False,
                     quiet: bool = False) -> ArtifactStatus:
    """Download the artifacts if they are not already here. Returns the new status.

    Called once at app startup. A missing HF_MODEL_REPO is not an error - a local
    checkout has the files already - but a missing repo *and* missing files is, and
    the message says which of the two to fix.
    """
    current = status()
    patterns = list(ARTIFACTS) + ([CHRONOS_PATTERN] if chronos else [])

    if current.present and not force and not chronos:
        if not quiet:
            print(f"  artifacts: {current.detail}, nothing to download")
        return current

    repo = repo_id()
    if repo is None:
        if current.present:
            return current
        raise DataError(
            f"model artifacts are {current.detail} and HF_MODEL_REPO is not set. "
            "Either train locally (python -m src.train_baselines) or set "
            "HF_MODEL_REPO and HF_TOKEN to a repo holding models/lgbm/ and "
            f"{TABLE_PATH} - see scripts/upload_models.py."
        )

    from huggingface_hub import snapshot_download

    if not quiet:
        print(f"  artifacts: downloading {', '.join(patterns)} from {repo}")

    snapshot_download(
        repo_id=repo,
        repo_type="model",
        token=token(),
        local_dir=str(ROOT),
        allow_patterns=patterns,
    )

    after = status()
    if not after.present:
        raise DataError(
            f"downloaded from {repo} but the artifacts are still {after.detail}. "
            "Check the repo layout: it must contain models/lgbm/*.txt and "
            f"{TABLE_PATH} at those exact paths."
        )
    if not quiet:
        print(f"  artifacts: {after.detail}")
    return after


def cache_dir() -> Path:
    """Where huggingface_hub will write. Set HF_HOME to keep it off a read-only fs."""
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="download even if the files are already present")
    parser.add_argument("--chronos", action="store_true",
                        help="also fetch the fine-tuned Chronos-2 predictor")
    args = parser.parse_args()

    print("--- artifacts ---")
    repo = repo_id()
    print(f"  HF_MODEL_REPO: {repo or 'not set'}")
    print(f"  HF_TOKEN     : {'set' if token() else 'missing'}")
    print(f"  cache        : {cache_dir()}")

    result = ensure_artifacts(force=args.force, chronos=args.chronos)
    summarize(
        "artifacts",
        rows=result.boosters,
        files=[],
        extra={
            "boosters": f"{result.boosters}/12",
            "feature table": "present" if result.table else "MISSING",
            "source": repo or "local disk",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
