"""E83（tech v5.11 §6.7，用户 D29 / U50）：AnthropicConverseClient 把 Converse 形状的 ConverseCall 翻成 Messages API 请求，
再把响应翻回 LlmTaskRunner 读的 Converse 形状；llm_tasks.py 与 LlmTaskRunner 不改。

按 tech v5.11 §6.7（E83）与 Claude Haiku 5.5 的接口事实（claude-api 技能）：
- 请求：model=ConverseCall.model_id；system 的 text 块 → {"type":"text"} 块；messages 的 text 块 → {"type":"text"} 块；
  toolSpec → tools[{name, description, input_schema}]；强制 toolChoice → tool_choice {"type":"tool","name",
  "disable_parallel_tool_use": true}；inferenceConfig.maxTokens → max_tokens（没给时 4096）；temperature、topP、topK 一律丢弃；
  每次 output_config={"effort":"low"}；不传 budget 式或 disabled 的 thinking。
- 响应：tool_use → {"toolUse": {toolUseId, name, input}}；text → {"text"}；thinking 块丢弃；stopReason 原样；usage 换成 Converse 键名。
- refusal（HTTP 200 的拒答）与 max_tokens 截断一律抛 AnthropicPlannerError，不当正常文本。
替身：FakeAnthropic 只实现 `messages.create(**kwargs)`，记录参数、按脚本返回 anthropic.types.Message 或抛异常；不出网。
"""

import json

import pytest
from anthropic.types import Message

from waitless import models as m
from waitless.bedrock import AnthropicConverseClient, AnthropicPlannerError, build_forced_tool_config
from waitless.llm_tasks import EXTRACT_REQUEST_TOOL, LlmTaskRunner
from waitless.models import ConverseCall

MODEL = "fixture-claude-model"
EXTRACTED = {
    "company_name": "Lakeshore Energy",
    "intent_text": "start power",
    "effective_date": "November 1",
    "service_address": "1200 W Example St, Apt 4B",
    "stated_preferences": [],
}
SAMPLING = ("temperature", "top_p", "top_k")


def message(content, *, stop_reason="end_turn", stop_details=None, usage=(11, 7)):
    data = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL, "content": list(content),
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}
    if stop_details is not None:
        data["stop_details"] = stop_details
    return Message.model_validate(data)


def text_block(text):
    return {"type": "text", "text": text}


def tool_block(name, tool_input, block_id="toolu_fixture_1"):
    return {"type": "tool_use", "id": block_id, "name": name, "input": dict(tool_input)}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-fixture"}


class FakeMessages:
    def __init__(self, entries):
        self.entries = list(entries)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.entries:
            raise AssertionError("FakeAnthropic: no scripted response left")
        entry = self.entries.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


class FakeAnthropic:
    """只有 messages.create 的 SDK 客户端替身（AnthropicConverseClient(client=...) 注入）。"""

    def __init__(self, *entries):
        self.messages = FakeMessages(entries)


def forced_call(**over):
    data = dict(task="extract_request", model_id=MODEL,
                system=[{"text": "system-marker-1"}, {"text": "system-marker-2"}],
                messages=[{"role": "user", "content": [{"text": "user-marker"}]}],
                tool_config=build_forced_tool_config("extract_request", "Extract the supplied data verbatim.", EXTRACT_REQUEST_TOOL))
    data.update(over)
    return ConverseCall(**data)


def text_call(**over):
    data = dict(task="phrase_reply", model_id=MODEL, system=[{"text": "skill-body-marker"}],
                messages=[{"role": "user", "content": [{"text": "{\"decision\": \"fixture\"}"}]}])
    data.update(over)
    return ConverseCall(**data)


def decision():
    return m.AssistantDecision(decision_id="dec-fixture", point="assistant_next_action", choice="present_task_card",
                               confidence=0.9, probabilities={"present_task_card": 0.9}, policy_rule="none")


# --- 请求翻译 -------------------------------------------------------------------


async def test_e83_forced_tool_request_is_translated_to_messages_api():
    fake = FakeAnthropic(message([tool_block("extract_request", EXTRACTED)], stop_reason="tool_use"))
    call = forced_call()

    await AnthropicConverseClient(client=fake).converse(call)

    [kw] = fake.messages.calls
    assert kw["model"] == MODEL
    assert kw["system"] == [{"type": "text", "text": "system-marker-1"}, {"type": "text", "text": "system-marker-2"}]
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "user-marker"}]}]
    spec = call.tool_config["tools"][0]["toolSpec"]
    assert kw["tools"] == [{"name": "extract_request", "description": spec["description"],
                            "input_schema": spec["inputSchema"]["json"]}]
    assert kw["tool_choice"]["type"] == "tool"
    assert kw["tool_choice"]["name"] == "extract_request"
    assert kw["tool_choice"].get("disable_parallel_tool_use") is True
    assert kw["output_config"] == {"effort": "low"}
    assert kw.get("thinking") in (None, {"type": "adaptive"})
    assert isinstance(kw["max_tokens"], int) and kw["max_tokens"] > 0


