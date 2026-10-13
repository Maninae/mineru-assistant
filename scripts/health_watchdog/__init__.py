"""System health watchdog for the assistant workspace (launchd job, every 5 min).

Python 3.9 compatible (see config.py). Run as `python3 -m health_watchdog` with the
scripts/ directory on PYTHONPATH; `DRY_RUN=1` prints what it would alert and kill.
"""
