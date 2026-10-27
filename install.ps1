# Instala ragnar-bridge en Windows: venv propio, config, chequeo y arranque solo al
# iniciar sesion (tarea programada sin ventana que lo reinicia si se cae, salvo si
# Ragnar rechaza el token). El token se pide sin eco o sale de RAGNAR_BRIDGE_TOKEN.
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/RagnarGroup/rg_ragnar_bridge/main/install.ps1))) -Url wss://panel.ragnargroup.app/api/v1/bridge/ws
#
# -ReusarConfig usa la config ya escrita (sin pedir token). -Desinstalar quita el arranque.
[CmdletBinding()]
param(
    [string]$Url,
    [ValidateSet("", "claude", "agy", "codex", "hermes")][string]$Agent = "",
    [string]$Workdir = $HOME,
    [string]$Model = "",
    [string]$Name = "",
    [string]$Source = "git+https://github.com/RagnarGroup/rg_ragnar_bridge",
    [switch]$ReusarConfig,
    [switch]$Desinstalar
)

$ErrorActionPreference = "Stop"

$sufijo = if ($Name) { "-$Name" } else { "" }
$raiz = Join-Path $env:LOCALAPPDATA "ragnar-bridge"
$venv = Join-Path $raiz "venv"
$exe = Join-Path $venv "Scripts\ragnar-bridge.exe"
$config = Join-Path $HOME ".config\ragnar-bridge\config$sufijo.json"
$runner = Join-Path $raiz "servicio$sufijo.cmd"
$prefijoRunner = Join-Path $raiz "servicio$sufijo."
$log = Join-Path $raiz "bridge$sufijo.log"
$tarea = "ragnar-bridge$sufijo"
$inicio = Join-Path ([Environment]::GetFolderPath("Startup")) "$tarea.lnk"

function Stop-Bridge {
    Stop-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process |
        Where-Object {
            $linea = $_.CommandLine
            $linea -and ($linea.Contains($prefijoRunner) -or ($linea.Contains("ragnar-bridge") -and $linea.TrimEnd().EndsWith(" run") -and ($linea.Contains($config) -or (-not $Name -and -not $linea.Contains("--config")))))
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Desinstalar) {
    Stop-Bridge
    Unregister-ScheduledTask -TaskName $tarea -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $inicio, "$($prefijoRunner)cmd", "$($prefijoRunner)ps1" -Force -ErrorAction SilentlyContinue
    Write-Host "Arranque automatico quitado. Quedan el venv ($venv) y la config ($config); borralos a mano si ya no los usas."
    exit 0
}

if ($ReusarConfig) {
    if (-not (Test-Path $config)) { Write-Error "No existe $config; corre sin -ReusarConfig." }
} else {
    if (-not $Url) {
        Write-Host "Uso: install.ps1 -Url wss://.../api/v1/bridge/ws [-Agent claude|agy|codex|hermes] [-Workdir DIR] [-Model M] [-Name N]"
        Write-Host "     install.ps1 -ReusarConfig    |    install.ps1 -Desinstalar"
        exit 2
    }
    $token = $env:RAGNAR_BRIDGE_TOKEN
    if (-not $token) {
        $seguro = Read-Host "Token del servidor (ragbrg_..., no se muestra)" -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro))
    }
    if (-not $token) { Write-Error "Falta el token." }
}

$python = $null
foreach ($candidato in @(@("py", "-3"), @("python"))) {
    if (Get-Command $candidato[0] -ErrorAction SilentlyContinue) {
        $version = & $candidato[0] @($candidato | Select-Object -Skip 1) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.9") { $python = $candidato; break }
    }
}
if (-not $python) { Write-Error "Falta Python >= 3.9 (https://www.python.org/downloads/ o winget install Python.Python.3.13)." }

