"""Agent layer: output validation, availability honesty, and prompt grounding.

No network. Model output is treated here as what it is -- untrusted input -- so these
tests are mostly about what happens when it is wrong.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services.agent.client import (
    RETRYABLE_CLIENT_STATUSES,
    agent_availability,
    parse_json_payload,
)
from app.services.agent.explain import validate_batch_response
from app.services.agent.prompts import (
    SYSTEM_INSTRUCTION,
    DiscrepancyPromptItem,
    build_batch_prompt,
)
from app.services.agent.schema import (
    MAX_ACTION_CHARS,
    MAX_EXPLANATION_CHARS,
    AgentExplanation,
)

ID_A = "11111111-1111-1111-1111-111111111111"
ID_B = "22222222-2222-2222-2222-222222222222"


def explanation(discrepancy_id: str, **overrides) -> dict:
    base = {
        "discrepancy_id": discrepancy_id,
        "explanation": "The bank credited 2360 paise less than the ledger expected.",
        "corrective_action": "Confirm the fee schedule with the processor.",
        "model_confidence": "high",
    }
    return {**base, **overrides}


# --- availability ---------------------------------------------------------------------


def test_missing_key_reports_unavailable_with_a_reason():
    """Silence is the failure mode to avoid: a blank explanation looks like 'nothing wrong'."""
    available, reason = agent_availability(Settings(gemini_api_key=None))
    assert available is False
    assert "GEMINI_API_KEY is not set" in reason
    assert "unaffected" in reason  # says what still works, not only what does not


def test_blank_key_is_treated_as_absent():
    available, _ = agent_availability(Settings(gemini_api_key="   "))
    assert available is False


def test_present_key_reports_available():
    available, _ = agent_availability(Settings(gemini_api_key="test-key"))
    assert available is True


def test_rate_limit_statuses_are_retryable_but_other_4xx_are_not():
    """429 means 'too fast'. 400 and 404 mean 'wrong', and retrying them wastes the demo."""
    assert 429 in RETRYABLE_CLIENT_STATUSES
    assert 408 in RETRYABLE_CLIENT_STATUSES
    assert 400 not in RETRYABLE_CLIENT_STATUSES
    assert 404 not in RETRYABLE_CLIENT_STATUSES
    assert 403 not in RETRYABLE_CLIENT_STATUSES


# --- response validation --------------------------------------------------------------


def test_a_well_formed_batch_is_accepted():
    payload = {"explanations": [explanation(ID_A), explanation(ID_B)]}
    accepted, problems = validate_batch_response(payload, {ID_A, ID_B})
    assert set(accepted) == {ID_A, ID_B}
    assert problems == []


def test_an_invented_id_is_dropped_and_reported():
    """Attaching an explanation to the wrong transaction is worse than attaching none."""
    payload = {"explanations": [explanation(ID_A), explanation("not-a-real-id")]}
    accepted, problems = validate_batch_response(payload, {ID_A})
    assert set(accepted) == {ID_A}
    assert any("unknown id" in p for p in problems)


def test_a_duplicate_id_keeps_the_first_and_reports_it():
    payload = {
        "explanations": [
            explanation(ID_A, explanation="First answer."),
            explanation(ID_A, explanation="Second, different answer."),
        ]
    }
    accepted, problems = validate_batch_response(payload, {ID_A})
    assert accepted[ID_A].explanation == "First answer."
    assert any("duplicate id" in p for p in problems)


def test_a_missing_id_is_reported_as_unexplained():
    """Counted, not quietly absent: the unexplained tally must stay truthful."""
    payload = {"explanations": [explanation(ID_A)]}
    accepted, problems = validate_batch_response(payload, {ID_A, ID_B})
    assert set(accepted) == {ID_A}
    assert any(ID_B in p and "no explanation" in p for p in problems)


def test_a_structurally_invalid_response_accepts_nothing():
    payload = {"explanations": [{"discrepancy_id": ID_A}]}  # missing required fields
    accepted, problems = validate_batch_response(payload, {ID_A})
    assert accepted == {}
    assert any("schema validation" in p for p in problems)


def test_a_response_missing_the_array_accepts_nothing():
    accepted, problems = validate_batch_response({"result": "ok"}, {ID_A})
    assert accepted == {}
    assert problems


def test_an_empty_batch_response_reports_every_id_as_unexplained():
    accepted, problems = validate_batch_response({"explanations": []}, {ID_A, ID_B})
    assert accepted == {}
    assert len(problems) == 2


@pytest.mark.parametrize("field", ["explanation", "corrective_action"])
def test_blank_text_is_rejected(field):
    """A blank explanation renders as 'nothing to see here', which is a lie."""
    with pytest.raises(ValueError, match="must not be blank"):
        AgentExplanation.model_validate(explanation(ID_A, **{field: "   "}))


def test_an_overlong_explanation_is_rejected():
    """Review queues get skimmed, and a skimmed explanation is worse than a short one."""
    with pytest.raises(ValueError, match="exceeds"):
        AgentExplanation.model_validate(
            explanation(ID_A, explanation="x" * (MAX_EXPLANATION_CHARS + 1))
        )


def test_an_overlong_corrective_action_is_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        AgentExplanation.model_validate(
            explanation(ID_A, corrective_action="x" * (MAX_ACTION_CHARS + 1))
        )


def test_an_unknown_confidence_value_is_rejected():
    with pytest.raises(ValueError):
        AgentExplanation.model_validate(explanation(ID_A, model_confidence="very-high"))


# --- payload parsing ------------------------------------------------------------------


def test_plain_json_parses():
    assert parse_json_payload('{"explanations": []}') == {"explanations": []}


def test_a_fenced_code_block_is_unwrapped():
    """A known, bounded model quirk. Anything beyond it is a real failure and raises."""
    assert parse_json_payload('```json\n{"explanations": []}\n```') == {"explanations": []}


def test_non_json_raises_rather_than_being_salvaged():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_json_payload("I could not complete that request.")


def test_a_json_array_is_rejected_because_the_contract_is_an_object():
    with pytest.raises(ValueError, match="expected a JSON object"):
        parse_json_payload("[1, 2, 3]")


# --- prompt grounding -----------------------------------------------------------------


def prompt_item(discrepancy_id: str = ID_A) -> DiscrepancyPromptItem:
    return DiscrepancyPromptItem(
        discrepancy_id=discrepancy_id,
        rule_id="R06_mdr_fee_variance",
        summary="processor deducted 3540 where the ledger rate implies 2360",
        evidence={"psp_fee_minor": 3540, "expected_fee_minor": 2360, "variance_minor": 1180},
        delta_minor=-1180,
        match_key="ord_00042",
    )


def test_the_prompt_contains_every_requested_id():
    items = [prompt_item(ID_A), prompt_item(ID_B)]
    prompt = build_batch_prompt(
        category_code="MDR_FEE_VARIANCE",
        category_display_name="MDR fee variance",
        category_description="Fee differs from the contracted rate.",
        items=items,
    )
    assert ID_A in prompt
    assert ID_B in prompt
    assert "exactly 2 entries" in prompt


def test_the_prompt_carries_the_stored_evidence_verbatim():
    """Grounding is structural: the model can only restate what the engine recorded."""
    prompt = build_batch_prompt(
        category_code="MDR_FEE_VARIANCE",
        category_display_name="MDR fee variance",
        category_description="Fee differs from the contracted rate.",
        items=[prompt_item()],
    )
    assert "3540" in prompt
    assert "2360" in prompt
    assert "R06_mdr_fee_variance" in prompt
    assert "ord_00042" in prompt


def test_the_prompt_payload_exposes_nothing_beyond_the_stored_finding():
    """If a field is not in the engine's record, the model never sees it."""
    payload = prompt_item().to_payload()
    assert set(payload) == {
        "discrepancy_id",
        "reference",
        "detected_by_rule",
        "rule_summary",
        "amount_delta_minor",
        "currency",
        "evidence",
    }


def test_the_system_instruction_forbids_invention_and_asserts_advisory_status():
    assert "Never invent" in SYSTEM_INSTRUCTION
    assert "ADVISORY" in SYSTEM_INSTRUCTION
    assert "Do not invent ids" in SYSTEM_INSTRUCTION
