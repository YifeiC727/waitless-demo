"""模拟公司进程（B 线；tech §5.4、§6.3）。`waitless company` 启动它，只监听回环地址。

公开接口：create_company_app(settings)、run_company(settings)（cli.py 调用）。
"""

from typing import Any

from waitless.config import Settings

__all__ = ["create_company_app", "run_company"]


def create_company_app(settings: Settings) -> Any:
    """FastAPI：挂 company.carrier.build_carrier_router；公司配置来自 settings.company_dir。
    settings.company_stats_path 有值时，CarrierCall 每通结束往它追加统计行（缺口 X-G6，暂定）。"""
    raise NotImplementedError


def run_company(settings: Settings) -> int:
    """uvicorn.run(host=settings.company_host, port=settings.company_port)；host 不是回环地址时拒绝启动（K18）。
    歧义表 A-13（定稿 company_cmd_only_root）：company_host 或 company_port 缺失时，在 stderr 列出缺的变量名
    （config.REQUIRED_FOR_COMPANY）并返回退出码 2，不启动；`waitless serve` 不检查这两个变量。"""
    raise NotImplementedError
