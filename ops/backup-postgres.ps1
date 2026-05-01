param(
  [string]$OutputDir = ".\backups",
  [string]$DbName = "fras",
  [string]$DbUser = $env:POSTGRES_USER
)

if (-not $DbUser) {
  throw "POSTGRES_USER is required. Load your .env or pass -DbUser."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupFile = Join-Path $OutputDir "$DbName-$timestamp.dump"

docker exec fras_db pg_dump -U $DbUser -Fc $DbName | Set-Content -Encoding Byte -Path $backupFile
Write-Output "Backup written to $backupFile"
