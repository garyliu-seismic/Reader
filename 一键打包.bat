@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PyReader 一键打包
echo ============================================
echo   PyReader 一键打包（安装包 + 便携版 + 离线模型）
echo ============================================
echo.
python build_installer.py %*
echo.
echo 打包结束。产物在 release\ 目录，可拷贝到 U 盘分发。
pause
