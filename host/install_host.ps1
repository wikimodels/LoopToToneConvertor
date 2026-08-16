param(
    [Parameter(Mandatory = $true)][string]$ExtensionId,
    [string]$Repo
)

$ErrorActionPreference = 'Stop'
if (-not $Repo) { $Repo = (Get-Location).Path }
$Repo = $Repo.TrimEnd('\')
$Repo = (Resolve-Path $Repo).Path

$py = (Get-Command python -ErrorAction Stop).Source
$jsonPath = Join-Path $Repo "host\com.looptotone.server.json"
$manifest = @{
    name            = "com.looptotone.server"
    description     = "Starts the LoopToToneConvertor engine on 127.0.0.1:8002"
    path            = $py
    type            = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$manifest | ConvertTo-Json | Set-Content -Path $jsonPath -Encoding UTF8

$keys = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.looptotone.server",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.looptotone.server"
)
foreach ($k in $keys) {
    New-Item -Path $k -Force | Out-Null
    Set-ItemProperty -Path $k -Name '(Default)' -Value $jsonPath
}

Write-Host "[+] registry keys written"
Write-Host "[+] host manifest: $jsonPath"
Write-Host "[+] extension id:  $ExtensionId"