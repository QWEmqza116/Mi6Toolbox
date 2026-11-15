@echo off
chcp 936 >nul
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"

rem 优先用自带的 Python（python-embed），整个文件夹拷到别的电脑也能直接跑
if exist "%~dp0python-embed\pythonw.exe" (
    start "" "%~dp0python-embed\pythonw.exe" "%~dp0webui\serve.py"
    exit /b 0
)
if exist "%~dp0python-embed\python.exe" (
    start "" "%~dp0python-embed\python.exe" "%~dp0webui\serve.py"
    exit /b 0
)

rem 没有内置 Python 就退回系统装的
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0webui\serve.py"
    exit /b 0
)
where python >nul 2>nul
if not errorlevel 1 (
    start "" python "%~dp0webui\serve.py"
    exit /b 0
)

echo.
echo  [!] 没找到 Python：python-embed 文件夹不在，系统里也没有 Python。
echo      把整个工具箱文件夹完整拷贝（别只拷 .bat），或者装个 Python 3.8+ 并勾选 Add Python to PATH。
echo.
pause