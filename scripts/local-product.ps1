[CmdletBinding()]
param(
  [ValidateSet('Check','Start','Stop','Status','Restart','ReloadApi')][string]$Action='Start',
  [string]$SettingsFile,
  [switch]$Preview
)
$ErrorActionPreference='Stop'
$repoRoot=Split-Path $PSScriptRoot -Parent
if (-not $SettingsFile) { $SettingsFile=Join-Path $repoRoot 'work/product-r2/settings.json' }
$SettingsFile=(Resolve-Path -LiteralPath $SettingsFile).Path
$runtimeSettings=Get-Content -LiteralPath $SettingsFile -Raw | ConvertFrom-Json
$runtimeDir=Join-Path $repoRoot 'work/product-r2/runtime'
$manifestFile=Join-Path $runtimeDir 'processes.json'
$python=Join-Path $repoRoot '.venv/Scripts/python.exe'
$helper=Join-Path $PSScriptRoot 'local_product_runtime.py'
New-Item -ItemType Directory -Force $runtimeDir | Out-Null

function Read-Manifest {
  if (Test-Path -LiteralPath $manifestFile) { return @(Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json) }
  return @()
}
function Owned-Process($record) {
  $process=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
  if (-not $process) { return $null }
  if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne $record.start_ticks) { return $null }
  if ($process.Path -ne $record.executable) { return $null }
  return $process
}
function Stop-Owned {
  foreach($record in (Read-Manifest)) {
    $process=Owned-Process $record
    if ($process) {
      # The tracked identity (PID, creation time, executable) guards PID reuse.
      # /T includes only descendants of this verified launcher process.
      & taskkill.exe /PID $record.pid /T /F | Out-Null
      Write-Output ("Stopped owned "+$record.name)
    } else { Write-Output ("Skipped absent or replaced PID for "+$record.name) }
  }
}
if ($Action -eq 'Stop') { Stop-Owned; exit 0 }
if ($Action -eq 'ReloadApi') {
  $records=Read-Manifest
  $apiRecord=@($records | Where-Object { $_.name -eq 'api' })
  if($apiRecord.Count -ne 1 -or -not (Owned-Process $apiRecord[0])) { throw 'Cannot verify owned API process; no process stopped.' }
  & taskkill.exe /PID $apiRecord[0].pid /T /F | Out-Null
  $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
  $stdout=Join-Path $runtimeDir "$stamp-api.stdout.log"
  $stderr=Join-Path $runtimeDir "$stamp-api.stderr.log"
  $arguments='"'+$helper+'" api --settings "'+$SettingsFile+'"'
  $process=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
  $newRecord=[pscustomobject]@{name='api';pid=$process.Id;start_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString();executable=$python;stdout=$stdout;stderr=$stderr}
  $records=@($records | Where-Object { $_.name -ne 'api' }) + @($newRecord)
  ConvertTo-Json -InputObject @($records) -Depth 4 | Set-Content -LiteralPath $manifestFile -Encoding utf8
  for($attempt=0;$attempt -lt 30;$attempt++) {
    if(-not (Owned-Process $newRecord)) { throw ('API exited; inspect '+$stderr) }
    try {
      $health=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$runtimeSettings.api_port+'/health') -TimeoutSec 2
      if($health.code -eq 0) { Write-Output 'Owned API reloaded; Worker and frontend retained.'; exit 0 }
    } catch { }
    Start-Sleep -Seconds 1
  }
  throw 'Reloaded API did not become ready; Worker and frontend were not stopped.'
}
if ($Action -eq 'Status') {
  foreach($record in (Read-Manifest)) { Write-Output ($record.name+': '+[bool](Owned-Process $record)) }
  & $python $helper check --settings $SettingsFile
  if($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  & $python $helper worker-ready --settings $SettingsFile
  exit $LASTEXITCODE
}
if ($Action -eq 'Restart') { Stop-Owned }
if ($Action -in @('Start','Restart')) {
  foreach($record in (Read-Manifest)) { if (Owned-Process $record) { throw 'Owned runtime already running; use Status or Restart.' } }
  # Reuse the named Compose project without recreating containers or volumes.
  $containerNames=@(& docker.exe ps -a --filter 'label=com.docker.compose.project=dovideo-r2' --format '{{.Names}}')
  if ($LASTEXITCODE -ne 0) { throw 'Docker is unavailable.' }
  if ($containerNames.Count -lt 5) { throw 'Existing infrastructure incomplete. Provision Compose separately with approved private config.' }
  & docker.exe start @containerNames | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not start existing infrastructure.' }
}
$checkExit=1
for($attempt=0;$attempt -lt 12;$attempt++) {
  & $python $helper check --settings $SettingsFile
  $checkExit=$LASTEXITCODE
  if($checkExit -eq 0) { break }
  if($Action -eq 'Check') { break }
  Start-Sleep -Seconds 2
}
if($checkExit -ne 0) { throw 'Preflight failed. No migration was run. Check private config, schema revision, dependencies and infrastructure.' }
if($Action -eq 'Check') { exit 0 }
foreach($port in @($runtimeSettings.api_port,$runtimeSettings.frontend_port)) {
  if(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "Port $port already occupied. Existing process was left untouched." }
}
$records=@()
try {
  foreach($name in @('api','worker',$(if($Preview){'preview'}else{'frontend'}))) {
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdout=Join-Path $runtimeDir "$stamp-$name.stdout.log"
    $stderr=Join-Path $runtimeDir "$stamp-$name.stderr.log"
    $arguments='"'+$helper+'" '+$name+' --settings "'+$SettingsFile+'"'
    $process=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $record=[pscustomobject]@{name=$name;pid=$process.Id;start_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString();executable=$python;stdout=$stdout;stderr=$stderr}
    $records+=$record
    ConvertTo-Json -InputObject @($records) -Depth 4 | Set-Content -LiteralPath $manifestFile -Encoding utf8
  }
  $ready=$false
  for($attempt=0;$attempt -lt 30;$attempt++) {
    foreach($record in $records) { if(-not (Owned-Process $record)) { throw ($record.name+' exited; inspect '+$record.stderr) } }
    try {
      $health=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$runtimeSettings.api_port+'/health') -TimeoutSec 2
      $front=Invoke-WebRequest -Uri ('http://127.0.0.1:'+$runtimeSettings.frontend_port+'/') -TimeoutSec 2
      if($health.code -eq 0 -and $front.StatusCode -eq 200) {
        & $python $helper worker-ready --settings $SettingsFile
        if($LASTEXITCODE -eq 0) { $ready=$true; break }
      }
    } catch { }
    Start-Sleep -Seconds 1
  }
  if(-not $ready) { throw 'API or frontend did not become ready.' }
  Write-Output ('VideoMind: http://127.0.0.1:'+$runtimeSettings.frontend_port+'/')
  Write-Output ('Logs: '+$runtimeDir)
  Write-Output 'Use .\scripts\local-product.ps1 Status to inspect Worker and infrastructure; Stop stops only tracked app processes and preserves containers/data.'
} catch {
  Stop-Owned
  throw
}
