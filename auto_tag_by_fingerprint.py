"""
auto_tag_by_fingerprint.py — 用声纹识别匹配音乐标签

用法:
    python auto_tag_by_fingerprint.py /path/to/music
    python auto_tag_by_fingerprint.py /path/to/music --fpcalc /path/to/fpcalc
    python auto_tag_by_fingerprint.py /path/to/music --acoustid-key YOUR_KEY --workers 8
"""

import sys
import os
import re
import time
import json
import argparse
import subprocess
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Tags
import requests

from common import has_mbid, AUDIO_EXTS

DEFAULT_ACOUSTID_KEY = "1vOwZtEn"
MB_BASE = "https://musicbrainz.org/ws/2"
HEADERS = {"User-Agent": "music-tagger/1.0 (https://github.com/user/music-tagger)"}

# 全局配置，由 main() 设置
_config = {"fpcalc": "fpcalc", "acoustid_key": DEFAULT_ACOUSTID_KEY, "dry_run": False, "quiet": False, "verbose": False}

# Rate limiter: 在锁内只记录时间，不在锁内 sleep
_rate_lock = threading.Lock()
_request_times = []


def log(msg):
    if not _config.get("quiet"):
        print(msg, flush=True)


def log_verbose(msg):
    if _config.get("verbose"):
        print(msg, flush=True)


def rate_limit(max_per_sec=3):
    """确保请求不超过 max_per_sec 次/秒。锁内完成检查、等待、记录。"""
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
    """自动查找 fpcalc 可执行文件"""
    found = shutil.which("fpcalc")
    if found:
        return found

    candidates = [
        os.path.join(os.environ.get("TEMP", ""), "chromaprint-fpcalc-1.5.1-windows-x86_64", "fpcalc.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "chromaprint", "fpcalc.exe"),
        "/usr/local/bin/fpcalc",
        "/opt/homebrew/bin/fpcalc",
        "/usr/bin/fpcalc",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def get_fingerprint(filepath):
    """用 fpcalc 生成音频指纹。失败返回 (None, None)，fpcalc 缺失抛出异常。"""
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
        raise RuntimeError(
            "找不到 fpcalc，请用 --fpcalc 指定路径或安装 chromaprint"
        )
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


def lookup_acoustid(duration, fingerprint):
    """查询 AcoustID"""
    return _acoustid_post(duration, fingerprint)


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
    # 降级：接受没有 release 信息的匹配
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
    """写入标签。支持 .flac / .mp3 / .m4a"""
    ext = os.path.splitext(filepath)[1].lower()
    title = match.get("recording_title", "")
    artist = ", ".join(a.get("name", "") for a in match.get("artists", []))
    release = match.get("release", {})
    album = release.get("title", "")
    date = release.get("date", "")
    track_num = ""

    if release.get("id"):
        track_num = _get_track_number_from_mb(release["id"], match.get("recording_id", ""))

    try:
        if ext == ".flac":
            f = FLAC(filepath)
            if f.tags is None:
                f.add_vorbis_comment()
            f.tags["title"] = title
            f.tags["artist"] = artist
            f.tags["album"] = album
            if date:
                f.tags["date"] = date
            if track_num:
                f.tags["tracknumber"] = track_num
            if match.get("recording_id"):
                f.tags["musicbrainz_trackid"] = match["recording_id"]
            if release.get("id"):
                f.tags["musicbrainz_albumid"] = release["id"]
            f.save()

        elif ext == ".mp3":
            try:
                audio = MP3(filepath, ID3=ID3)
            except Exception:
                audio = MP3(filepath)
                audio.add_tags()
            audio.tags.delall("TIT2")
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.delall("TPE1")
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.delall("TALB")
            audio.tags.add(TALB(encoding=3, text=album))
            if date:
                audio.tags.delall("TDRC")
                audio.tags.add(TDRC(encoding=3, text=date))
            if track_num:
                audio.tags.delall("TRCK")
                audio.tags.add(TRCK(encoding=3, text=track_num))
            # MusicBrainz ID 写入 TXXX 帧
            if match.get("recording_id"):
                audio.tags.delall("TXXX:MusicBrainz Track Id")
                audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=match["recording_id"]))
            if release.get("id"):
                audio.tags.delall("TXXX:MusicBrainz Album Id")
                audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=release["id"]))
            audio.save()

        elif ext == ".m4a":
            audio = MP4(filepath)
            if audio.tags is None:
                audio.add_tags()
            audio.tags["\xa9nam"] = [title]
            audio.tags["\xa9ART"] = [artist]
            audio.tags["\xa9alb"] = [album]
            if date:
                audio.tags["\xa9day"] = [date]
            if track_num:
                try:
                    audio.tags["trkn"] = [(int(track_num.split("/")[0]), 0)]
                except (ValueError, AttributeError):
                    pass
            if match.get("recording_id"):
                audio.tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [match["recording_id"].encode()]
            if release.get("id"):
                audio.tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [release["id"].encode()]
            audio.save()

        else:
            log(f"  不支持的格式: {ext}")
            return False

        return True
    except Exception as e:
        log(f"  标签写入错误: {e}")
        return False


