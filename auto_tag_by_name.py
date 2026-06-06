"""
auto_tag_by_name.py — 按歌手文件夹名匹配 MusicBrainz 标签

用法:
    python auto_tag_by_name.py /path/to/music
    python auto_tag_by_name.py /path/to/music --artist-map artists.yaml
    python auto_tag_by_name.py /path/to/music --start-from "周杰伦"
"""

import sys
import os
import re
import time
import argparse
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Tags
import requests

from common import has_mbid, AUDIO_EXTS, log, log_verbose, set_log_file

MB_BASE = "https://musicbrainz.org/ws/2"
HEADERS = {"User-Agent": "music-tagger/1.0 (https://github.com/user/music-tagger)"}
RATE_LIMIT = 1.1

# 全局配置，由 main() 设置
_config = {"dry_run": False, "quiet": False, "verbose": False}


def load_artist_map(path):
    """从 YAML 文件加载歌手名映射"""
    if not path:
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _mb_get(url, params, retries=3):
    for attempt in range(retries):
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
            else:
                return None
    return None


def mb_search_artist(name):
    data = _mb_get(f"{MB_BASE}/artist", {"query": name, "fmt": "json", "limit": 1})
    if data and data.get("artists"):
        a = data["artists"][0]
        return a["name"], a["id"]
    return None


def mb_search_album(artist_mbid, album_name):
    query = f'arid:{artist_mbid} AND release:"{album_name}"'
    data = _mb_get(f"{MB_BASE}/release", {"query": query, "fmt": "json", "limit": 5})
    return data.get("releases", []) if data else []


def mb_get_release(mbid):
    return _mb_get(f"{MB_BASE}/release/{mbid}", {"fmt": "json", "inc": "recordings+artist-credits"})


def clean_album_name(dirname):
    if "MOOFEEL" in dirname or "磨坊" in dirname:
        return None
    if re.match(r"^CD\d+$", dirname.strip()):
        return None
    name = re.sub(r"^\d{4}(-\d{2}){0,2}\s+", "", dirname)
    name = re.sub(r"\s*\[.*?\]\s*", "", name)
    name = re.sub(r"\s*\(.*?(\d+CD|WAV|CUE).*?\)\s*", "", name)
    name = re.sub(r"\.CD\d+$", "", name)
    name = re.sub(r"\s*（EP）\s*$", "", name)
    name = re.sub(r"\s*\(EP\)\s*$", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def get_track_number(filename):
    m = re.match(r"^(\d{1,3})\s*[-.]\s*", filename)
    return int(m.group(1)) if m else None


def get_title_from_filename(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r"^\d{1,3}\s*[-.]\s*", "", name)
    return name.strip()



def update_flac_tags(filepath, track_info, release_info):
    f = FLAC(filepath)
    if f.tags is None:
        f.add_vorbis_comment()
    artist_credit = release_info.get("artist-credit", [{}])
    artist_name = artist_credit[0].get("name", "") if artist_credit else ""
    f.tags["title"] = track_info.get("title", "")
    f.tags["artist"] = artist_name
    f.tags["album"] = release_info.get("title", "")
    f.tags["date"] = release_info.get("date", "")
    f.tags["tracknumber"] = str(track_info.get("number", ""))
    f.tags["musicbrainz_trackid"] = track_info.get("id", "")
    f.tags["musicbrainz_albumid"] = release_info.get("id", "")
    f.save()


def update_mp3_tags(filepath, track_info, release_info):
    try:
        audio = MP3(filepath, ID3=ID3)
    except Exception:
        audio = MP3(filepath)
        audio.add_tags()
    artist_credit = release_info.get("artist-credit", [{}])
    artist_name = artist_credit[0].get("name", "") if artist_credit else ""
    audio.tags.delall("TIT2")
    audio.tags.add(TIT2(encoding=3, text=track_info.get("title", "")))
    audio.tags.delall("TPE1")
    audio.tags.add(TPE1(encoding=3, text=artist_name))
    audio.tags.delall("TALB")
    audio.tags.add(TALB(encoding=3, text=release_info.get("title", "")))
    audio.tags.delall("TDRC")
    audio.tags.add(TDRC(encoding=3, text=release_info.get("date", "")))
    audio.tags.delall("TRCK")
    audio.tags.add(TRCK(encoding=3, text=str(track_info.get("number", ""))))
    # MusicBrainz ID 写入 TXXX 帧
    if track_info.get("id"):
        audio.tags.delall("TXXX:MusicBrainz Track Id")
        audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=track_info["id"]))
    if release_info.get("id"):
        audio.tags.delall("TXXX:MusicBrainz Album Id")
        audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=release_info["id"]))
    audio.save()


