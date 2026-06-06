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
_quiet = False
_verbose = False


def set_log_file(path):
    """设置日志输出文件"""
    global _log_file
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _log_file = open(path, "w", encoding="utf-8")


def set_quiet(q):
    global _quiet
    _quiet = q


def set_verbose(v):
    global _verbose
    _verbose = v


def log(msg):
    if not _quiet:
        print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


def log_always(msg):
    """始终输出（忽略 quiet 模式），用于最终统计"""
    print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


def log_verbose(msg):
    if _verbose:
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


# ==================== 文件工具 ====================

def sanitize_filename(name):
    """清理文件名中的非法字符"""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


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


# ==================== 标签写入 ====================

def write_tags(filepath, title=None, artist=None, album=None,
               date=None, tracknumber=None, mb_track_id=None, mb_album_id=None,
               cover_data=None, cover_mime=None):
    """
    统一写入音频标签。支持 .flac / .mp3 / .m4a。
    只写入非 None 的字段。
    """
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".flac":
            _write_flac(filepath, title, artist, album, date,
                        tracknumber, mb_track_id, mb_album_id,
                        cover_data, cover_mime)
        elif ext == ".mp3":
            _write_mp3(filepath, title, artist, album, date,
                       tracknumber, mb_track_id, mb_album_id,
                       cover_data, cover_mime)
        elif ext == ".m4a":
            _write_m4a(filepath, title, artist, album, date,
                       tracknumber, mb_track_id, mb_album_id,
                       cover_data, cover_mime)
        else:
            return False
        return True
    except Exception as e:
        log(f"  标签写入错误 {os.path.basename(filepath)}: {e}")
        return False


def _write_flac(filepath, title, artist, album, date, tracknumber,
                mb_track_id, mb_album_id, cover_data, cover_mime):
    from mutagen.flac import FLAC, Picture
    f = FLAC(filepath)
    if f.tags is None:
        f.add_vorbis_comment()
    if title is not None:
        f.tags["title"] = title
    if artist is not None:
        f.tags["artist"] = artist
    if album is not None:
        f.tags["album"] = album
    if date is not None:
        f.tags["date"] = date
    if tracknumber is not None:
        f.tags["tracknumber"] = str(tracknumber)
    if mb_track_id is not None:
        f.tags["musicbrainz_trackid"] = mb_track_id
    if mb_album_id is not None:
        f.tags["musicbrainz_albumid"] = mb_album_id
    if cover_data is not None:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = cover_mime or "image/jpeg"
        pic.data = cover_data
        f.clear_pictures()
        f.add_picture(pic)
    f.save()


def _write_mp3(filepath, title, artist, album, date, tracknumber,
               mb_track_id, mb_album_id, cover_data, cover_mime):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TXXX, APIC
    try:
        audio = MP3(filepath, ID3=ID3)
    except Exception:
        audio = MP3(filepath)
        audio.add_tags()
    if title is not None:
        audio.tags.delall("TIT2")
        audio.tags.add(TIT2(encoding=3, text=title))
    if artist is not None:
        audio.tags.delall("TPE1")
        audio.tags.add(TPE1(encoding=3, text=artist))
    if album is not None:
        audio.tags.delall("TALB")
        audio.tags.add(TALB(encoding=3, text=album))
    if date is not None:
        audio.tags.delall("TDRC")
        audio.tags.add(TDRC(encoding=3, text=date))
    if tracknumber is not None:
        audio.tags.delall("TRCK")
        audio.tags.add(TRCK(encoding=3, text=str(tracknumber)))
    if mb_track_id is not None:
        audio.tags.delall("TXXX:MusicBrainz Track Id")
        audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=mb_track_id))
    if mb_album_id is not None:
        audio.tags.delall("TXXX:MusicBrainz Album Id")
        audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=mb_album_id))
    if cover_data is not None:
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=cover_mime or "image/jpeg",
                            type=3, data=cover_data))
    audio.save()


def _write_m4a(filepath, title, artist, album, date, tracknumber,
               mb_track_id, mb_album_id, cover_data, cover_mime):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(filepath)
    if audio.tags is None:
        audio.add_tags()
    if title is not None:
        audio.tags["\xa9nam"] = [title]
    if artist is not None:
        audio.tags["\xa9ART"] = [artist]
    if album is not None:
        audio.tags["\xa9alb"] = [album]
    if date is not None:
        audio.tags["\xa9day"] = [date]
    if tracknumber is not None:
        try:
            audio.tags["trkn"] = [(int(str(tracknumber).split("/")[0]), 0)]
        except (ValueError, AttributeError):
            pass
    if mb_track_id is not None:
        audio.tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [mb_track_id.encode()]
    if mb_album_id is not None:
        audio.tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [mb_album_id.encode()]
    if cover_data is not None:
        fmt = MP4Cover.FORMAT_JPEG if (cover_mime or "image/jpeg") == "image/jpeg" else MP4Cover.FORMAT_PNG
        audio.tags["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
    audio.save()


def read_tags(filepath):
    """读取音频文件的基本标签"""
    try:
        f = mutagen.File(filepath, easy=True)
        if f is None:
            return {}
        return {
            "title": (f.get("title") or [None])[0],
            "artist": (f.get("artist") or [None])[0],
            "album": (f.get("album") or [None])[0],
            "date": (f.get("date") or [None])[0],
            "tracknumber": (f.get("tracknumber") or [None])[0],
        }
    except Exception:
        return {}


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
