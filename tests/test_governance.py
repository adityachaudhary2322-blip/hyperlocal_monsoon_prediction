"""The rule from the spec: text from Sarvam or Gemini ALWAYS needs human approval.

That rule lives in two halves - config/alert_policy.yaml holds LLM text for review,
and src/pipeline/run.py has to report the source truthfully for the policy to fire.
The second half is the fragile one: the pipeline wrote a literal "template" for a
while, so LLM text would have been auto-approved and sent while the policy that was
meant to stop it sat there matching nothing. These tests cover both halves.
"""

from __future__ import annotations

import ast

import pytest

from src.common import CONFIG_DIR, load_config
from src.pipeline.run import classify

RUN_PY = CONFIG_DIR.parent / "src" / "pipeline" / "run.py"

BASE = {"risk_level": "amber", "confidence": "medium", "hazard": "dry",
        "horizon": 14, "action": "IRRIGATE_MULCH"}


def test_llm_text_is_never_auto_approved():
    policy = load_config("alert_policy")
    status, reason, _ = classify({**BASE, "source": "llm"}, policy)
    assert status == "pending_approval"
    assert "language model" in reason


def test_template_text_at_amber_can_be_auto_approved():
    """The counterpart: the hold has to be about the source, not about everything."""
    policy = load_config("alert_policy")
    status, _, _ = classify({**BASE, "source": "template"}, policy)
    assert status == "approved"


def _advisory_row_call(tree: ast.AST) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "AdvisoryRow"):
            return node
    pytest.fail("no AdvisoryRow(...) call found in src/pipeline/run.py")


def test_pipeline_does_not_hardcode_the_source_label():
    """A literal here silently defeats test_llm_text_is_never_auto_approved."""
    call = _advisory_row_call(ast.parse(RUN_PY.read_text(encoding="utf-8")))
    source = next((k.value for k in call.keywords if k.arg == "source"), None)
    assert source is not None, "AdvisoryRow is built without a source"
    assert not isinstance(source, ast.Constant), (
        "src/pipeline/run.py hardcodes the advisory source; it must pass through "
        "what the provider chain actually returned, or LLM text gets labelled "
        "template and skips the llm_text hold rule"
    )


def test_source_is_decided_before_the_approval_check():
    """classify() must see the real source, so text generation comes first."""
    body = RUN_PY.read_text(encoding="utf-8")
    assert body.index('context["source"]') < body.index("status, reason, urgent ="), (
        "the source is recorded after classify() runs, so the policy decides on a "
        "source that is not yet known"
    )