def update_m4a_tags(filepath, track_info, release_info):
    audio = MP4(filepath)
    if audio.tags is None:
        audio.add_tags()
    artist_credit = release_info.get("artist-credit", [{}])
    artist_name = artist_credit[0].get("name", "") if artist_credit else ""
    audio.tags["\xa9nam"] = [track_info.get("title", "")]
    audio.tags["\xa9ART"] = [artist_name]
    audio.tags["\xa9alb"] = [release_info.get("title", "")]
    if release_info.get("date"):
        audio.tags["\xa9day"] = [release_info["date"]]
    if track_info.get("number"):
        try:
            audio.tags["trkn"] = [(int(str(track_info["number"]).split("/")[0]), 0)]
        except (ValueError, AttributeError):
            pass
    if track_info.get("id"):
        audio.tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [track_info["id"].encode()]
    if release_info.get("id"):
        audio.tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [release_info["id"].encode()]
    audio.save()


def match_tracks_to_release(files, release):
    media = release.get("media", [])
    if not media:
        return {}
    tracks = [t for m in media for t in m.get("tracks", [])]

    matches = {}
    unmatched = []
    for fpath in files:
        track_num = get_track_number(os.path.basename(fpath))
        matched = False
        if track_num:
            for t in tracks:
                if t.get("number") == str(track_num) or t.get("position") == track_num:
                    matches[fpath] = t
                    matched = True
                    break
        if not matched:
            unmatched.append(fpath)

    for fpath in unmatched:
        local_title = get_title_from_filename(os.path.basename(fpath))
        if not local_title:
            continue
        for t in tracks:
            if t["title"] == local_title:
                matches[fpath] = t
                break
        else:
            local_clean = local_title.replace(" ", "").replace("　", "")
            for t in tracks:
                if t["title"].replace(" ", "").replace("　", "") == local_clean:
                    matches[fpath] = t
                    break
    return matches


def process_album(artist_folder, album_folder, files, artist_map):
    clean_name = clean_album_name(album_folder)
    if not clean_name:
        return 0, "skip"

    artist_info = artist_map.get(artist_folder)
    if not artist_info:
        return 0, "unknown_artist"

    artist_mbid = None
    for search_name in artist_info:
        result = mb_search_artist(search_name)
        if result:
            _, artist_mbid = result
            break
        time.sleep(RATE_LIMIT)

    if not artist_mbid:
        return 0, "artist_not_found"

    releases = mb_search_album(artist_mbid, clean_name)
    time.sleep(RATE_LIMIT)

    if not releases:
        simpler = re.sub(r"\s*\(.*?\)\s*", "", clean_name)
        if simpler != clean_name:
            releases = mb_search_album(artist_mbid, simpler)
            time.sleep(RATE_LIMIT)

    if not releases:
        return 0, "no_match"

    best = next((r for r in releases if r.get("status") == "official"), releases[0])
    release_detail = mb_get_release(best["id"])
    time.sleep(RATE_LIMIT)

    if not release_detail:
        return 0, "release_error"

    matches = match_tracks_to_release(files, release_detail)
    updated = 0
    for fpath, track_info in matches.items():
        fname = os.path.basename(fpath)
        if _config.get("dry_run"):
            title = track_info.get("title", "?")
            artist_credit = release_detail.get("artist-credit", [{}])
            artist = artist_credit[0].get("name", "") if artist_credit else ""
            album = release_detail.get("title", "?")
            log(f"  [DRY-RUN] {fname} -> {artist} - {title} [{album}]")
            updated += 1
            continue
        try:
            ext = os.path.splitext(fpath)[1].lower()
            if ext == ".flac":
                update_flac_tags(fpath, track_info, release_detail)
            elif ext == ".mp3":
                update_mp3_tags(fpath, track_info, release_detail)
            elif ext == ".m4a":
                update_m4a_tags(fpath, track_info, release_detail)
            else:
                log(f"  跳过不支持的格式: {fname}")
                continue
            updated += 1
        except Exception as e:
            log(f"  ERR updating {fname}: {e}")
    return updated, "ok"


