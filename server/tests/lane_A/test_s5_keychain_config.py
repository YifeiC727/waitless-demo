import os
from pathlib import Path
import shutil
import subprocess


def test_keychain_uses_documented_item_names_from_env_file(tmp_path):
    root = tmp_path / "launcher"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[3] / "scripts/load-keys-macos.sh",
                    root / "scripts/load-keys-macos.sh")
    (root / ".env").write_text("WAITLESS_KEYCHAIN_AWS_ID_ITEM=fixture-id-item\n"
                               "WAITLESS_KEYCHAIN_AWS_SECRET_ITEM=fixture-secret-item\n"
                               "WAITLESS_KEYCHAIN_JEV_ITEM=fixture-jev-item\n")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log = root / "query.txt"
    for name, body in {
        "uname": "printf 'Darwin\\n'\n",
        # Refuse lookup: only record item names, never return credential data.
        "security": 'printf "%s\\n" "$@" > "$KEYCHAIN_TEST_LOG"\nexit 97\n',
    }.items():
        script = bin_dir / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
    env = {"PATH": str(bin_dir) + os.pathsep + os.defpath, "KEYCHAIN_TEST_LOG": str(log)}
    result = subprocess.run(["bash", str(root / "scripts/load-keys-macos.sh"), "true"],
                            cwd=root, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 97, result.stderr
    assert log.read_text().splitlines() == ["find-generic-password", "-s", "fixture-id-item", "-w"]
    assert result.stdout == ""


def test_env_example_lists_documented_keychain_item_variables():
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    for name in ("WAITLESS_KEYCHAIN_AWS_ID_ITEM", "WAITLESS_KEYCHAIN_AWS_SECRET_ITEM", "WAITLESS_KEYCHAIN_JEV_ITEM"):
        assert name + "=" in example
