"""Offline holder scenario and Polly fixtures for H3 checks."""

import shutil
from unittest.mock import Mock

import yaml

from waitless import cli
from waitless.company import ivr_audio

HOLDER_SAY = "This is the account holder. I confirm my date of birth on file."


def holder_company(tmp_path, company_dir, monkeypatch):
    copied = tmp_path / "holder-company"
    shutil.copytree(company_dir, copied)
    path = copied / "lakeshore-energy" / "scenarios" / "baseline.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["steps"]["rep"]["scripted"] = {
        "reply_end_silence_ms": 1500, "polly_voice": "Matthew",
        "holder_prompt_ms": 3000, "holder_max_asks": 4, "resume_wait_ms": 8000,
        "holder_reply": {"say": HOLDER_SAY, "polly_voice": "Salli"},
    }
    path.write_text(yaml.safe_dump(raw))
    polly = object()
    monkeypatch.setattr(cli, "_create_holder_polly_client", Mock(return_value=polly), raising=False)
    synth = Mock(return_value=b"\x01\x02" * 1500)
    monkeypatch.setattr(ivr_audio, "synthesize_prompt", synth)
    return copied, path, synth, polly


def two_question_company(tmp_path, company_dir, monkeypatch):
    copied, path, synth, polly = holder_company(tmp_path, company_dir, monkeypatch)
    raw = yaml.safe_load(path.read_text())
    rep = raw["steps"]["rep"]
    rep["scripted"]["holder_reply"]["then_keys"] = "1"
    rep["actions"].insert(2, {
        "do": "require_account_holder", "verification": "deposit_consent",
        "line": "A refundable deposit is required. Press 1 to agree or 2 to decline.",
        "keypad_choice": {"1": "agree", "2": "decline"},
        "choice_lines": {"agree": "Thank you, the deposit is noted.",
                         "decline": "Understood, no deposit has been approved."},
    })
    path.write_text(yaml.safe_dump(raw))
    return copied, path, synth, polly