def collect_files(music_root):
    """扫描目录，收集所有没有 MBID 的音频文件"""
    albums = []  # [(artist_dir, album_dir, [file_paths])]
    for artist_dir in sorted(os.listdir(music_root)):
        artist_path = os.path.join(music_root, artist_dir)
        if not os.path.isdir(artist_path) or artist_dir.startswith("_"):
            continue
        for album_dir in sorted(os.listdir(artist_path)):
            album_path = os.path.join(artist_path, album_dir)
            if not os.path.isdir(album_path):
                continue
            files = []
            for fname in os.listdir(album_path):
                if fname.lower().endswith(AUDIO_EXTS):
                    fpath = os.path.join(album_path, fname)
                    if not has_mbid(fpath):
                        files.append(fpath)
            if files:
                albums.append((artist_dir, album_dir, files))
    return albums


def main():
    parser = argparse.ArgumentParser(
        description="按歌手文件夹名搜 MusicBrainz，匹配专辑并更新标签"
    )
    parser.add_argument("music_root", help="音乐目录路径")
    parser.add_argument(
        "--artist-map", dest="artist_map",
        help="歌手名映射文件（YAML 格式），不指定则用文件夹名直接搜索"
    )
    parser.add_argument(
        "--start-from", dest="start_from",
        help="从指定歌手开始处理（断点续跑）"
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
        help="详细模式：输出 MusicBrainz 查询等调试信息"
    )
    parser.add_argument("--log-file", help="日志输出文件")
    args = parser.parse_args()

    music_root = args.music_root
    if not os.path.isdir(music_root):
        print(f"错误: 目录不存在: {music_root}", flush=True)
        sys.exit(1)

    _config["dry_run"] = args.dry_run
    _config["quiet"] = args.quiet
    _config["verbose"] = args.verbose

    if args.log_file:
        set_log_file(args.log_file)

    if args.dry_run:
        log("[DRY-RUN 模式] 不会写入任何标签")

    artist_map = load_artist_map(args.artist_map) if args.artist_map else {}
    albums = collect_files(music_root)

    if not albums:
        log("没有找到需要处理的文件")
        return

    total_files = sum(len(files) for _, _, files in albums)
    log(f"找到 {len(albums)} 个专辑，共 {total_files} 个文件需要处理")
    if artist_map:
        log(f"已加载 {len(artist_map)} 个歌手映射")
    log("")

    started = args.start_from is None
    updated_total = 0
    skipped_albums = 0

    for i, (artist_dir, album_dir, files) in enumerate(albums):
        if not started:
            if artist_dir == args.start_from:
                started = True
            else:
                continue

        if artist_map and artist_dir not in artist_map:
            log(f"[{i+1}/{len(albums)}] SKIP (不在映射中): {artist_dir}")
            continue

        log(f"[{i+1}/{len(albums)}] {artist_dir} / {album_dir} ({len(files)} 文件)")
        updated, status = process_album(artist_dir, album_dir, files, artist_map or {})

        if updated > 0:
            updated_total += updated
            action = "可匹配" if args.dry_run else "更新"
            log(f"  -> {action} {updated} 个文件")
        elif status == "skip":
            log(f"  -> 跳过（水印/通用目录名）")
            skipped_albums += 1
        elif status == "unknown_artist":
            log(f"  -> 跳过（无歌手映射）")
        elif status == "no_match":
            log(f"  -> 未匹配到专辑")
        elif status == "artist_not_found":
            log(f"  -> MusicBrainz 找不到该歌手")

    # 始终输出统计（即使 quiet 模式）
    print(f"\n{'='*50}", flush=True)
    print(f"完成!", flush=True)
    print(f"总文件数: {total_files}", flush=True)
    print(f"已更新: {updated_total}", flush=True)
    print(f"成功率: {updated_total * 100 // max(total_files, 1)}%", flush=True)


if __name__ == "__main__":
    main()