def process_one(args):
    """处理单个文件"""
    i, total, artist_dir, album_dir, fname, fpath = args
    label = f"[{i+1}/{total}]"

    duration, fp = get_fingerprint(fpath)
    if not fp:
        return ("skip", f"{label} {artist_dir}/{fname} - 指纹生成失败")

    log_verbose(f"  {label} 指纹长度: {len(fp)}, 时长: {duration}s")

    results = lookup_acoustid(duration, fp)
    if not results:
        return ("fail", f"{label} {artist_dir}/{fname} - 未匹配")

    log_verbose(f"  {label} AcoustID 返回 {len(results)} 个结果")

    match = get_best_match(results)
    if not match:
        return ("fail", f"{label} {artist_dir}/{fname} - 置信度太低")

    score = match.get("score", 0)
    title = match.get("recording_title", "?")
    artist = ", ".join(a.get("name", "") for a in match.get("artists", []))
    album = match.get("release", {}).get("title", "?")

    if _config.get("dry_run"):
        return ("dry-run", f"{label} {artist_dir}/{fname} -> ({score:.0%}) {artist} - {title} [{album}] [DRY-RUN]")

    if update_tags(fpath, match):
        return ("match", f"{label} {artist_dir}/{fname} -> ({score:.0%}) {artist} - {title}")
    else:
        return ("fail", f"{label} {artist_dir}/{fname} - 标签写入失败")


def collect_files(music_root):
    """扫描目录，收集所有没有 MBID 的音频文件"""
    files = []
    for artist_dir in sorted(os.listdir(music_root)):
        artist_path = os.path.join(music_root, artist_dir)
        if not os.path.isdir(artist_path) or artist_dir.startswith("_"):
            continue
        for album_dir in sorted(os.listdir(artist_path)):
            album_path = os.path.join(artist_path, album_dir)
            if not os.path.isdir(album_path):
                continue
            for fname in os.listdir(album_path):
                if fname.lower().endswith(AUDIO_EXTS):
                    fpath = os.path.join(album_path, fname)
                    if not has_mbid(fpath):
                        files.append((artist_dir, album_dir, fname, fpath))
    return files


def main():
    parser = argparse.ArgumentParser(
        description="用声纹识别 (AcoustID) 匹配音乐标签"
    )
    parser.add_argument("music_root", help="音乐目录路径")
    parser.add_argument(
        "--fpcalc", dest="fpcalc",
        help="fpcalc 可执行文件路径（不指定则自动查找）"
    )
    parser.add_argument(
        "--acoustid-key", dest="acoustid_key", default=DEFAULT_ACOUSTID_KEY,
        help="AcoustID API key（默认使用 beets 公共 key）"
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="并行线程数（默认 4，注意 AcoustID 限流 3 次/秒）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="试运行：只输出匹配结果，不写入标签"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="安静模式：只输出最终统计"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="详细模式：输出 API 请求等调试信息"
    )
    args = parser.parse_args()

    music_root = args.music_root
    if not os.path.isdir(music_root):
        log(f"错误: 目录不存在: {music_root}")
        sys.exit(1)

    # 查找 fpcalc
    fpcalc_path = args.fpcalc or find_fpcalc()
    if not fpcalc_path:
        log("错误: 找不到 fpcalc。请用 --fpcalc 指定路径，或安装 chromaprint:")
        log("  Windows: 从 https://github.com/acoustid/chromaprint/releases 下载")
        log("  macOS:   brew install chromaprint")
        log("  Linux:   apt install libchromaprint-tools")
        sys.exit(1)
    if not os.path.isfile(fpcalc_path):
        log(f"错误: fpcalc 不存在: {fpcalc_path}")
        sys.exit(1)

    _config["fpcalc"] = fpcalc_path
    _config["acoustid_key"] = args.acoustid_key
    _config["dry_run"] = args.dry_run
    _config["quiet"] = args.quiet
    _config["verbose"] = args.verbose

    if args.dry_run:
        log("[DRY-RUN 模式] 不会写入任何标签")
    log(f"fpcalc: {fpcalc_path}")
    log(f"线程数: {args.workers}")

    # 启动前验证 fpcalc 可用
    test_result = subprocess.run(
        [_config["fpcalc"], "-version"],
        capture_output=True, timeout=10
    )
    if test_result.returncode != 0:
        print(f"错误: fpcalc 无法运行: {fpcalc_path}", flush=True)
        sys.exit(1)

    files = collect_files(music_root)
    total = len(files)
    if total == 0:
        log("没有找到需要处理的文件（所有文件已有 MusicBrainz 标签）")
        return

    est_sec = total * 0.5 / args.workers
    log(f"找到 {total} 个文件需要处理")
    log(f"预估时间: {est_sec / 60:.0f} 分钟")
    log("")

    matched = 0
    failed = 0
    skipped = 0
    dry_run = 0

    tasks = [(i, total, a, al, f, fp) for i, (a, al, f, fp) in enumerate(files)]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, t): t for t in tasks}
        for future in as_completed(futures):
            try:
                status, msg = future.result()
                log(msg)
                if status == "match":
                    matched += 1
                elif status == "dry-run":
                    dry_run += 1
                elif status == "skip":
                    skipped += 1
                else:
                    failed += 1
            except RuntimeError as e:
                print(f"致命错误: {e}", flush=True)
                sys.exit(1)
            except Exception as e:
                log(f"  错误: {e}")
                failed += 1

    # 始终输出统计（即使 quiet 模式）
    print(f"\n{'='*50}", flush=True)
    print(f"完成!", flush=True)
    print(f"总文件数: {total}", flush=True)
    if args.dry_run:
        print(f"可匹配: {dry_run}", flush=True)
    else:
        print(f"已匹配: {matched}", flush=True)
    print(f"未匹配: {failed}", flush=True)
    print(f"跳过: {skipped}", flush=True)
    count = dry_run if args.dry_run else matched
    print(f"成功率: {count * 100 // max(total, 1)}%", flush=True)


if __name__ == "__main__":
    main()
