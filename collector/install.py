"""Registering the collector as a Windows Scheduled Task.

Task XML is generated rather than using `schtasks` flags, because the settings
that actually matter for an always-on collector have no flag equivalents — in
particular ExecutionTimeLimit, which defaults to 72 hours and would otherwise
kill the task silently after three days.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "MTGA Log Collector"

TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{created}</Date>
    <Author>{author}</Author>
    <Description>Captures MTG Arena's Player.log so sessions survive Arena restarts.</Description>
    <URI>\\{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
    <WakeToRun>false</WakeToRun>
    <!-- PT0S = no limit. The 72-hour default would kill a long-running capture. -->
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def pythonw_executable() -> Path:
    """The windowless interpreter, so the task runs with no console."""
    executable = Path(sys.executable)
    candidate = executable.with_name("pythonw.exe")
    return candidate if candidate.exists() else executable


def build_xml(config_path: Path, working_dir: Path) -> str:
    arguments = f'-m collector run --config "{config_path}"'
    return TEMPLATE.format(
        created=datetime.now().isoformat(timespec="seconds"),
        author=escape(os.environ.get("USERNAME", "collector")),
        task_name=escape(TASK_NAME),
        command=escape(str(pythonw_executable())),
        arguments=escape(arguments),
        working_dir=escape(str(working_dir)),
    )


def install(config_path: Path, working_dir: Path | None = None) -> str:
    if os.name != "nt":
        raise RuntimeError("Scheduled Task installation only works on Windows.")

    working_dir = working_dir or Path(__file__).resolve().parent.parent
    xml = build_xml(config_path.resolve(), working_dir)

    # schtasks requires the XML file to be UTF-16.
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-16", suffix=".xml", delete=False
    )
    try:
        handle.write(xml)
        handle.close()
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", handle.name, "/F"],
            capture_output=True,
            text=True,
        )
    finally:
        Path(handle.name).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    return (
        f"Registered scheduled task {TASK_NAME!r}.\n"
        f"  runs:   {pythonw_executable()} -m collector run --config {config_path}\n"
        f"  starts: at logon, restarts up to 3x on failure, no execution time limit\n\n"
        "Start it now without logging out:\n"
        f'  schtasks /Run /TN "{TASK_NAME}"\n'
        "Then confirm with:\n"
        "  python -m collector status"
    )


def uninstall() -> str:
    if os.name != "nt":
        raise RuntimeError("Scheduled Task removal only works on Windows.")
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return f"Removed scheduled task {TASK_NAME!r}."
