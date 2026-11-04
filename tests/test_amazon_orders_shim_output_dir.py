"""`bin/amazon-orders` pins the library's output dir under cache/, never the caller's cwd.

The amazon-orders library `makedirs("./output")` on every invocation, which left stray
empty `output/` dirs wherever an agent's shell happened to be (e.g. memory/daily/).
The test swaps in a fake venv whose `amazon-orders` records its argv.
"""

import os
import subprocess
from pathlib import Path

SHIM_PATH = Path(__file__).resolve().parent.parent / "bin" / "amazon-orders"


def test_shim_passes_output_dir_under_cache_and_keeps_user_args(tmp_path: Path) -> None:
    mineru_home = tmp_path / "home"
    venv_bin = mineru_home / ".venv-amazon" / "bin"
    venv_bin.mkdir(parents=True)
    argv_capture_path = tmp_path / "argv.txt"
    (venv_bin / "activate").write_text(f'PATH="{venv_bin}:$PATH"\n')
    fake_cli = venv_bin / "amazon-orders"
    fake_cli.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{argv_capture_path}"\n')
    fake_cli.chmod(0o755)
    caller_cwd = tmp_path / "caller-cwd"
    caller_cwd.mkdir()

    completed = subprocess.run(
        ["/bin/bash", str(SHIM_PATH), "history", "--last-30-days"],
        cwd=caller_cwd, env=dict(os.environ, MINERU_HOME=str(mineru_home)),
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    recorded_argv = argv_capture_path.read_text().splitlines()
    assert recorded_argv == [
        "--output-dir", str(mineru_home / "cache" / "amazon-orders-output"),
        "history", "--last-30-days",
    ]
    assert list(caller_cwd.iterdir()) == []
