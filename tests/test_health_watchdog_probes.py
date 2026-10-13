"""Tests for the health watchdog's pure parsers: ps / launchctl rows, sysctl and
memory_pressure output, plist classification, daemon-log counting, tab summaries."""

import datetime as dt
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from health_watchdog import config  # noqa: E402
from health_watchdog.browser_hygiene import renderer_processes, summarize_tabs  # noqa: E402
from health_watchdog.landline_health import count_silent_kills  # noqa: E402
from health_watchdog.plist_inventory import classify_plist, service_labels  # noqa: E402
from health_watchdog.process_table import (  # noqa: E402
    ancestors,
    children_by_ppid,
    descendants,
    format_top_rss,
    index_by_pid,
    parse_etime,
    parse_launchctl_list,
    parse_ps_output,
    short_command,
)
from health_watchdog.subprocess_deadline import run_with_deadline  # noqa: E402
from health_watchdog.system_probes import (  # noqa: E402
    parse_loadavg,
    parse_memory_pressure,
    parse_swapusage,
    tccd_cpu,
)

PS_TEXT = """\
50356     1 50356 ??       root              17-18:55:04 130.4  19248 /System/Library/PrivateFrameworks/TCC.framework/Support/tccd system
91805     1 91805 ??       operator          02-00:37:20   3.3  16528 /System/Library/PrivateFrameworks/TCC.framework/Support/tccd
38917     1 38917 ??       operator          15-23:51:40   0.0  40000 /usr/bin/python3 -m landline
 1234 38917  1234 ??       operator             01:02:03  12.0 551136 claude --permission-mode bypassPermissions --print
92035 92029 92035 ttys000  operator          20-06:50:02   0.5 300000 claude --dangerously-skip-permissions
  777     1   777 ??       operator                 45:10   0.0   2000 /bin/bash /x/scripts/trigger-morning-brief-claude-code.sh
garbage line here
"""


def test_parse_etime_formats():
    assert parse_etime("45:10") == 45 * 60 + 10
    assert parse_etime("01:02:03") == 3723
    assert parse_etime("02-00:37:20") == 2 * 86400 + 37 * 60 + 20


def test_parse_ps_rows_and_tree_helpers():
    table = parse_ps_output(PS_TEXT)
    assert [row.pid for row in table] == [50356, 91805, 38917, 1234, 92035, 777]
    root = table[0]
    assert root.user == "root" and root.cpu_percent == 130.4 and root.command.endswith("tccd system")
    assert table[4].tty == "ttys000"
    by_pid = index_by_pid(table)
    children = children_by_ppid(table)
    assert ancestors(1234, by_pid) == [38917]
    assert descendants(38917, children) == [1234]
    assert tccd_cpu(table) == (130.4, 3.3)


def test_parse_launchctl_list_filters_prefix():
    text = "-\t-15\t%s.morning-brief\n38917\t0\t%s.telegram-daemon\n123\t0\tcom.apple.other\n" % (
        config.LAUNCHD_PREFIX, config.LAUNCHD_PREFIX)
    assert parse_launchctl_list(text) == {
        "%s.morning-brief" % config.LAUNCHD_PREFIX: None,
        "%s.telegram-daemon" % config.LAUNCHD_PREFIX: 38917,
    }


def test_sysctl_and_memory_pressure_parsers():
    assert parse_swapusage("vm.swapusage: total = 4096.00M  used = 2817.31M  free = 1278.69M  (encrypted)") == (
        4096.0, 2817.31)
    assert parse_swapusage("total = 10.00G  used = 9.20G  free = 0.80G") == (10240.0, 9420.8)
    assert parse_swapusage("nonsense") is None
    assert parse_memory_pressure("Pages free: 1\n\nSystem-wide memory free percentage: 65%\n") == 65
    assert parse_memory_pressure("") is None
    assert parse_loadavg("{ 2.60 5.90 9.43 }") == (2.6, 5.9, 9.43)
    assert parse_loadavg("") is None


