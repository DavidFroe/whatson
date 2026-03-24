#!/usr/bin/env python3
"""
WhatSON Scheduler Daemon – Runs in the background and executes scheduled jobs.

Reads jobs from ~/.whatson/jobs.json and executes them according to their schedule.
Controlled via: whatson scheduler start | stop | restart | status

Job types:
  - message: Send a message to a conversation
  - fetch:   Fetch new messages for a conversation (or all)

Schedule formats:
  - "every 30m"              → every 30 minutes
  - "every 2h"               → every 2 hours
  - "daily 08:00"            → every day at 08:00
  - "once 2026-03-02 10:00"  → once at that datetime (disabled after execution)
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# ==============================================================================
# Configuration
# ==============================================================================

WHATSON_HOME = Path.home() / ".whatson"
JOBS_PATH = WHATSON_HOME / "jobs.json"
PID_PATH = WHATSON_HOME / "scheduler.pid"
LOG_PREFIX = "[scheduler]"

# How often to check for due jobs (seconds)
CHECK_INTERVAL = 30


# ==============================================================================
# Job Storage
# ==============================================================================

def load_jobs():
    if JOBS_PATH.exists():
        with open(JOBS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_jobs(jobs):
    with open(JOBS_PATH, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


# ==============================================================================
# Schedule Parsing
# ==============================================================================

def parse_interval(schedule: str) -> int:
    """Parse 'every Xm' or 'every Xh' to seconds. Returns 0 if not an interval."""
    m = re.match(r"every\s+(\d+)\s*(m|h)", schedule.strip(), re.IGNORECASE)
    if not m:
        return 0
    val = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        return val * 60
    elif unit == "h":
        return val * 3600
    return 0


def parse_daily_time(schedule: str):
    """Parse 'daily HH:MM' and return (hour, minute) or None."""
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})", schedule.strip(), re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_once_time(schedule: str):
    """Parse 'once YYYY-MM-DD HH:MM' and return datetime or None."""
    m = re.match(r"once\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", schedule.strip(), re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def is_job_due(job: dict) -> bool:
    """Check if a job should run right now."""
    if not job.get("enabled", True):
        return False

    schedule = job.get("schedule", "")
    last_run_str = job.get("last_run")
    now = datetime.now()

    # --- every Xm / every Xh ---
    interval = parse_interval(schedule)
    if interval > 0:
        if last_run_str is None:
            return True  # Never run before
        try:
            last_run = datetime.fromisoformat(last_run_str)
            return (now - last_run).total_seconds() >= interval
        except ValueError:
            return True

    # --- daily HH:MM ---
    daily = parse_daily_time(schedule)
    if daily:
        hour, minute = daily
        if now.hour == hour and now.minute == minute:
            if last_run_str:
                try:
                    last_run = datetime.fromisoformat(last_run_str)
                    # Don't run again in the same minute
                    if last_run.date() == now.date() and last_run.hour == hour and last_run.minute == minute:
                        return False
                except ValueError:
                    pass
            return True
        return False

    # --- once YYYY-MM-DD HH:MM ---
    once_dt = parse_once_time(schedule)
    if once_dt:
        if now >= once_dt and last_run_str is None:
            return True
        return False

    return False


# ==============================================================================
# Job Execution
# ==============================================================================

def run_whatson_cmd(args: list) -> bool:
    """Run a whatson CLI command as a subprocess."""
    cmd = [sys.executable, "-m", "whatson"] + args
    # Fallback: try the whatson script directly
    whatson_bin = Path.home() / ".local" / "bin" / "whatson"
    if whatson_bin.exists():
        cmd = [str(whatson_bin)] + args

    log(f"  Ausführen: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max
        )
        if result.returncode != 0:
            log(f"  FEHLER (exit {result.returncode}): {result.stderr.strip()[:200]}")
            return False
        if result.stderr.strip():
            for line in result.stderr.strip().split("\n")[-3:]:
                log(f"  {line}")
        return True
    except subprocess.TimeoutExpired:
        log("  TIMEOUT (5 min überschritten)")
        return False
    except Exception as exc:
        log(f"  FEHLER: {exc}")
        return False


def execute_job(job: dict) -> bool:
    """Execute a single job. Returns True on success."""
    job_type = job.get("type", "")
    target = job.get("target", "")
    text = job.get("text", "")

    log(f"Job '{job['id']}' ({job_type} → {target})...")

    if job_type == "message":
        if not text:
            log("  Kein Text für message-Job.")
            return False
        return run_whatson_cmd(["send", target, text])

    elif job_type == "fetch":
        return run_whatson_cmd(["fetch", target])

    else:
        log(f"  Unbekannter Job-Typ: {job_type}")
        return False


# ==============================================================================
# Main Loop
# ==============================================================================

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {LOG_PREFIX} {msg}", flush=True)


def handle_signal(signum, frame):
    log("Signal empfangen, beende Scheduler...")
    PID_PATH.unlink(missing_ok=True)
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Write PID
    WHATSON_HOME.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))

    log(f"Scheduler gestartet (PID {os.getpid()}, interval={CHECK_INTERVAL}s)")

    try:
        while True:
            try:
                jobs = load_jobs()
                for job in jobs:
                    if is_job_due(job):
                        success = execute_job(job)
                        now_str = datetime.now().isoformat(timespec="seconds")
                        job["last_run"] = now_str

                        # Disable "once" jobs after execution
                        if job.get("schedule", "").startswith("once") and success:
                            job["enabled"] = False
                            log(f"  Job '{job['id']}' (once) deaktiviert.")

                # Save updated last_run timestamps
                save_jobs(jobs)

            except Exception as exc:
                log(f"Fehler im Hauptloop: {exc}")

            time.sleep(CHECK_INTERVAL)

    except (KeyboardInterrupt, SystemExit):
        log("Scheduler beendet.")
    finally:
        PID_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