async def test_e83_text_request_has_no_tools_and_default_max_tokens():
    fake = FakeAnthropic(message([text_block("Here is your task card.")]))

    await AnthropicConverseClient(client=fake).converse(text_call())

    [kw] = fake.messages.calls
    assert "tools" not in kw and "tool_choice" not in kw
    assert kw["system"] == [{"type": "text", "text": "skill-body-marker"}]
    assert kw["max_tokens"] == 4096
    assert kw["output_config"] == {"effort": "low"}


async def test_e83_sampling_parameters_are_dropped_and_max_tokens_kept():
    fake = FakeAnthropic(message([text_block("ok")]))
    call = text_call(inference_config={"maxTokens": 321, "temperature": 0.0, "topP": 0.5, "topK": 20})

    await AnthropicConverseClient(client=fake).converse(call)

    [kw] = fake.messages.calls
    assert kw["max_tokens"] == 321
    sent = json.dumps({k: v for k, v in kw.items()}, default=str)
    for name in SAMPLING + ("topP", "topK", "inferenceConfig", "inference_config"):
        assert name not in kw, name
        assert f'"{name}"' not in sent, name
    for name in SAMPLING:
        assert name not in (kw.get("extra_body") or {}), name


# --- 响应翻译 -------------------------------------------------------------------


async def test_e83_tool_use_response_has_the_converse_shape():
    fake = FakeAnthropic(message([tool_block("extract_request", EXTRACTED, "toolu_fixture_9")], stop_reason="tool_use",
                                 usage=(120, 30)))

    out = await AnthropicConverseClient(client=fake).converse(forced_call())

    assert out["stopReason"] == "tool_use"
    assert out["output"]["message"]["role"] == "assistant"
    assert out["output"]["message"]["content"] == [
        {"toolUse": {"toolUseId": "toolu_fixture_9", "name": "extract_request", "input": EXTRACTED}}]
    assert out["usage"] == {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150}
    assert isinstance(out["metrics"]["latencyMs"], int)


async def test_e83_text_response_drops_thinking_blocks():
    fake = FakeAnthropic(message([THINKING, text_block("Here is your task card.")]))

    out = await AnthropicConverseClient(client=fake).converse(text_call())

    assert out["stopReason"] == "end_turn"
    assert out["output"]["message"]["content"] == [{"text": "Here is your task card."}]


async def test_e83_llm_task_runner_works_unchanged_on_the_anthropic_client():
    """LlmTaskRunner（不改）经 AnthropicConverseClient 走 L1（强制工具）与 L2（文字）。"""
    fake = FakeAnthropic(
        message([tool_block("extract_request", EXTRACTED)], stop_reason="tool_use"),
        message([THINKING, text_block("I will prepare your task card now.")]),
    )
    runner = LlmTaskRunner(AnthropicConverseClient(client=fake), model_id=MODEL)

    fields = await runner.extract_request("Start my power with Lakeshore Energy.")
    say = await runner.phrase_reply(skill_body="skill-body-marker", decision=decision(), tool_result=None,
                                    user_text="Start my power with Lakeshore Energy.")

    assert fields == m.ExtractedRequest(**EXTRACTED)
    assert say == "I will prepare your task card now."
    first, second = fake.messages.calls
    assert first["tool_choice"]["name"] == "extract_request"
    assert "tools" not in second
    assert all(c["model"] == MODEL for c in fake.messages.calls)


# --- refusal 与截断 ---------------------------------------------------------------


@pytest.mark.parametrize("content", [[], [text_block("Partial answer-marker")]], ids=["before-output", "mid-output"])
async def test_e83_refusal_raises_instead_of_returning_text(content):
    fake = FakeAnthropic(message(content, stop_reason="refusal",
                                 stop_details={"type": "refusal", "category": "cyber", "explanation": None}))

    with pytest.raises(AnthropicPlannerError) as err:
        await AnthropicConverseClient(client=fake).converse(text_call())

    assert err.value.kind == "refusal"
    assert "answer-marker" not in str(err.value)
    assert len(fake.messages.calls) == 1


@pytest.mark.parametrize("content,call", [
    ([text_block("Cut off answer-marker")], text_call()),
    ([tool_block("extract_request", {"company_name": "Lakeshore"})], forced_call()),
], ids=["text", "tool"])
async def test_e83_max_tokens_truncation_raises(content, call):
    fake = FakeAnthropic(message(content, stop_reason="max_tokens"))

    with pytest.raises(AnthropicPlannerError) as err:
        await AnthropicConverseClient(client=fake).converse(call)

    assert err.value.kind == "truncated"
    assert "answer-marker" not in str(err.value)
    assert len(fake.messages.calls) == 1


async def test_e83_runner_surfaces_a_refusal_as_an_error():
    fake = FakeAnthropic(message([], stop_reason="refusal", stop_details={"type": "refusal", "category": "general_harms",
                                                                         "explanation": None}))
    runner = LlmTaskRunner(AnthropicConverseClient(client=fake), model_id=MODEL)

    with pytest.raises(AnthropicPlannerError):
        await runner.phrase_reply(skill_body="skill-body-marker", decision=decision(), tool_result=None, user_text=None)
