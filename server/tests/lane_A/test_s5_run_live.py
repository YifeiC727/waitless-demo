import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

from a_support import LOOPBACK


@pytest.mark.parametrize("company_exit", [None, 2])
def test_live_launcher_starts_company_and_cleans_it_up(tmp_path, unused_tcp_port_factory, company_exit):
    root = tmp_path / "launcher"
    (root / "scripts").mkdir(parents=True)
    (root / "server").mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[3] / "scripts/run.sh", root / "scripts/run.sh")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log = root / "commands.jsonl"
    fake = bin_dir / "uv"
    fake.write_text("#!/usr/bin/env python3\n" + textwrap.dedent('''
        import json, os, signal, socket, sys, time
        from pathlib import Path
        log = Path(os.environ["LAUNCH_TEST_LOG"])
        def record(event):
            with log.open("a") as file:
                file.write(json.dumps(event) + "\\n")
        command = sys.argv[-1]
        record({"command": command, "pid": os.getpid()})
        if command == "company":
            if os.environ.get("LAUNCH_TEST_EXIT"):
                sys.exit(int(os.environ["LAUNCH_TEST_EXIT"]))
            def stop(signum, frame):
                record({"stopped": os.getpid()})
                sys.exit(0)
            signal.signal(signal.SIGTERM, stop)
            listener = socket.socket()
            listener.bind((os.environ["WAITLESS_COMPANY_HOST"], int(os.environ["WAITLESS_COMPANY_PORT"])))
            listener.listen()
            while True:
                conn, _ = listener.accept()
                conn.close()
        sys.exit(0)
    '''))
    fake.chmod(0o755)
    env = {"PATH": str(bin_dir) + os.pathsep + os.defpath, "WAITLESS_ENV_LOADED": "1",
           "LAUNCH_TEST_LOG": str(log), "WAITLESS_HOST": LOOPBACK,
           "WAITLESS_PORT": str(unused_tcp_port_factory()), "WAITLESS_COMPANY_HOST": LOOPBACK,
           "WAITLESS_COMPANY_PORT": str(unused_tcp_port_factory())}
    if company_exit is not None:
        env["LAUNCH_TEST_EXIT"] = str(company_exit)
    process = subprocess.Popen(["bash", str(root / "scripts/run.sh"), "live-sim"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    finally:
        # Test failures must not leave launcher children behind.
        try:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait()
    events = [json.loads(line) for line in log.read_text().splitlines()]
    commands = [e["command"] for e in events if "command" in e]
    if company_exit is not None:
        assert commands == ["company"]
        assert process.returncode == company_exit
    else:
        assert commands == ["company", "serve"]
        assert process.returncode == 0, stderr
        company_pid = events[0]["pid"]
        assert {"stopped": company_pid} in events
        with pytest.raises(ProcessLookupError):
            os.kill(company_pid, 0)
        assert env["WAITLESS_HOST"] in stdout and env["WAITLESS_PORT"] in stdout
