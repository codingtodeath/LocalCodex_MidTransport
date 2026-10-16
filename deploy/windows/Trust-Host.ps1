param(
    [Parameter(Mandatory=$true)][string]$HostName,
    [int]$Port = 22,
    [Parameter(Mandatory=$true)][string]$ExpectedFingerprint,
    [string]$HostKeyFile,
    [string]$Config = (Join-Path $env:LOCALAPPDATA 'CodexRelay\worker.json')
)
$ErrorActionPreference = 'Stop'
if ($HostName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.:-]+$' -or $Port -lt 1 -or $Port -gt 65535) { throw 'Invalid hostname/port' }
$cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$target = $HostName
if ($Port -ne 22) { $target = '[' + $HostName + ']:' + $Port }
if ($HostKeyFile) {
    $rows = @(Get-Content -LiteralPath $HostKeyFile -Encoding UTF8 | Where-Object { $_ -match '^[^#].* ssh-ed25519 ' })
} else {
    # Windows OpenSSH 9.5 keyscan can fail on sntrup761x25519 KEX. A previously
    # trusted user entry is usable only after the SAME explicit fingerprint check.
    $priorHosts = Join-Path $env:USERPROFILE '.ssh\known_hosts'
    $rows = @()
    if (Test-Path -LiteralPath $priorHosts) {
        $rows = @(& ssh-keygen.exe -F $target -f $priorHosts 2>$null | Where-Object { $_ -match '^[^#].* ssh-ed25519 ' })
    }
    if ($rows.Count -eq 0) {
        $rows = @(& ssh-keyscan.exe -T 5 -p $Port -t ed25519 $HostName 2>$null | Where-Object { $_ -match '^[^#].* ssh-ed25519 ' })
    }
}
if ($rows.Count -ne 1) { throw 'No unique ED25519 key received' }
$parts = $rows[0].Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $digest = $sha.ComputeHash([Convert]::FromBase64String($parts[2])) } finally { $sha.Dispose() }
$actual = 'SHA256:' + [Convert]::ToBase64String($digest).TrimEnd('=')
if ($actual -cne $ExpectedFingerprint) { throw 'Host key fingerprint mismatch; no trust file changed' }
[System.IO.File]::WriteAllText($cfg.ssh.known_hosts_file, $rows[0] + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Output ('Trusted verified ED25519 host key: ' + $actual)
