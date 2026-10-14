import json

from pydantic import ConfigDict

from a_support import AEnv, LAKESHORE_REQUEST, PROFILE_VALUES, default_prepare_gate
from waitless.models import Profile, UPDATABLE_FIELD_KEYS


class DecoyProfile(Profile):
    model_config = ConfigDict(extra="allow")


async def test_prepare_and_update_share_only_whitelisted_company_fields(tmp_path, monkeypatch):
    env = AEnv(tmp_path)
    try:
        env.store.save_profile(PROFILE_VALUES, company_id="lakeshore-energy")
        env.store.save_profile({"account_number": "unrelated-company-account"}, company_id="other-company")
        original_get_profile = env.store.get_profile
        decoys = {
            "ssn": "decoy-sensitive-ssn",
            "date_of_birth": "decoy-sensitive-birth",
            "card_number": "decoy-sensitive-card",
            "email": "decoy-private-email",
            "zip": "decoy-private-zip",
            "account_number": "decoy-unscoped-account",
            "unexpected_field": "decoy-extra-field",
        }
        monkeypatch.setattr(env.store, "get_profile", lambda: DecoyProfile.model_validate({
            **original_get_profile().model_dump(), **decoys,
        }))
        gate = default_prepare_gate()
        tasks = env.task_service(gate=gate)
        prepared = await tasks.prepare(LAKESHORE_REQUEST)
        updated = await tasks.update(prepared.task_id, decisions={"deposit_or_autopay": "autopay"})

        assert prepared.status == updated.status == "ready"
        for card in (prepared.task_card, updated.task_card):
            shared = {f.key: f.display for f in card.shared_fields}
            assert set(shared) == set(UPDATABLE_FIELD_KEYS)
            assert shared["account_number"] == PROFILE_VALUES["account_number"]
            for value in (*decoys.values(), "unrelated-company-account"):
                assert value not in card.model_dump_json()
        assert updated.task_card.card_version == prepared.task_card.card_version + 1
        assert next(q.selected for q in updated.task_card.precall_decisions
                    if q.question_id == "deposit_or_autopay") == "autopay"
        assert {point for point, _ in gate.requests} == {"company_skill", "task_intent", "precall_default"}
        for _, request in gate.requests:
            assert set(request.context.allowed_fields) == {"name", "phone_number", "address"}
            for value in (*decoys.values(), "unrelated-company-account"):
                assert value not in json.dumps(request.model_dump(mode="json"))
        assert all(value not in env.store.get_task(prepared.task_id)["task_card_json"] for value in decoys.values())
    finally:
        env.close()
