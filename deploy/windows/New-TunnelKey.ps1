param([string]$Config = (Join-Path $env:LOCALAPPDATA 'CodexRelay\worker.json'))
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if (Test-Path -LiteralPath $cfg.ssh.identity_file) { throw 'Dedicated key already exists; it was preserved' }
# Interactive choice of passphrase. Unattended task needs an available agent or
# a dedicated no-passphrase key protected by ACL and server forwarding restrictions.
& ssh-keygen.exe -t ed25519 -f $cfg.ssh.identity_file -C codex-relay-windows
if ($LASTEXITCODE -ne 0) { throw 'Dedicated key generation failed' }
Write-Output ('Send only this public key to the server installer: ' + $cfg.ssh.identity_file + '.pub')