$cli = ""
if ($Agent) {
    $comando = Get-Command $Agent -ErrorAction SilentlyContinue
    if (-not $comando) { Write-Error "No encuentro '$Agent' en el PATH (abri una terminal nueva si acabas de instalarlo)." }
    $cli = $comando.Source
    Write-Host "==> Agente: $Agent ($cli)"
} else {
    $detectados = @("claude", "agy", "codex", "hermes") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue }
    if (-not $detectados) {
        Write-Host "No encuentro claude, agy, codex ni hermes en el PATH. Instala al menos uno y logueate corriendolo una vez:"
        Write-Host "  Claude Code:  irm https://claude.ai/install.ps1 | iex"
        Write-Host "  Codex:        npm install -g @openai/codex   (y despues: codex login)"
        exit 1
    }
    Write-Host "==> Agentes detectados: $($detectados -join ', ')"
}

Stop-Bridge
if (Test-Path $log) { Start-Sleep -Milliseconds 500; Move-Item $log "$log.1" -Force -ErrorAction SilentlyContinue }

Write-Host "==> Instalando ragnar-bridge en $venv"
New-Item -ItemType Directory -Force $raiz | Out-Null
& $python[0] @($python | Select-Object -Skip 1) -m venv $venv
if ($LASTEXITCODE -ne 0) { Write-Error "No pude crear el venv." }
& (Join-Path $venv "Scripts\python.exe") -m pip install --quiet --disable-pip-version-check --upgrade --force-reinstall $Source
if ($LASTEXITCODE -ne 0) { Write-Error "Fallo pip install $Source." }

if (-not $ReusarConfig) {
    Write-Host "==> Escribiendo la config en $config"
    $argumentos = @("--config", $config, "init", "--url", $Url, "--workdir", $Workdir)
    if ($Agent) { $argumentos += @("--agent", $Agent, "--cli", $cli) }
    if ($Model) { $argumentos += @("--model", $Model) }
    $env:RAGNAR_BRIDGE_TOKEN = $token
    try { $null | & $exe @argumentos } finally { Remove-Item Env:RAGNAR_BRIDGE_TOKEN -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) { Write-Error "Fallo ragnar-bridge init." }
}

Write-Host "==> Verificando (config, CLI y conexion con Ragnar; no gasta cuota)"
& $exe --config $config doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "Algun paso fallo (arriba). Corregilo y corre:  & '$exe' --config '$config' doctor"
    exit 1
}

Remove-Item "$($prefijoRunner)ps1" -Force -ErrorAction SilentlyContinue
@"
@echo off
:bucle
for %%A in ("$log") do if %%~zA GTR 10485760 move /y "$log" "$log.1" >nul
"$exe" --config "$config" run >> "$log" 2>&1
if %ERRORLEVEL%==78 (
    echo Ragnar rechazo el token ^(revocado o invalido^): no se reintenta.>> "$log"
    exit /b 78
)
ping -n 6 127.0.0.1 >nul
goto bucle
"@ | Set-Content -Path $runner -Encoding Default

$argumentosTarea = "--headless cmd.exe /d /c `"$runner`""
$usuario = "$env:USERDOMAIN\$env:USERNAME"
try {
    $accion = New-ScheduledTaskAction -Execute "conhost.exe" -Argument $argumentosTarea
    $disparador = New-ScheduledTaskTrigger -AtLogOn -User $usuario
    $ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $tarea -Action $accion -Trigger $disparador -Settings $ajustes -Principal $principal -Description "Ragnar bridge$(if ($Name) { " ($Name)" })" -Force | Out-Null
    Remove-Item $inicio -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $tarea
    $modo = "tarea programada '$tarea'"
} catch {
    $atajo = (New-Object -ComObject WScript.Shell).CreateShortcut($inicio)
    $atajo.TargetPath = "conhost.exe"
    $atajo.Arguments = $argumentosTarea
    $atajo.Save()
    Start-Process conhost.exe -ArgumentList $argumentosTarea
    $modo = "acceso directo en Inicio ($inicio)"
}

Write-Host ""
Write-Host "Listo. Arranca solo al iniciar sesion ($modo). Ya deberias verlo conectado en la app de Ragnar."
Write-Host "  Logs:        Get-Content '$log' -Wait -Tail 50"
Write-Host "  Chequeo:     & '$exe' --config '$config' doctor"
Write-Host "  Desinstalar: install.ps1 -Desinstalar$(if ($Name) { " -Name $Name" })"
