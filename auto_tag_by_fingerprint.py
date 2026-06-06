"""
auto_tag_by_fingerprint.py — 用声纹识别匹配音乐标签

用法:
    python auto_tag_by_fingerprint.py /path/to/music
    python auto_tag_by_fingerprint.py /path/to/music --fpcalc /path/to/fpcalc
    python auto_tag_by_fingerprint.py /path/to/music --resume
"""

import sys
import os
import time
import json
import argparse
import subprocess
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import mutagen
import requests

from common import (
    has_mbid, AUDIO_EXTS, log, log_always, log_verbose, ProgressBar,
    load_progress, save_progress, mark_done, set_log_file, set_quiet, set_verbose, write_tags,
)

DEFAULT_ACOUSTID_KEY = "1vOwZtEn"
MB_BASE = "https://musicbrainz.org/ws/2"
HEADERS = {"User-Agent": "music-tagger/1.0 (https://github.com/huihui0765/music_tagger)"}
PROGRESS_FILE = ".fingerprint_progress.json"

# 全局配置
_config = {
    "fpcalc": "fpcalc", "acoustid_key": DEFAULT_ACOUSTID_KEY,
    "dry_run": False, "quiet": False, "verbose": False,
}

# Rate limiter
_rate_lock = threading.Lock()
_request_times = []


def rate_limit(max_per_sec=3):
    """确保请求不超过 max_per_sec 次/秒"""
    with _rate_lock:
        now = time.time()
        while _request_times and _request_times[0] < now - 1.0:
            _request_times.pop(0)
        if len(_request_times) >= max_per_sec:
            wait = 1.0 - (now - _request_times[0])
            if wait > 0:
                time.sleep(wait)
        _request_times.append(time.time())


def find_fpcalc():
    """自动查找 fpcalc"""
    found = shutil.which("fpcalc")
    if found:
        return found
    # 常见安装位置
    temp = os.environ.get("TEMP", "")
    for item in os.listdir(temp) if temp else []:
        if item.startswith("chromaprint-fpcalc") and item.endswith("windows-x86_64"):
            candidate = os.path.join(temp, item, "fpcalc.exe")
            if os.path.isfile(candidate):
                return candidate
    for candidate in [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "chromaprint", "fpcalc.exe"),
        "/usr/local/bin/fpcalc",
        "/opt/homebrew/bin/fpcalc",
        "/usr/bin/fpcalc",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None


