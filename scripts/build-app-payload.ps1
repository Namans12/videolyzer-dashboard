[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$BuildId,
    [Parameter(Mandatory=$true)][string]$Revision,
    [Parameter(Mandatory=$true)][string]$SnapshotManifest,
    [switch]$RequireCommitted
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($Destination)) { throw 'Destination must be absolute' }
Push-Location -LiteralPath $root
try {
    # Existing node_modules only. This script never invokes an installer/resolver.
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    $argsList=@('-I','-B',(Join-Path $root 'app_preflight.py'),'--export','--destination',$Destination,
        '--build-id',$BuildId,'--revision',$Revision,'--snapshot-manifest',$SnapshotManifest)
    if ($RequireCommitted) { $argsList+='--require-committed' }
    & (Join-Path $root '.venv\Scripts\python.exe') @argsList
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard export failed' }
} finally { Pop-Location }
