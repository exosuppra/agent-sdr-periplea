# ============================================================
#  Cree une archive de LIVRAISON propre, SANS aucun secret.
#  Exclut .env (cles reelles), les caches, la base, les logs et
#  les fichiers de correspondance. Verifie l'archive avant de la
#  produire : si un secret est detecte, l'archive N'EST PAS creee.
#
#  Utilisation : clic droit sur ce fichier > "Executer avec PowerShell"
#  ou dans un terminal : powershell -ExecutionPolicy Bypass -File package_livraison.ps1
# ============================================================
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$out = Join-Path (Split-Path $src -Parent) "sdr-agent-livraison-$stamp.zip"

# Dossiers / fichiers a NE JAMAIS inclure (secrets, caches, donnees perso)
$excludeNames = @(
    '.env', '.git', '__pycache__', 'logs', 'chaos.json', '_full.txt',
    'hubspot_map.json', 'unipile_map.json', 'do_not_contact.json',
    'package_livraison.ps1'
)
$excludeExt = @('.pyc', '.db')

$tmp = Join-Path $env:TEMP "sdr_pkg_$stamp"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp | Out-Null

# Copie selective dans un dossier temporaire
Get-ChildItem -Path $src -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($src.Length).TrimStart('\')
    $parts = $rel -split '[\\/]'
    $skip = $false
    foreach ($p in $parts) { if ($excludeNames -contains $p) { $skip = $true; break } }
    if ($excludeExt -contains $_.Extension) { $skip = $true }
    if (-not $skip) {
        $dest = Join-Path $tmp $rel
        New-Item -ItemType Directory -Path (Split-Path $dest -Parent) -Force | Out-Null
        Copy-Item $_.FullName $dest
    }
}

# Garde-fou : lit les VRAIES valeurs de cle dans le .env local et verifie
# qu'aucune ne se retrouve dans l'archive (aucun fragment de cle code en dur ici).
$envPath = Join-Path $src '.env'
$secretValues = @()
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*(ANTHROPIC_API_KEY|CALCOM_API_KEY|UNIPILE_API_KEY|HUBSPOT_TOKEN|APIFY_TOKEN|HUNTER_API_KEY)\s*=\s*(.+)$') {
            $v = $matches[2].Trim()
            if ($v.Length -ge 12) { $secretValues += $v }
        }
    }
}
$leak = $null
if ($secretValues.Count -gt 0) {
    $leak = Get-ChildItem $tmp -Recurse -File | Select-String -SimpleMatch -Pattern $secretValues
}
if ($leak) {
    Write-Host "ALERTE : un secret a ete detecte. Archive NON creee." -ForegroundColor Red
    $leak | ForEach-Object { Write-Host ("  " + $_.Path + " : " + $_.Line) }
    Remove-Item $tmp -Recurse -Force
    exit 1
}

if (Test-Path $out) { Remove-Item $out -Force }
Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $out
Remove-Item $tmp -Recurse -Force

Write-Host ""
Write-Host "Archive de livraison creee (sans secrets) :" -ForegroundColor Green
Write-Host "  $out"
Write-Host ""
Write-Host "Elle contient .env.example (placeholders) mais PAS .env (tes vraies cles)." -ForegroundColor Green
