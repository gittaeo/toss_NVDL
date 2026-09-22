$ErrorActionPreference = 'Stop'
$env:TOSS_CLIENT_ID = Read-Host 'Toss client ID'
$secure = Read-Host 'Toss client secret' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:TOSS_CLIENT_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
Remove-Variable secure -ErrorAction SilentlyContinue
try {
    python (Join-Path $PSScriptRoot 'bot.py')
} finally {
    Remove-Item Env:TOSS_CLIENT_ID -ErrorAction SilentlyContinue
    Remove-Item Env:TOSS_CLIENT_SECRET -ErrorAction SilentlyContinue
}
