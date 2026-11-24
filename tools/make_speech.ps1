# Synthesizes Russian speech into a 16 kHz mono WAV using the built-in SAPI voice.
# Gives the ASR benchmark a reference transcript with exactly known ground truth.
#
#   .\tools\make_speech.ps1 -Text "Коллеги, начинаем." -Out .artifacts\speech\sample.wav
param(
    [Parameter(Mandatory = $true)][string]$Text,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Voice = 'Microsoft Irina Desktop'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$full = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Out))
$dir = Split-Path -Parent $full
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice($Voice)
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $synth.SetOutputToWaveFile($full, $format)
    $synth.Speak($Text)
}
finally {
    $synth.Dispose()
}

$info = Get-Item $full
Write-Host ("wrote {0} ({1:N1} KB)" -f $info.FullName, ($info.Length / 1KB))
