param([string]$ServerUrl = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    $taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    if ($ServerUrl) {
        $env:CREATOR_BUILD_URL = $ServerUrl
        @'
import json, os
from pathlib import Path
from creator_desk.endpoints import validate_server_url
url = validate_server_url(os.environ['CREATOR_BUILD_URL'])
Path('creator_desk/client-config.json').write_text(json.dumps({'server_url': url}), encoding='utf-8')
'@ | & $taskPython -
        if ($LASTEXITCODE -ne 0) { throw 'Invalid server URL' }
    }
    & $taskPython -m PyInstaller --noconfirm --onedir --windowed --name CreatorDesk --specpath build `
        --paths $taskRoot --add-data "$(Join-Path $taskRoot 'migrations');migrations" `
        --collect-data creator_desk --collect-submodules keyring.backends `
        --hidden-import alembic.ddl.sqlite --hidden-import sqlalchemy.dialects.sqlite `
        --hidden-import comtypes.stream --hidden-import pywinauto `
        --exclude-module creator_desk_server --exclude-module aiogram --exclude-module fastapi `
        --exclude-module psycopg --exclude-module psycopg_binary `
        --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets `
        --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
        scripts/client-entry.py
    if ($LASTEXITCODE -ne 0) { throw 'Client build failed' }
    @'
Запуск: CreatorDesk.exe
При первом запуске введите ключ от администратора.
Ctrl+, — стиль, примеры и память. Ctrl+Shift+L — смена ключа.
Переписка для подготовки ответа обрабатывается на сервере AI.
Распространяйте всю папку CreatorDesk, включая _internal.
'@ | Set-Content -LiteralPath 'dist\CreatorDesk\ПРОЧТИ.txt' -Encoding utf8
    Write-Host 'Client built: dist\CreatorDesk\CreatorDesk.exe'
} finally {
    Remove-Item Env:CREATOR_BUILD_URL -ErrorAction SilentlyContinue
    Pop-Location
}
