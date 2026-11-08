param(
    [string]$Python = 'python',
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build.ps1') }
& $Python (Join-Path $PSScriptRoot 'tests\test_cli.py')
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }
$testExe = Join-Path $PSScriptRoot 'CaptureCliTests.exe'
& $compiler /nologo /target:exe /platform:x64 /main:CaptureCliTests ("/out:" + $testExe) ("/reference:" + (Join-Path $PSScriptRoot 'Capture3DS.dll')) ("/reference:" + (Join-Path $PSScriptRoot 'DirectShowLib-2005.dll')) /reference:System.Drawing.dll /reference:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'CaptureCli.cs') (Join-Path $PSScriptRoot 'tests\CaptureCliTests.cs')
if ($LASTEXITCODE -ne 0) { throw 'Capture tests build failed' }
& $testExe
if ($LASTEXITCODE -ne 0) { throw 'Capture tests failed' }
