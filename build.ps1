param([string]$OutputName = 'CaptureCli.exe')
$ErrorActionPreference = 'Stop'
if ([IO.Path]::GetFileName($OutputName) -ne $OutputName -or -not $OutputName.EndsWith('.exe')) { throw 'OutputName must be a local .exe filename' }
$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:exe /platform:x64 ("/out:" + (Join-Path $PSScriptRoot $OutputName)) ("/reference:" + (Join-Path $PSScriptRoot 'Capture3DS.dll')) ("/reference:" + (Join-Path $PSScriptRoot 'DirectShowLib-2005.dll')) /reference:System.Drawing.dll /reference:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'CaptureCli.cs')
if ($LASTEXITCODE -ne 0) { throw 'CaptureCli build failed' }
