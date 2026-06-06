"""
common.py — 公共工具函数
"""

import os
import mutagen
from mutagen.mp4 import MP4Tags


AUDIO_EXTS = (".flac", ".mp3", ".m4a")


def log(msg):
    print(msg, flush=True)


def has_mbid(filepath):
    """检查文件是否已有 MusicBrainz Album ID"""
    try:
        f = mutagen.File(filepath, easy=False)
        if f is None or not hasattr(f, "tags") or not f.tags:
            return False
        if isinstance(f.tags, mutagen.flac.VCFLACDict):
            return bool(f.tags.get("musicbrainz_albumid"))
        elif isinstance(f.tags, mutagen.id3.ID3):
            # ID3 键格式: "TXXX:MusicBrainz Album Id"（含空格）
            return any("musicbrainz" in k.lower() and "album" in k.lower() for k in f.tags)
        elif isinstance(f.tags, MP4Tags):
            return any("musicbrainz" in k.lower() and "album" in k.lower() for k in f.tags)
    except Exception:
        pass
    return False


def collect_files(music_root):
    """扫描目录，收集所有没有 MBID 的音频文件"""
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
                    if not has_mbid(fpath):
                        files.append((artist_dir, album_dir, fname, fpath))
    return files


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    import re
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()
