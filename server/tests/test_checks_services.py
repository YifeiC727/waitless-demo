"""AC-31：代码里 boto3 客户端的服务名只有 bedrock-runtime 和 polly；依赖里没有 twilio、Connect 或云存储 SDK。

验证方式是 `scripts/checks.sh services`（第 3 步写检查体，见 X 组）；这里在仓库根目录跑它，要求退出码 0，
从而守住 A 线代码（bedrock.py 等）只用 bedrock-runtime。检查脚本本身对违规样例的判定由 checks.sh 的测试负责。
"""

import shutil
import subprocess

import pytest


def test_ac31_services_check_passes_on_repo(repo_root):
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to run scripts/checks.sh")
    script = repo_root / "scripts" / "checks.sh"
    assert script.is_file(), f"missing {script}"
    proc = subprocess.run([bash, str(script), "services"], cwd=repo_root, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"checks.sh services exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
