"""Pre-translate the rule base into Marathi through the provider chain.

Run:  python -m src.translate_rules [--language mr] [--dry-run]

Uses the API provider chain (Sarvam first), never a local model. Writes the result
back into config/rules.yaml surgically so the provenance comments survive, and
produces outputs/marathi_review.md for side-by-side review.

Why this is not a plain "translate each string" loop
----------------------------------------------------
The first pass at this lost the `{crops}` placeholder from six of seven texts - the
translator treated it as a word - which would have shipped advisories that never
name the crop. So the placeholder is swapped for a sentinel that reads as an ordinary
noun phrase, translated, and swapped back; if it does not survive, the translation is
rejected rather than written.

Every candidate is also length-checked against the 300-character SMS limit using the
longest crop list any state carries, because a translation can be 15% longer than its
source and only fail on Uttar Pradesh's nine crops.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

from src.advisory import crop_phrase
from src.common import CONFIG_DIR, OUTPUTS_DIR, load_config
from src.llm.providers import build_chain
from src.report import DataError, summarize

RULES_PATH = CONFIG_DIR / "rules.yaml"
REVIEW_PATH = OUTPUTS_DIR / "marathi_review.md"

# Reads as a normal noun phrase, so the translator keeps it in a natural position
# instead of deleting it or transliterating a brace.
SENTINEL = "CROPNAMEHERE"
MAX_CHARS = 300


def protect(text: str) -> str:
    return " ".join(text.split()).replace("{crops}", SENTINEL)


def restore(text: str) -> str | None:
    """Put `{crops}` back, or return None if the sentinel did not survive."""
    pattern = re.compile(re.escape(SENTINEL), re.IGNORECASE)
    if not pattern.search(text):
        return None
    return pattern.sub("{crops}", text)


def longest_crop_list(cfg: dict) -> list[str]:
    return max(cfg["crops"].values(), key=len)


def _scalar(text: str) -> str:
    dumped = yaml.safe_dump(text, allow_unicode=True, default_flow_style=False,
                            width=10**9).strip()
    if dumped.endswith("\n..."):
        dumped = dumped[:-4].strip()
    return dumped


def splice(source: str, action_code: str, language: str, value: str) -> str:
    pattern = re.compile(
        rf"(^  {re.escape(action_code)}:\n(?:^(?:    |\s*$).*\n)*?)^    {language}: .*$",
        re.MULTILINE,
    )
    updated, count = pattern.subn(rf"\g<1>    {language}: {_scalar(value)}",
                                  source, count=1)
    if count != 1:
        raise DataError(f"could not find `{language}:` inside action {action_code}")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", default="mr")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true",
                        help="retranslate actions that already have text")
    args = parser.parse_args()

    rules = load_config("rules")
    llm_cfg = load_config("llm")
    chain = build_chain(llm_cfg)

    usable = [p.name for p in chain.providers if p.available()]
    print("--- translate_rules ---")
    print(f"  language  : {args.language}")
    print(f"  providers : {llm_cfg['fallback_order']}; usable now: {usable}")
    if usable == ["template"]:
        raise DataError(
            "only the template provider is available, and templates cannot "
            "translate. Set SARVAM_API_KEY (or GEMINI_API_KEY) in .env and re-run."
        )

    crops = longest_crop_list(rules)
    rows: list[tuple[str, str, str, str]] = []
    accepted: dict[str, str] = {}
    rejected: list[str] = []

    for code, action in rules["actions"].items():
        if action.get(args.language) and not args.overwrite:
            rows.append((code, protect(action["en"]), action[args.language], "kept"))
            continue

        english = protect(action["en"])
        result = chain.translate(
            english, args.language, source_language="en",
            mode=llm_cfg["providers"]["sarvam"]["default_mode"],
        )
        restored = restore(result.text)
        if restored is None and "{crops}" in action["en"]:
            rejected.append(f"{code}: placeholder lost in translation")
            rows.append((code, english, result.text, "REJECTED: placeholder lost"))
            continue

        candidate = restored if restored is not None else result.text
        rendered = candidate.replace(
            "{crops}", crop_phrase(crops, args.language, rules)
        )
        if len(rendered) > MAX_CHARS:
            rejected.append(f"{code}: {len(rendered)} chars over the {MAX_CHARS} limit")
            rows.append((code, english, candidate,
                         f"REJECTED: {len(rendered)} chars"))
            continue

        accepted[code] = candidate
        rows.append((code, english, candidate, f"ok ({len(rendered)} chars)"))
        print(f"  {code}: ok ({len(rendered)} chars, via {result.provider})")

    # --- review file ------------------------------------------------------
    lines = [
        f"# {args.language} translations for review", "",
        f"Produced through the API provider chain ({', '.join(usable)}), translated "
        "from the **English** source.", "",
        "`CROPNAMEHERE` is the protected `{crops}` placeholder. A translation that "
        "loses it is rejected, not written: without it the message never names the "
        "crop.", "",
        "| action | English (protected) | translation | status |",
        "|---|---|---|---|",
    ]
    for code, english, translated, status in rows:
        lines.append(f"| `{code}` | {english.replace('|', '/')} | "
                     f"{translated.replace('|', '/')} | {status} |")
    REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if rejected:
        print(f"\n  {len(rejected)} rejected:")
        for line in rejected:
            print(f"      - {line}")

    if args.dry_run:
        print(f"\n  dry run: {REVIEW_PATH} written, rules.yaml untouched")
        return 0

    if accepted:
        source = RULES_PATH.read_text(encoding="utf-8")
        for code, value in accepted.items():
            source = splice(source, code, args.language, value)
        RULES_PATH.write_text(source, encoding="utf-8")

    summarize(
        "translate_rules",
        rows=len(rows),
        files=[RULES_PATH, REVIEW_PATH],
        extra={
            "language": args.language,
            "written": len(accepted),
            "rejected": len(rejected),
            "providers used": usable,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
