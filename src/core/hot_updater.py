"""热更新模块。

下载 GitHub release 源码 zip，解压覆盖后自动重启。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUEST_TIMEOUT = 30


def run_hot_update(tag: str, repo_owner: str, repo_name: str) -> tuple[bool, str]:
    """下载指定 release 源码 zip，解压覆盖并更新版本号。"""
    version = tag.lstrip("v")
    success, msg = _download_and_extract(tag, repo_owner, repo_name)
    if success:
        _write_version(version)
    return (success, msg)


def _write_version(version: str) -> None:
    """将新版本号写入 config/version.json。"""
    version_path = PROJECT_ROOT / "config" / "version.json"
    try:
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(
            json.dumps({"version": version}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("版本号已更新为 %s", version)
    except Exception as e:
        logger.warning("写入版本号失败：%s", e)


def _download_and_extract(tag: str, repo_owner: str, repo_name: str) -> tuple[bool, str]:
    """下载 GitHub 源码 zip 并解压覆盖。"""
    clean_tag = tag.lstrip("v")
    zip_url = f"https://github.com/{repo_owner}/{repo_name}/archive/refs/tags/v{clean_tag}.zip"
    logger.info("正在下载：%s", zip_url)

    try:
        resp = requests.get(zip_url, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
    except Exception as e:
        return (False, f"下载失败：{e}")

    tmp_dir = tempfile.mkdtemp(prefix="scraper_update_")
    zip_path = os.path.join(tmp_dir, "update.zip")
    try:
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        inner_dirs = [d for d in Path(extract_dir).iterdir() if d.is_dir()]
        if not inner_dirs:
            return (False, "解压后未找到源码目录")
        src_dir = inner_dirs[0]

        _replace_project(src_dir, PROJECT_ROOT)

        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("更新完成，已切换到 v%s", clean_tag)
        return (True, f"已更新到 v{clean_tag}")
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return (False, f"解压覆盖失败：{e}")


def _replace_project(src: Path, dst: Path) -> None:
    """用 src 目录内容整体替换 dst，保留用户数据。"""
    preserve = {".env", "user_data", "output", ".git", ".gitignore"}

    # 新版本包含的条目
    new_items = {item.name for item in src.iterdir()}

    # 删除旧版本有但新版本没有的文件
    for old_item in list(dst.iterdir()):
        if old_item.name in preserve:
            continue
        if old_item.name not in new_items:
            if old_item.is_dir():
                shutil.rmtree(old_item, ignore_errors=True)
            else:
                old_item.unlink(missing_ok=True)
            logger.info("已删除残留：%s", old_item.name)

    # 复制新版本条目
    for item in src.iterdir():
        if item.name in preserve:
            continue
        target = dst / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def restart_app() -> None:
    """启动新进程，不退出当前进程（由调用方在主线程安全退出）。"""
    logger.info("正在重启应用…")
    subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
