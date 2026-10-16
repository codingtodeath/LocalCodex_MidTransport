import os
import subprocess
from pathlib import Path


def strict_acl(path: Path):
    if os.name != "nt":
        return path.stat().st_mode & 0o077 == 0
    env = os.environ.copy()
    env["RELAY_AUDIT_PATH"] = str(path.resolve())
    script = r"""
$ErrorActionPreference = 'Stop'
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18')
$acl = ([System.IO.DirectoryInfo]$env:RELAY_AUDIT_PATH).GetAccessControl()
$ok = $acl.AreAccessRulesProtected
foreach ($rule in $acl.Access) {
    $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($rule.AccessControlType -eq 'Allow' -and $sid -notin $allowed) { $ok = $false }
}
if ($ok) { exit 0 } else { exit 1 }
"""
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0


def secure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    env = os.environ.copy()
    env["RELAY_ACL_PATH"] = str(path.resolve())
    script = r"""
$ErrorActionPreference = 'Stop'
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
foreach ($principal in @($sid, [System.Security.Principal.SecurityIdentifier]'S-1-5-18')) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($principal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
([System.IO.DirectoryInfo]$env:RELAY_ACL_PATH).SetAccessControl($acl)
"""
    if subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=subprocess.CREATE_NO_WINDOW).returncode:
        raise OSError("acl_setup_failed")
