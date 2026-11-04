"""Regression: a gog response carrying invalid UTF-8 bytes must not crash the read.

Both `bin/gog-firewall` and `scripts/read-redacted-email.py` decode the `gog`
subprocess output as text. Without `errors="replace"`, one email body with a
stray Latin-1 byte raised UnicodeDecodeError and killed the whole read.
Each test points the code at a fake `gog` that writes an invalid byte (0xE9).
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GOG_FIREWALL_PATH = REPO_ROOT / "bin" / "gog-firewall"
READ_REDACTED_EMAIL_PATH = REPO_ROOT / "scripts" / "read-redacted-email.py"

# JSON whose body contains a raw 0xE9 byte (Latin-1 "e-acute"), invalid as UTF-8.
INVALID_UTF8_MESSAGE_BYTES = b'{"body": "Caf\xe9 ballot status update", "payload": {}}\n'


def write_fake_gog_binary(directory: Path) -> Path:
    """Write an executable `gog` that prints the invalid-UTF-8 JSON to stdout."""
    fake_gog_path = directory / "gog"
    fake_gog_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.buffer.write({INVALID_UTF8_MESSAGE_BYTES!r})\n"
    )
    fake_gog_path.chmod(0o755)
    return fake_gog_path


def test_gog_firewall_survives_invalid_utf8_from_gog(tmp_path: Path) -> None:
    write_fake_gog_binary(tmp_path)
    environment = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    # A write command passes output straight through, isolating the decode step.
    completed = subprocess.run(
        [sys.executable, str(GOG_FIREWALL_PATH), "gmail", "labels", "create", "x"],
        capture_output=True,
        env=environment,
        timeout=60,
    )
    assert b"UnicodeDecodeError" not in completed.stderr, completed.stderr
    assert completed.returncode == 0, completed.stderr
    assert "Caf� ballot" in completed.stdout.decode("utf-8")


def test_read_redacted_email_survives_invalid_utf8_from_gog(tmp_path: Path, monkeypatch) -> None:
    fake_gog_path = write_fake_gog_binary(tmp_path)
    module_spec = importlib.util.spec_from_file_location("read_redacted_email", READ_REDACTED_EMAIL_PATH)
    read_redacted_email = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(read_redacted_email)
    monkeypatch.setattr(read_redacted_email, "GOG_BINARY_PATH", str(fake_gog_path))

    message = read_redacted_email.fetch_message_json("any-message-id")
    assert message["body"] == "Caf� ballot status update"
    assert json.dumps(message)  # round-trips as ordinary text
