"""模拟运营商：拨号 API、场景切换、媒体中继（B 线；tech §6.3；AC-83、AC-84、AC-98）。

公开接口（§7）：carrier 路由（§6.3 表）、CarrierCall。只监听回环（公司进程的 host/port 来自 Settings）。
"""

from typing import Any

from waitless.models import CarrierDialIn, CarrierDialOut, CarrierHangupOut, CompanyConfig, ScenarioSelection

__all__ = ["CarrierCall", "CarrierState", "build_carrier_router"]


class CarrierCall:
    """一通模拟电话：连引擎的 /ws/line（stream_url?token=stream_token），把媒体在引擎与公司方之间中继。"""

    def __init__(self, *, dial: CarrierDialIn, call_sid: str, scenario_id: str, company: CompanyConfig) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        """发 connected、start（customParameters={call_id, task_id}）、20 ms μ-law media；收引擎的 media/mark/clear
        并按 line_protocol 校验；DTMF 用 company.dtmf_detect.GoertzelDetector 识别后交场景引擎。
        帧一律用 call/line_protocol.py 的现成函数构造与解析（第 3 步已写好，A-23）：make_connected、make_start、
        make_media_in、make_stop、encode_line_message；收帧 parse_line_message(text, direction="to_carrier")。
        μ-law 与 PCM16 互转用 audio/codec.py 的 ulaw_encode、ulaw_decode（第 3 步已写好，A-21）。"""
        raise NotImplementedError

    async def hangup(self) -> None:
        """发 stop 并关闭连接（菜单两次超时、坐席结束或引擎挂断，AC-83）。
        结束时若 settings.company_stats_path 有值，往该文件追加一行 JSON
        {call_sid, nonsilent_inbound_frames_during_handoff}（缺口 X-G6，定稿 stats_jsonl，暂定）。
        收到引擎的 mark 只记录、不回显（缺口 B-G-10，定稿 no_echo；D 不得依赖 mark 回显）。"""
        raise NotImplementedError

    @property
    def nonsilent_inbound_frames_during_handoff(self) -> int:
        """AC-98 真机检查单用：转接期间收到的用户非静音音频帧数（只计数，不存音频）。"""
        raise NotImplementedError


class CarrierState:
    """号码 → 线路、线路 → 当前场景、call_sid → CarrierCall。"""

    def __init__(self, companies: list[CompanyConfig]) -> None:
        raise NotImplementedError

    def set_scenario(self, sel: ScenarioSelection) -> ScenarioSelection:
        """写入后下一通电话用它（R26、AC-84）。
        缺口 B-G-3（定稿 reject_4xx）：线路未知 → 404 {"code":"unknown_line"}；scenario_id 不在该线路所属公司的
        list_scenarios(company_dir, company_id) 里 → 404 {"code":"unknown_scenario"}；两种情况当前选择都不变。
        CarrierState 怎样拿到 company_dir 由 B 线自定（缺口 B-G-1，lane_internal）。"""
        raise NotImplementedError

    def get_scenario(self, line_number: str) -> ScenarioSelection:
        raise NotImplementedError

    async def place_call(self, dial: CarrierDialIn) -> CarrierDialOut:
        """号码未知 → 404 {"code":"unknown_number"}；否则建 CarrierCall 并在后台连接引擎，立即返回 queued。"""
        raise NotImplementedError

    async def hangup(self, call_sid: str) -> CarrierHangupOut:
        """call_sid 未知 → 404 {"code":"unknown_call"}（缺口 B-G-4，定稿 as_fake_carrier）。"""
        raise NotImplementedError


def build_carrier_router(state: CarrierState) -> Any:
    """FastAPI APIRouter：POST /carrier/calls、POST /carrier/calls/{call_sid}/hangup、PUT/GET /sim/scenario、
    WS /ws/company-bot?role=agent|rep&call_sid=（运营商把媒体中继到公司方的 Sonic bot）。
    状态码与错误码见 models.CARRIER_ERROR_CODES 上方的说明（缺口 B-G-3、B-G-4）：成功一律 200，
    与共享的 tests/fakes/carrier.FakeCarrier 一致。"""
    raise NotImplementedError
