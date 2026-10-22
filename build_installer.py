# -*- coding: utf-8 -*-
"""PyReader 一键打包脚本：生成「离线完整安装包 + 便携绿色版」，可拷贝到 U 盘分发。

步骤：
  1. prep_models —— 确保离线 TTS 模型已就位（Melo / Kokoro / Piper，缺失才联网下载）
  2. pyinstaller —— 打包应用（dist/PyReader）
  3. ISCC        —— 生成安装包 release/PyReader-Setup-<版本>.exe（含离线模型）
  4. 便携版       —— 生成 release/PyReader-Portable-<版本>.zip（解压即用，无需安装）

用法：
  python build_installer.py               # 完整一键打包
  python build_installer.py --skip-build  # 跳过 PyInstaller，复用现有 dist/PyReader
"""
import os
import sys
import time
import shutil
import zipfile
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION = "0.3.0"
PYINSTALLER = [sys.executable, "-m", "PyInstaller"]

ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd):
    log(">> " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"\n命令失败（退出码 {r.returncode}）：{' '.join(cmd)}")


def find_iscc():
    for c in ISCC_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("未找到 Inno Setup 编译器 ISCC.exe，请先安装 Inno Setup 6 后再运行。")


def main():
    skip_build = "--skip-build" in sys.argv
    dist = os.path.join(ROOT, "dist", "PyReader")
    release = os.path.join(ROOT, "release")
    os.makedirs(release, exist_ok=True)

    # 1. 离线 TTS 模型
    log("步骤 1/4：准备离线 TTS 模型（缺失才联网下载）")
    run([sys.executable, "prep_models.py"])

    # 2. PyInstaller
    if skip_build:
        log("步骤 2/4：跳过 PyInstaller（--skip-build），复用现有 dist/PyReader")
    else:
        log("步骤 2/4：PyInstaller 打包应用")
        run(PYINSTALLER + ["PyReader.spec", "--noconfirm"])
    if not os.path.isfile(os.path.join(dist, "PyReader.exe")):
        raise SystemExit("未找到 dist/PyReader/PyReader.exe，打包失败？")

    # 3. Inno Setup 安装包
    log("步骤 3/4：Inno Setup 生成安装包（含离线模型，需几分钟压缩）")
    run([find_iscc(), "/Qp", "PyReader-setup.iss"])

    # 4. 便携绿色版
    log("步骤 4/4：生成便携绿色版 zip")
    portable_zip = os.path.join(release, f"PyReader-Portable-{VERSION}.zip")
    staging = os.path.join(ROOT, "build", "_portable_stage")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)
    stage_root = os.path.join(staging, f"PyReader-Portable-{VERSION}")
    shutil.copytree(dist, stage_root)
    shutil.copytree(os.path.join(ROOT, "installer_models"), os.path.join(stage_root, "models"))
    for extra in ("README.txt", "start.bat", ".env"):
        p = os.path.join(ROOT, extra)
        if os.path.isfile(p):
            shutil.copy(p, stage_root)
    if os.path.isfile(portable_zip):
        os.remove(portable_zip)
    # 模型 onnx 等已是压缩格式，用低压缩级别省时；应用 DLL 仍会得到一定压缩
    with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for root, _, files in os.walk(stage_root):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, staging))
    shutil.rmtree(staging)
    log(f"便携版已生成：{portable_zip}")

    # 单独放一份 .env 到 release/，方便在 U 盘里直接查看/替换密钥
    env_src = os.path.join(ROOT, ".env")
    if os.path.isfile(env_src):
        shutil.copy(env_src, os.path.join(release, ".env"))

    log("")
    log("全部完成！release/ 目录中的文件可拷贝到 U 盘分发：")
    for f in sorted(os.listdir(release)):
        p = os.path.join(release, f)
        log(f"  {os.path.relpath(p, ROOT):50s} {os.path.getsize(p)/1e6:8.1f} MB")


if __name__ == "__main__":
    main()
