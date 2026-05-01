param(
  [Parameter(Mandatory = $true)]
  [string]$BackupFile,
  [string]$DbName = "fras",
  [string]$DbUser = $env:POSTGRES_USER
)

if (-not $DbUser) {
  throw "POSTGRES_USER is required. Load your .env or pass -DbUser."
}
if (-not (Test-Path -LiteralPath $BackupFile)) {
  throw "Backup file not found: $BackupFile"
}

$containerPath = "/tmp/restore.dump"
docker cp $BackupFile "fras_db:$containerPath"
docker exec fras_db pg_restore -U $DbUser -d $DbName --clean --if-exists $containerPath
docker exec fras_db rm -f $containerPath
Write-Output "Restored $BackupFile into database $DbName"