def get_fingerprint(filepath):
    """用 fpcalc 生成音频指纹"""
    try:
        result = subprocess.run(
            [_config["fpcalc"], "-json", "-length", "120", filepath],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("duration"), data.get("fingerprint")
    except FileNotFoundError:
        raise RuntimeError("找不到 fpcalc，请用 --fpcalc 指定路径或安装 chromaprint")
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        log(f"  fpcalc 错误: {e}")
    return None, None


def _acoustid_post(duration, fingerprint, retries=3):
    """带重试的 AcoustID 查询"""
    for attempt in range(retries):
        rate_limit(max_per_sec=3)
        try:
            r = requests.post("https://api.acoustid.org/v2/lookup", data={
                "client": _config["acoustid_key"],
                "duration": int(duration),
                "fingerprint": fingerprint,
                "meta": "recordings releasegroups releases tracks",
                "format": "json",
            }, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "ok":
                    return data.get("results", [])
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                log(f"  AcoustID 限流，等待 {wait} 秒...")
                time.sleep(wait)
                continue
            return []
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < retries - 1:
                time.sleep(2)
    return []


def _mb_get(url, params, retries=3):
    """带重试和限速的 MusicBrainz 请求"""
    for attempt in range(retries):
        rate_limit(max_per_sec=1)
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 503:
                time.sleep(5)
                continue
            return None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < retries - 1:
                time.sleep(3)
    return None


def get_best_match(results):
    """从 AcoustID 结果中选最佳匹配"""
    for r in results:
        if r.get("score", 0) < 0.5:
            continue
        recordings = r.get("recordings", [])
        if not recordings:
            continue
        for rec in recordings:
            releases = rec.get("releases", [])
            if releases:
                return {
                    "recording_id": rec.get("id"),
                    "recording_title": rec.get("title"),
                    "artists": rec.get("artists", []),
                    "release": releases[0],
                    "score": r.get("score"),
                }
    for r in results:
        if r.get("score", 0) < 0.3:
            continue
        recordings = r.get("recordings", [])
        if recordings:
            rec = recordings[0]
            releases = rec.get("releases", [])
            return {
                "recording_id": rec.get("id"),
                "recording_title": rec.get("title"),
                "artists": rec.get("artists", []),
                "release": releases[0] if releases else {},
                "score": r.get("score"),
            }
    return None


def _get_track_number_from_mb(release_id, recording_id):
    """从 MusicBrainz 获取曲目号"""
    data = _mb_get(f"{MB_BASE}/release/{release_id}", {
        "fmt": "json", "inc": "recordings"
    })
    if data:
        for media in data.get("media", []):
            for t in media.get("tracks", []):
                if t.get("recording", {}).get("id") == recording_id:
                    return str(t.get("number", ""))
    return ""


def update_tags(filepath, match):
    """写入标签（调用 common.write_tags）"""
    release = match.get("release", {})
    track_num = ""
    if release.get("id"):
        track_num = _get_track_number_from_mb(release["id"], match.get("recording_id", ""))

    return write_tags(
        filepath,
        title=match.get("recording_title"),
        artist=", ".join(a.get("name", "") for a in match.get("artists", [])),
        album=release.get("title"),
        date=release.get("date"),
        tracknumber=track_num or None,
        mb_track_id=match.get("recording_id"),
        mb_album_id=release.get("id"),
    )


def _collect_files(music_root, resume_set):
    """扫描目录，收集需要处理的文件"""
    files = []
    try:
        artist_dirs = sorted(os.listdir(music_root))
    except PermissionError:
        log(f"警告: 无权限读取 {music_root}")
        return files

    for artist_dir in artist_dirs:
        artist_path = os.path.join(music_root, artist_dir)
        if not os.path.isdir(artist_path) or artist_dir.startswith("_"):
            continue
        try:
            album_dirs = sorted(os.listdir(artist_path))
        except PermissionError:
            log(f"警告: 无权限读取 {artist_path}")
            continue
        for album_dir in album_dirs:
            album_path = os.path.join(artist_path, album_dir)
            if not os.path.isdir(album_path):
                continue
            try:
                fnames = os.listdir(album_path)
            except PermissionError:
                log(f"警告: 无权限读取 {album_path}")
                continue
            for fname in fnames:
                if fname.lower().endswith(AUDIO_EXTS):
                    fpath = os.path.join(album_path, fname)
                    if has_mbid(fpath):
                        continue
                    if os.path.abspath(fpath) in resume_set:
                        continue
                    files.append((artist_dir, album_dir, fname, fpath))
    return files


def main():
    parser = argparse.ArgumentParser(
        description="用声纹识别 (AcoustID) 匹配音乐标签"
    )
    parser.add_argument("music_root", help="音乐目录路径")
    parser.add_argument("--fpcalc", help="fpcalc 可执行文件路径")
    parser.add_argument("--acoustid-key", default=DEFAULT_ACOUSTID_KEY, help="AcoustID API key")
    parser.add_argument("--workers", type=int, default=4, help="并行线程数")
    parser.add_argument("--resume", action="store_true", help="断点续跑：跳过上次已处理的文件")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    parser.add_argument("--verbose", action="store_true", help="详细模式")
    parser.add_argument("--log-file", help="日志输出文件")
    args = parser.parse_args()

    music_root = args.music_root
    if not os.path.isdir(music_root):
        print(f"错误: 目录不存在: {music_root}", flush=True)
        sys.exit(1)

    _config["acoustid_key"] = args.acoustid_key
    _config["dry_run"] = args.dry_run
    _config["quiet"] = args.quiet
    _config["verbose"] = args.verbose

    set_quiet(args.quiet)
    set_verbose(args.verbose)
    if args.log_file:
        set_log_file(args.log_file)

    # 查找 fpcalc
    fpcalc_path = args.fpcalc or find_fpcalc()
    if not fpcalc_path:
        log("错误: 找不到 fpcalc。请用 --fpcalc 指定路径，或安装 chromaprint:")
        log("  Windows: https://github.com/acoustid/chromaprint/releases")
        log("  macOS:   brew install chromaprint")
        log("  Linux:   apt install libchromaprint-tools")
        sys.exit(1)
    if not os.path.isfile(fpcalc_path):
        log(f"错误: fpcalc 不存在: {fpcalc_path}")
        sys.exit(1)

    _config["fpcalc"] = fpcalc_path

    # 验证 fpcalc 可用（不检查退出码，只检查能否执行）
    try:
        subprocess.run(
            [fpcalc_path, "-version"], capture_output=True, timeout=10
        )
    except FileNotFoundError:
        log(f"错误: fpcalc 无法运行: {fpcalc_path}")
        sys.exit(1)

    if args.dry_run:
        log("[DRY-RUN 模式] 不会写入任何标签")
    log(f"fpcalc: {fpcalc_path}")
    log(f"线程数: {args.workers}")

    # 断点续跑
    progress_path = os.path.join(music_root, PROGRESS_FILE)
    resume_set = set()
    if args.resume:
        resume_set = load_progress(progress_path)
        if resume_set:
            log(f"断点续跑: 上次已处理 {len(resume_set)} 个文件，将跳过")

    # 扫描文件
    files = _collect_files(music_root, resume_set)
    total = len(files)
    if total == 0:
        log("没有找到需要处理的文件")
        return

    est_sec = total * 0.5 / args.workers
    log(f"找到 {total} 个文件需要处理")
    log(f"预估时间: {est_sec / 60:.0f} 分钟")
    log("")

    progress = ProgressBar(total, desc="声纹识别", quiet=args.quiet)

    tasks = [(i, total, a, al, f, fp) for i, (a, al, f, fp) in enumerate(files)]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, t): t for t in tasks}
        for future in as_completed(futures):
            task_info = futures[future]
            fpath = task_info[5]  # filepath
            try:
                status, msg = future.result()
                if status == "match":
                    progress.update("match")
                    mark_done(progress_path, resume_set, fpath)
                elif status == "dry-run":
                    progress.update("match")
                elif status == "skip":
                    progress.update("skip")
                else:
                    progress.update("fail")
                    mark_done(progress_path, resume_set, fpath)
            except RuntimeError as e:
                print(f"\n致命错误: {e}", flush=True)
                sys.exit(1)
            except Exception as e:
                progress.update("fail")
                mark_done(progress_path, resume_set, fpath)

    progress.finish()

    count = progress.matched
    log_always(f"成功率: {count * 100 // max(total, 1)}%")


if __name__ == "__main__":
    main()
