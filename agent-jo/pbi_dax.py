"""
pbi_dax.py - read-only DAX query runner for the semantic model open in Power BI Desktop.

Plug it into the adapter to give the model a `run_dax_query` tool, so it can test a measure
(DEFINE MEASURE ... EVALUATE ...) before handing it to you:

    from pbi_dax import DesktopDaxRunner
    pbi = PowerBIAgent(cfg, dax_runner=DesktopDaxRunner())

How it works: Power BI Desktop hosts a local Analysis Services instance and writes its port to
    %LOCALAPPDATA%\\Microsoft\\Power BI Desktop\\AnalysisServicesWorkspaces\\*\\Data\\msmdsrv.port.txt
    (Store version: %USERPROFILE%\\Microsoft\\Power BI Desktop Store App\\AnalysisServicesWorkspaces\\...)
The runner finds the newest port file, then runs the query through ADOMD.NET from Windows
PowerShell and returns the rows as JSON. Nothing leaves the machine.

ADOMD.NET: point `adomd_dll` (or the PBI_ADOMD_DLL environment variable) at
Microsoft.AnalysisServices.AdomdClient.dll. It ships with SSMS, DAX Studio and the
"Microsoft.AnalysisServices.AdomdClient.retail.amd64" NuGet package; the runner also
searches those usual install folders.

Any callable with the same signature works as a runner, e.g. one that calls the
Power BI Modeling MCP server or semantic-link's evaluate_dax:
    runner(query: str, max_rows: int) -> {"results": [{"columns": [...], "rows": [[...]], "total": n}]}
"""
from __future__ import annotations

import glob
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

_PS_SCRIPT = r"""
param([string]$DllPath, [int]$Port, [string]$QueryFile, [int]$MaxRows)
$ErrorActionPreference = 'Stop'
Add-Type -Path $DllPath
$query = [IO.File]::ReadAllText($QueryFile)
$conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$Port")
$conn.Open()
try {
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = $query
    $reader = $cmd.ExecuteReader()
    $sets = New-Object System.Collections.ArrayList
    do {
        $cols = @(for ($i = 0; $i -lt $reader.FieldCount; $i++) { $reader.GetName($i) })
        $rows = New-Object System.Collections.ArrayList
        $total = 0
        while ($reader.Read()) {
            $total++
            if ($rows.Count -lt $MaxRows) {
                $row = New-Object object[] $reader.FieldCount
                for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                    $v = $reader.GetValue($i)
                    if ($v -is [DBNull] -or $null -eq $v) { $row[$i] = $null }
                    elseif ($v -is [datetime]) { $row[$i] = $v.ToString('s') }
                    else { $row[$i] = [Convert]::ToString($v, [Globalization.CultureInfo]::InvariantCulture) }
                }
                [void]$rows.Add($row)
            }
        }
        [void]$sets.Add(@{ columns = $cols; rows = $rows.ToArray(); total = $total })
    } while ($reader.NextResult())
    $reader.Close()
    ConvertTo-Json -InputObject @{ results = $sets.ToArray() } -Depth 6 -Compress
} finally {
    $conn.Close()
}
"""

_DLL_GLOBS = [
    r"C:\Program Files\Microsoft SQL Server Management Studio*\**\Microsoft.AnalysisServices.AdomdClient.dll",
    r"C:\Program Files (x86)\Microsoft SQL Server Management Studio*\**\Microsoft.AnalysisServices.AdomdClient.dll",
    r"C:\Program Files\DAX Studio\Microsoft.AnalysisServices.AdomdClient.dll",
    r"C:\Program Files\Microsoft.NET\ADOMD.NET\*\Microsoft.AnalysisServices.AdomdClient.dll",
    os.path.expanduser(r"~\.nuget\packages\microsoft.analysisservices.adomdclient*\**\Microsoft.AnalysisServices.AdomdClient.dll"),
]


class DaxRunnerError(RuntimeError):
    pass


def find_desktop_ports() -> list:
    """Ports of running Power BI Desktop instances, newest first."""
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces",
        Path.home() / "Microsoft" / "Power BI Desktop Store App" / "AnalysisServicesWorkspaces",
    ]
    files = []
    for r in roots:
        files += list(r.glob("*/Data/msmdsrv.port.txt")) if r.exists() else []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    ports = []
    for f in files:
        raw = f.read_bytes()
        text = raw.decode("utf-16-le" if raw[1:2] == b"\x00" else "utf-8", errors="ignore")
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            ports.append(int(digits))
    return ports


def find_adomd_dll() -> Optional[str]:
    env = os.environ.get("PBI_ADOMD_DLL")
    if env and Path(env).exists():
        return env
    for pattern in _DLL_GLOBS:
        hits = sorted(glob.glob(pattern, recursive=True), reverse=True)
        if hits:
            return hits[0]
    return None


class DesktopDaxRunner:
    """Run read-only DAX queries against the model open in Power BI Desktop (Windows only)."""

    def __init__(self, port: Optional[int] = None, adomd_dll: Optional[str] = None, timeout: int = 120):
        self.port = port
        self.adomd_dll = adomd_dll
        self.timeout = timeout

    def __call__(self, query: str, max_rows: int = 50) -> dict:
        if platform.system() != "Windows":
            raise DaxRunnerError("DesktopDaxRunner needs Windows with Power BI Desktop open")
        port = self.port or next(iter(find_desktop_ports()), None)
        if not port:
            raise DaxRunnerError("no running Power BI Desktop model found - open the .pbip or .pbix first")
        dll = self.adomd_dll or find_adomd_dll()
        if not dll:
            raise DaxRunnerError("Microsoft.AnalysisServices.AdomdClient.dll not found - install SSMS or DAX Studio, "
                                 "or set PBI_ADOMD_DLL to its path")
        with tempfile.TemporaryDirectory() as td:
            ps1, qf = Path(td) / "run.ps1", Path(td) / "query.dax"
            ps1.write_text(_PS_SCRIPT, encoding="utf-8-sig")
            qf.write_text(query, encoding="utf-8")
            p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-File", str(ps1), "-DllPath", dll, "-Port", str(port), "-QueryFile", str(qf),
                                "-MaxRows", str(int(max_rows))],
                               capture_output=True, text=True, timeout=self.timeout)
        if p.returncode != 0:
            raise DaxRunnerError((p.stderr or p.stdout).strip()[-2000:] or f"exit {p.returncode}")
        try:
            return json.loads(p.stdout.strip() or "{}")
        except json.JSONDecodeError:
            raise DaxRunnerError("unexpected runner output: " + p.stdout[-500:])
