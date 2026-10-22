# -*- coding: utf-8 -*-
"""打包前准备离线 TTS 模型：下载缺失模型到 installer_models/ 暂存目录。

用法：python prep_models.py
产物：installer_models/
  sherpa_models/vits-melo-tts-zh_en/        （Melo，默认音色，从 ~/.pyreader 复制）
  sherpa_models/kokoro-int8-multi-lang-v1_1/（Kokoro，可选音色，下载）
  piper_models/en_US-lessac-medium.onnx(.json)（Piper Lessac，英文离线，下载）
"""
import os
import sys
import time
import bz2
import shutil
import tarfile
import urllib.request

HOME = os.path.expanduser("~")
STAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer_models")
USER_AGENT = {"User-Agent": "Mozilla/5.0"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def download(url, dest, timeout=180, retries=5):
    """带重试的下载，写临时文件后原子替换。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=USER_AGENT)
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                total = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
            os.replace(tmp, dest)
            return total
        except Exception as e:
            log(f"  下载失败(第{attempt+1}次)：{e}")
            try:
                os.remove(tmp)
            except Exception:
                pass
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"下载失败：{url}")


def stage_melo():
    src = os.path.join(HOME, ".pyreader", "sherpa_models", "vits-melo-tts-zh_en")
    dst = os.path.join(STAGE, "sherpa_models", "vits-melo-tts-zh_en")
    if not os.path.isfile(os.path.join(src, ".complete")):
        raise RuntimeError("Melo 模型不完整，请先在应用里用一次默认音色朗读以触发下载")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    log(f"Melo 已复制：{dst}")


def stage_kokoro():
    url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2"
    dst_dir = os.path.join(STAGE, "sherpa_models", "kokoro-int8-multi-lang-v1_1")
    flag = os.path.join(dst_dir, ".complete")
    if os.path.isfile(flag):
        log("Kokoro 已存在，跳过")
        return
    log("下载 Kokoro（约 140MB，GitHub）…")
    tar_path = os.path.join(STAGE, "sherpa_models", "kokoro.tar.bz2")
    download(url, tar_path, timeout=300)
    log("解压 Kokoro …")
    os.makedirs(STAGE, exist_ok=True)
    with bz2.BZ2File(tar_path) as bz, tarfile.open(fileobj=bz) as tf:
        tf.extractall(os.path.join(STAGE, "sherpa_models"))
    os.remove(tar_path)
    # 归档内部目录名需与预期一致，补 .complete 标记
    if not os.path.isdir(dst_dir):
        # 归档可能解压成别的目录名，尝试找到
        for name in os.listdir(os.path.join(STAGE, "sherpa_models")):
            if name.startswith("kokoro"):
                os.rename(os.path.join(STAGE, "sherpa_models", name), dst_dir)
                break
    with open(flag, "w") as f:
        f.write("ok")
    log(f"Kokoro 完成：{dst_dir}")


def stage_piper():
    base = "https://hf-mirror.com/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
    dst_dir = os.path.join(STAGE, "piper_models")
    onnx = os.path.join(dst_dir, "en_US-lessac-medium.onnx")
    js = onnx + ".json"
    if os.path.isfile(onnx) and os.path.isfile(js):
        log("Piper Lessac 已存在，跳过")
        return
    log("下载 Piper Lessac（约 60MB，hf-mirror）…")
    download(base + "/en_US-lessac-medium.onnx", onnx)
    download(base + "/en_US-lessac-medium.onnx.json", js)
    log(f"Piper Lessac 完成：{dst_dir}")


if __name__ == "__main__":
    os.makedirs(STAGE, exist_ok=True)
    stage_melo()
    stage_kokoro()
    stage_piper()
    log("全部模型准备完毕：")
    for root, dirs, files in os.walk(STAGE):
        for f in files:
            p = os.path.join(root, f)
            if os.path.getsize(p) > 1 << 20:
                print(f"  {os.path.relpath(p, STAGE):70s} {os.path.getsize(p)/1e6:8.1f} MB")
    print("DONE", flush=True)