def test_plist_classification_and_service_labels():
    daemon = classify_plist({"Label": "p.telegram-daemon", "KeepAlive": True, "ProgramArguments": ["python3"]})
    conditional = classify_plist({"Label": "p.webapp", "KeepAlive": {"SuccessfulExit": False}})
    cron = classify_plist({"Label": "p.morning-brief", "StartCalendarInterval": {"Hour": 7}})
    assert daemon.keepalive and not daemon.scheduled and daemon.suffix == "telegram-daemon"
    assert conditional.keepalive
    assert not cron.keepalive and cron.scheduled
    labels = service_labels([daemon, conditional, cron], prefix="p")
    assert {"p.telegram-daemon", "p.webapp", "p.health-watchdog", "p.daemon-watchdog"} <= labels
    assert "p.morning-brief" not in labels


def test_silent_kill_counting_respects_window():
    log = "\n".join([
        "[2026-10-12 21:57:29] Claude silent for 600s, killing process",
        "[2026-10-13 00:26:53] Claude silent for 600s, killing process",
        "[2026-10-13 14:28:28] Claude silent for 600s, killing process",
        "[2026-10-13 14:30:00] some other line",
        "[bad stamp] Claude silent for 600s, killing process",
    ])
    now = dt.datetime(2026, 10, 13, 16, 30)
    assert count_silent_kills(log, now, window_seconds=6 * 3600) == 1
    assert count_silent_kills(log, now, window_seconds=24 * 3600) == 3


def test_tab_summaries_use_hosts_only_and_cap():
    tabs = [{"url": "https://example.com/private/path?token=1", "title": "A very long title " * 5}]
    tabs += [{"url": "https://host%d.test/x" % i, "title": ""} for i in range(10)]
    lines = summarize_tabs(tabs, limit=3)
    assert lines[0].startswith("example.com - A very long title") and "token" not in lines[0]
    assert lines[1] == "host0.test" and lines[-1] == "... and 8 more"


def test_renderer_processes_match_profile_only():
    table = parse_ps_output(
        "1 0 1 ?? u 00:01 50.0 1 Chromium Helper --type=renderer --user-data-dir=%s\n"
        "2 0 2 ?? u 00:01 50.0 1 Chromium Helper --type=renderer --user-data-dir=/elsewhere\n"
        % config.BROWSER_PROFILE_DIR)
    assert [row.pid for row in renderer_processes(table)] == [1]


def test_short_command_and_top_rss_formatting():
    assert short_command("/usr/bin/python3 -m landline") == "python3 -m landline"
    assert short_command("/bin/bash /x/scripts/trigger-morning-brief-claude-code.sh") == (
        "bash trigger-morning-brief-claude-code.sh")
    assert short_command("/Applications/OrbStack.app/Contents/MacOS/OrbStack Helper --flag") == "OrbStack"
    assert short_command("claude --dangerously-skip-permissions") == "claude --dangerously-skip-permissions"
    table = parse_ps_output(PS_TEXT)
    assert format_top_rss(table, 1) == ["claude --permission-mode (pid 1234) 0.5 GB"]


def test_run_with_deadline_times_out_and_kills_a_sleeper():
    outcome = run_with_deadline(["/bin/sleep", "30"], deadline_seconds=0.3)
    assert outcome.status == "timeout" and not outcome.abandoned
    assert outcome.elapsed_seconds < 10


def test_wait_for_exit_treats_unreaped_zombie_as_exited():
    import os
    import signal
    import subprocess

    from health_watchdog.subprocess_deadline import process_alive, wait_for_exit

    child = subprocess.Popen(["/bin/sleep", "300"])
    os.kill(child.pid, signal.SIGKILL)
    # Not reaped yet: kill(0) still says alive, but the killer must not call it a survivor.
    assert process_alive(child.pid)
    assert wait_for_exit([child.pid], grace_seconds=3) == []
    child.wait(timeout=5)


def test_run_with_deadline_captures_output_and_start_errors():
    ok = run_with_deadline(["/bin/echo", "hi"], deadline_seconds=5)
    assert ok.succeeded and ok.stdout.strip() == "hi"
    missing = run_with_deadline(["/nonexistent/binary"], deadline_seconds=5)
    assert missing.status == "error" and not missing.succeeded
