from __future__ import annotations

import os
import subprocess
from pathlib import Path


def choose_folder(initial_dir: str = "") -> str:
    if os.name != "nt":
        raise RuntimeError("当前只支持在 Windows 上弹出本机文件夹选择器")

    initial = Path(initial_dir).expanduser() if initial_dir else Path.home()
    if not initial.exists() or not initial.is_dir():
        initial = Path.home()

    script = rf"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择 BookVault 要扫描的文件夹'
$dialog.SelectedPath = '{_escape_powershell_string(str(initial))}'
$dialog.ShowNewFolderButton = $false
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.StartPosition = 'CenterScreen'
$owner.Width = 1
$owner.Height = 1
$owner.ShowInTaskbar = $false
$owner.Show()
$owner.WindowState = 'Minimized'
$result = $dialog.ShowDialog($owner)
$owner.Dispose()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
  Write-Output $dialog.SelectedPath
}}
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "无法打开文件夹选择器").strip())
    return completed.stdout.strip()


def _escape_powershell_string(value: str) -> str:
    return value.replace("'", "''")

