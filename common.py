"""
common.py — 公共工具函数
"""

import os
import re
import sys
import time
import json
import threading
import mutagen
from mutagen.mp4 import MP4Tags


AUDIO_EXTS = (".flac", ".mp3", ".m4a")


# ==================== 日志 ====================

_log_file = None


def set_log_file(path):
    """设置日志输出文件"""
    global _log_file
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _log_file = open(path, "w", encoding="utf-8")


def log(msg):
    print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


def log_verbose(msg, verbose=False):
    if verbose:
        log(msg)


# ==================== 进度条 ====================

class ProgressBar:
    """简单的进度条，支持 ETA"""

    def __init__(self, total, desc="处理中", quiet=False):
        self.total = total
        self.desc = desc
        self.quiet = quiet
        self.current = 0
        self.matched = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = time.time()
        self._lock = threading.Lock()

    def update(self, status="match"):
        with self._lock:
            self.current += 1
            if status == "match":
                self.matched += 1
            elif status == "skip":
                self.skipped += 1
            else:
                self.failed += 1
            if not self.quiet:
                self._print()

    def _print(self):
        elapsed = time.time() - self.start_time
        pct = self.current * 100 // max(self.total, 1)
        if self.current > 0:
            eta = elapsed / self.current * (self.total - self.current)
            eta_str = _fmt_time(eta)
        else:
            eta_str = "??:??"

        bar_len = 25
        filled = bar_len * self.current // max(self.total, 1)
        bar = "█" * filled + "░" * (bar_len - filled)

        line = (
            f"\r  {self.desc} |{bar}| "
            f"{self.current}/{self.total} ({pct}%) "
            f"✓{self.matched} ✗{self.failed} ⏭{self.skipped} "
            f"ETA {eta_str}  "
        )
        sys.stdout.write(line)
        sys.stdout.flush()

    def finish(self):
        if not self.quiet:
            sys.stdout.write("\n")
            sys.stdout.flush()
        elapsed = time.time() - self.start_time
        log(f"  {self.desc}完成: {self.current} 个文件, 耗时 {_fmt_time(elapsed)}")
        log(f"  匹配: {self.matched}, 失败: {self.failed}, 跳过: {self.skipped}")


def _fmt_time(seconds):
    """格式化秒数为 mm:ss 或 hh:mm:ss"""
    if seconds < 0:
        return "??:??"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60:02d}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ==================== 配置文件 ====================

def load_config(config_path=None):
    """加载 YAML 配置文件，返回 dict"""
    if config_path and os.path.isfile(config_path):
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    # 尝试默认位置
    for default in ["config.yaml", "config.yml"]:
        if os.path.isfile(default):
            import yaml
            with open(default, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    return {}


def merge_args(config, args):
    """合并配置文件和命令行参数。命令行参数优先。"""
    result = dict(config)
    # 只覆盖非 None 的命令行参数
    for key, val in vars(args).items():
        if val is not None and val is not False and val != []:
            result[key] = val
        elif key not in result:
            result[key] = val
    return result


# ==================== 音频工具 ====================

def has_mbid(filepath):
    """检查文件是否已有 MusicBrainz Album ID"""
    try:
        f = mutagen.File(filepath, easy=False)
        if f is None or not hasattr(f, "tags") or not f.tags:
            return False
        if isinstance(f.tags, mutagen.flac.VCFLACDict):
            return bool(f.tags.get("musicbrainz_albumid"))
        elif isinstance(f.tags, mutagen.id3.ID3):
            return any("musicbrainz" in k.lower() and "album" in k.lower() for k in f.tags)
        elif isinstance(f.tags, MP4Tags):
            return any("musicbrainz" in k.lower() and "album" in k.lower() for k in f.tags)
    except Exception:
        pass
    return False


def collect_files(music_root, filter_mbid=True):
    """扫描目录，收集音频文件。filter_mbid=True 时只收集没有 MBID 的。"""
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
                    if not filter_mbid or not has_mbid(fpath):
                        files.append((artist_dir, album_dir, fname, fpath))
    return files


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


# ==================== 断点续跑 ====================

def load_progress(progress_file):
    """加载已完成的文件列表"""
    if os.path.isfile(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_progress(progress_file, done_set):
    """保存已完成的文件列表"""
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(list(done_set), f)


def mark_done(progress_file, done_set, filepath):
    """标记一个文件为已完成"""
    done_set.add(os.path.abspath(filepath))
    save_progress(progress_file, done_set)
