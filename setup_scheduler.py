"""
setup_scheduler.py
==================
Creates and manages a robust Windows Task Scheduler task that runs
update_profile.py daily with production-grade reliability settings:

  * Explicit working directory (avoids C:\\Windows\\System32 cwd bug)
  * StartWhenAvailable: Catches up immediately if PC was asleep or off at scheduled time
  * AllowStartIfOnBatteries: Runs on laptops even when unplugged
  * Auto-retries: Retries 3 times if network is temporarily unavailable
  * Execution time limit: Prevents hung processes from lingering

Usage:
    python setup_scheduler.py                    # Schedule at 09:00 AM daily
    python setup_scheduler.py --time 08:30       # Schedule at custom time
    python setup_scheduler.py --status           # Check task status and next run time
    python setup_scheduler.py --run-now          # Trigger an immediate run via Task Scheduler
    python setup_scheduler.py --remove           # Delete the scheduled task
"""

import sys
import argparse
import subprocess
from pathlib import Path

TASK_NAME = "NaukriProfileUpdater"
BASE_DIR = Path(__file__).parent.resolve()


def get_python_path() -> str:
    """Returns the absolute path to pythonw.exe (no console window) or python.exe."""
    python_exe = sys.executable
    pythonw = Path(python_exe).parent / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else python_exe


def create_task(run_time: str):
    """Registers task with PowerShell Register-ScheduledTask (falls back to schtasks.exe)."""
    runner = get_python_path()
    script_path = (BASE_DIR / "update_profile.py").resolve()

    print(f"\nSetting up production scheduled task '{TASK_NAME}'...")
    print(f"  Script            : {script_path}")
    print(f"  Working Directory : {BASE_DIR}")
    print(f"  Runner            : {runner}")
    print(f"  Schedule          : {run_time} daily")
    print(f"  Missed-run catchup: Enabled (runs when PC wakes up)")
    print(f"  Battery execution : Enabled (runs on laptop battery)")
    print()

    # Try modern PowerShell Register-ScheduledTask first
    ps_script = f"""
    $Action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{script_path}"' -WorkingDirectory '{BASE_DIR}'
    $Trigger = New-ScheduledTaskTrigger -Daily -At '{run_time}'
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $Action -Trigger $Trigger -Settings $Settings -Force
    """

    ps_result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
    )

    if ps_result.returncode == 0:
        print(f"Success! Task '{TASK_NAME}' created with full battery & catch-up support.")
    else:
        # Fallback to standard schtasks.exe
        print("Note: PowerShell task registration encountered an error, falling back to schtasks.exe...")
        schtasks_cmd = [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", f'"{runner}" "{script_path}"',
            "/SC", "DAILY",
            "/ST", run_time,
            "/RL", "HIGHEST",
            "/F",
        ]
        res = subprocess.run(schtasks_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"Success! Task '{TASK_NAME}' created successfully via schtasks.")
        else:
            print(f"ERROR: Could not create scheduled task.")
            print(f"stdout: {res.stdout.strip()}")
            print(f"stderr: {res.stderr.strip()}")
            print("\nTip: Run your terminal or PowerShell as Administrator.")
            sys.exit(1)

    print("\nVerification commands:")
    print(f"  Check status : python setup_scheduler.py --status")
    print(f"  Test run now : python setup_scheduler.py --run-now")
    print(f"  Remove task  : python setup_scheduler.py --remove\n")


def check_status():
    """Queries and displays the current task status."""
    print(f"\nChecking status for task '{TASK_NAME}'...")
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        lines = res.stdout.splitlines()
        keys_to_show = ["TaskName", "Next Run Time", "Status", "Last Run Time", "Last Result", "Task To Run"]
        for line in lines:
            if any(line.strip().startswith(k) for k in keys_to_show):
                print(f"  {line.strip()}")
    else:
        print(f"Task '{TASK_NAME}' is not currently scheduled.")


def run_now():
    """Triggers an immediate execution of the scheduled task."""
    print(f"\nTriggering immediate execution of '{TASK_NAME}' via Task Scheduler...")
    res = subprocess.run(
        ["schtasks", "/Run", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        print(f"Task '{TASK_NAME}' triggered successfully. Check logs/ for progress.")
    else:
        print(f"Could not trigger task: {res.stderr.strip()}")


def remove_task():
    """Removes the scheduled task."""
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    print(f"Removing scheduled task '{TASK_NAME}'...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' removed successfully.")
    else:
        print(f"Could not remove task (it may not exist): {result.stderr.strip()}")


def validate_time(value: str) -> str:
    """Validates HH:MM format."""
    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Time must be in HH:MM format, e.g. 09:00")
    h, m = parts
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise argparse.ArgumentTypeError("Invalid time. Use HH:MM (24-hour), e.g. 08:30")
    return f"{int(h):02d}:{int(m):02d}"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Set up Windows Task Scheduler to run the Naukri updater daily with production reliability."
    )
    parser.add_argument(
        "--time", default="09:00", type=validate_time,
        metavar="HH:MM",
        help="Time to run daily (24-hour format). Default: 09:00"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Check the status, next run time, and last result of the scheduled task"
    )
    parser.add_argument(
        "--run-now", action="store_true",
        help="Trigger the scheduled task immediately through Windows Task Scheduler"
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove the scheduled task from Windows Task Scheduler"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.status:
        check_status()
    elif args.run_now:
        run_now()
    elif args.remove:
        remove_task()
    else:
        create_task(args.time)
