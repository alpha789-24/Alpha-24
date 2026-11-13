@echo off
cd /d %~dp0
if not exist node_modules\react\package.json (
    echo Installing frontend dependencies...
    call npm install
)
call npm run dev
