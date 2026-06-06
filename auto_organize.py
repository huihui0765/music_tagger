"""
auto_organize.py — 本地音乐库整理工具

功能:
    1. 去重保高音质 — 同一首歌保留最高音质版本
    2. 噪声分类 — 识别低码率/损坏/异常文件
    3. 元数据修正 — 统一标签格式、补全缺失信息
    4. 封面嵌入 — 将文件夹内的封面图嵌入音频文件
    5. 文件整理 — 按 歌手/专辑/曲目-歌名 重命名

用法:
    python auto_organize.py /path/to/music
    python auto_organize.py /path/to/music --dry-run
    python auto_organize.py /path/to/music --only dedup
    python auto_organize.py /path/to/music --only cover
"""

import os
import re
import sys
import shutil
import argparse
from collections import defaultdict

import mutagen

from common import AUDIO_EXTS, log, log_verbose, set_log_file, write_tags

# 全局配置
_config = {"dry_run": False, "quiet": False, "verbose": False, "noise_dir": ""}


def _in_noise_dir(dirpath):
    """检查目录是否在噪声目录内"""
    noise = _config.get("noise_dir", "")
    if not noise:
        return False
    return os.path.abspath(dirpath).startswith(noise)


# ==================== 1. 去重保高音质 ====================

FORMAT_PRIORITY = {".flac": 3, ".m4a": 2, ".mp3": 1}


def get_quality_score(filepath):
    """给音频文件打质量分。越高越好。"""
    ext = os.path.splitext(filepath)[1].lower()
    format_score = FORMAT_PRIORITY.get(ext, 0)

    try:
        f = mutagen.File(filepath)
        if f is None:
            return 0
        bitrate = getattr(f.info, "bitrate", 0) or 0
        sample_rate = getattr(f.info, "sample_rate", 0) or 0
        bits_per_sample = getattr(f.info, "bits_per_sample", 0) or 0
        # flac: bits_per_sample * sample_rate; mp3: bitrate
        if bits_per_sample > 0:
            quality = bits_per_sample * sample_rate
        else:
            quality = bitrate
        return format_score * 10_000_000 + quality
    except Exception:
        return 0


def get_song_key(filepath):
    """生成歌曲指纹: 歌手+歌名（用于判断是否同一首歌）"""
    try:
        f = mutagen.File(filepath, easy=True)
        if f is None:
            return None
        artist = (f.get("artist", [None]) or [None])[0] or ""
        title = (f.get("title", [None]) or [None])[0] or ""
        if not title:
            title = os.path.splitext(os.path.basename(filepath))[0]
            title = re.sub(r"^\d{1,3}\s*[-.]\s*", "", title)
        # artist 和 title 都为空时，用目录名+文件名避免碰撞
        if not artist and not title:
            dir_name = os.path.basename(os.path.dirname(filepath))
            fname = os.path.splitext(os.path.basename(filepath))[0]
            return f"__untitled__||{dir_name}||{fname}".lower()
        return f"{artist.lower().strip()}||{title.lower().strip()}"
    except Exception:
        return None


def dedup(root):
    """去重: 同一首歌保留最高音质版本"""
    log("\n[1] 去重扫描...")
    groups = defaultdict(list)  # song_key -> [filepath]

    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                fp = os.path.join(dirpath, f)
                key = get_song_key(fp)
                if key:
                    groups[key].append(fp)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        log("  没有发现重复文件")
        return 0

    log(f"  发现 {len(dupes)} 组重复文件")
    removed = 0

    for key, files in dupes.items():
        # 按质量排序，保留最好的
        ranked = sorted(files, key=get_quality_score, reverse=True)
        keep = ranked[0]
        losers = ranked[1:]

        artist, title = key.split("||", 1)
        log(f"  {artist} - {title}")
        log(f"    保留: {os.path.basename(keep)} ({get_quality_score(keep):,})")

        for loser in losers:
            log(f"    删除: {os.path.basename(loser)} ({get_quality_score(loser):,})")
            if not _config.get("dry_run"):
                os.remove(loser)
            removed += 1

    log(f"  共删除 {removed} 个重复文件")
    return removed


# ==================== 2. 噪声分类 ====================

LOW_BITRATE = 96000      # 低于 96kbps 算低码率
SHORT_DURATION = 30      # 低于 30 秒算异常短
TINY_SIZE = 100_000      # 低于 100KB 算异常小


def classify_noise(root, noise_dir):
    """识别低码率/损坏/异常文件，移到噪声文件夹"""
    log("\n[2] 噪声分类...")
    moved = 0

    for dirpath, _, filenames in os.walk(root):
        if os.path.abspath(dirpath).startswith(os.path.abspath(noise_dir)):
            continue
        for f in filenames:
            if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                continue
            fp = os.path.join(dirpath, f)
            problems = []

            try:
                af = mutagen.File(fp)
                if af is None:
                    problems.append("无法读取")
                else:
                    size = os.path.getsize(fp)
                    duration = af.info.length if hasattr(af.info, "length") else 0
                    bitrate = getattr(af.info, "bitrate", 0) or 0

                    if size < TINY_SIZE:
                        problems.append(f"文件过小({size // 1024}KB)")
                    if duration < SHORT_DURATION:
                        problems.append(f"时长过短({duration:.0f}s)")
                    if 0 < bitrate < LOW_BITRATE:
                        problems.append(f"低码率({bitrate // 1000}kbps)")
            except Exception as e:
                problems.append(f"读取错误: {e}")

            if problems:
                log(f"  [NOISE] {f}: {', '.join(problems)}")
                if not _config.get("dry_run"):
                    dest_dir = os.path.join(noise_dir, os.path.relpath(dirpath, root))
                    os.makedirs(dest_dir, exist_ok=True)
                    dest = os.path.join(dest_dir, f)
                    shutil.move(fp, dest)
                moved += 1

    log(f"  共移动 {moved} 个异常文件到 noise/")
    return moved


# ==================== 3. 元数据修正 ====================

def fix_metadata(root):
    """修正元数据: 补全缺失字段、统一格式"""
    log("\n[3] 元数据修正...")
    fixed = 0

    for dirpath, _, filenames in os.walk(root):
        if _in_noise_dir(dirpath):
            continue
        # 从目录名推断歌手和专辑
        rel = os.path.relpath(dirpath, root)
        parts = rel.split(os.sep)
        dir_artist = parts[0] if len(parts) >= 1 else ""
        dir_album = parts[1] if len(parts) >= 2 else ""

        for f in filenames:
            if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                continue
            fp = os.path.join(dirpath, f)
            changes = []

            try:
                af = mutagen.File(fp, easy=True)
                if af is None:
                    continue

                # 补全缺失的艺术家
                if not af.get("artist"):
                    if dir_artist and dir_artist != "Unknown":
                        af["artist"] = [dir_artist]
                        changes.append(f"artist={dir_artist}")

                # 补全缺失的专辑
                if not af.get("album"):
                    if dir_album and not dir_album.startswith("Unknown"):
                        af["album"] = [dir_album]
                        changes.append(f"album={dir_album}")

                # 从文件名补全缺失的标题
                if not af.get("title"):
                    title = os.path.splitext(f)[0]
                    title = re.sub(r"^\d{1,3}\s*[-.]\s*", "", title)
                    af["title"] = [title]
                    changes.append(f"title={title}")

                if changes:
                    log_verbose(f"  {f}: {', '.join(changes)}")
                    if not _config.get("dry_run"):
                        af.save()
                    fixed += 1

            except Exception as e:
                log_verbose(f"  {f}: 错误 {e}")

    log(f"  修正 {fixed} 个文件")
    return fixed


# ==================== 4. 封面嵌入 ====================

def embed_covers(root):
    """将文件夹内的封面图嵌入音频文件"""
    log("\n[4] 封面嵌入...")
    embedded = 0

    for dirpath, _, filenames in os.walk(root):
        if _in_noise_dir(dirpath):
            continue
        # 找封面图
        cover_file = None
        for f in filenames:
            fl = f.lower()
            if fl in ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png"):
                cover_file = os.path.join(dirpath, f)
                break
            if fl.endswith((".jpg", ".png")) and not cover_file:
                cover_file = os.path.join(dirpath, f)

        if not cover_file:
            continue

        try:
            with open(cover_file, "rb") as f:
                img_data = f.read()
        except Exception:
            continue

        ext = os.path.splitext(cover_file)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

        for f in filenames:
            if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                continue
            fp = os.path.join(dirpath, f)

            # 检查是否已有封面
            try:
                af = mutagen.File(fp, easy=False)
                if af is None:
                    continue
                has_cover = False
                if isinstance(af, mutagen.flac.FLAC):
                    has_cover = any(p.type == 3 for p in af.pictures)
                elif isinstance(af, mutagen.id3.ID3):
                    has_cover = any(k.startswith("APIC") for k in af.tags) if af.tags else False
                elif isinstance(af, mutagen.mp4.MP4):
                    has_cover = "covr" in af.tags if af.tags else False
                if has_cover:
                    continue
            except Exception:
                continue

            if _config.get("dry_run"):
                log(f"  [COVER] {f}")
                embedded += 1
                continue

            ok = write_tags(fp, cover_data=img_data, cover_mime=mime)
            if ok:
                log(f"  [COVER] {f}")
                embedded += 1

    log(f"  嵌入 {embedded} 个文件")
    return embedded


# ==================== 5. 文件整理 ====================

def sanitize(name):
    """清理文件名中的非法字符"""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def organize_files(root):
    """按 歌手/专辑/曲目-歌名 重命名"""
    log("\n[5] 文件整理...")
    moved = 0

    for dirpath, _, filenames in os.walk(root):
        if _in_noise_dir(dirpath):
            continue
        for f in filenames:
            if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                continue
            fp = os.path.join(dirpath, f)

            try:
                af = mutagen.File(fp, easy=True)
                if af is None:
                    continue

                artist = sanitize((af.get("artist", [None]) or [None])[0] or "Unknown")
                album = sanitize((af.get("album", [None]) or [None])[0] or "Unknown")
                title = sanitize((af.get("title", [None]) or [None])[0] or os.path.splitext(f)[0])
                track = (af.get("tracknumber", [""])[0] or "").split("/")[0]
                ext = os.path.splitext(f)[1].lower()

                # 构建目标路径
                if track:
                    new_name = f"{int(track):02d} - {title}{ext}"
                else:
                    new_name = f"{title}{ext}"

                target_dir = os.path.join(root, artist, album)
                target_path = os.path.join(target_dir, new_name)

                # 已经在正确位置就跳过
                if os.path.normpath(fp) == os.path.normpath(target_path):
                    continue

                if _config.get("dry_run"):
                    log(f"  [MOVE] {f} -> {artist}/{album}/{new_name}")
                    moved += 1
                    continue

                os.makedirs(target_dir, exist_ok=True)
                # 避免覆盖
                if os.path.exists(target_path):
                    log_verbose(f"  跳过(已存在): {artist}/{album}/{new_name}")
                    continue

                os.rename(fp, target_path)
                log(f"  [MOVE] {f} -> {artist}/{album}/{new_name}")
                moved += 1

            except Exception as e:
                log_verbose(f"  {f}: 错误 {e}")

    log(f"  整理 {moved} 个文件")
    return moved


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(
        description="本地音乐库整理: 去重、噪声分类、元数据修正、封面嵌入、文件整理"
    )
    parser.add_argument("music_root", help="音乐目录路径")
    parser.add_argument(
        "--only", choices=["dedup", "noise", "meta", "cover", "organize"],
        help="只执行指定步骤"
    )
    parser.add_argument(
        "--noise-dir", default=None,
        help="噪声文件夹路径（默认: 音乐目录/noise）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="试运行：只显示会做什么，不实际操作"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="安静模式：只输出最终统计"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="详细模式：输出每一步的调试信息"
    )
    parser.add_argument("--log-file", help="日志输出文件")
    args = parser.parse_args()

    root = os.path.abspath(args.music_root)
    if not os.path.isdir(root):
        print(f"错误: 目录不存在: {root}", flush=True)
        sys.exit(1)

    noise_dir = os.path.abspath(args.noise_dir or os.path.join(root, "noise"))

    _config["dry_run"] = args.dry_run
    _config["quiet"] = args.quiet
    _config["verbose"] = args.verbose
    _config["noise_dir"] = noise_dir

    if args.log_file:
        set_log_file(args.log_file)

    log("=" * 50)
    log("  Music Organizer")
    if args.dry_run:
        log("  [DRY-RUN 模式]")
    log("=" * 50)
    log(f"  目录: {root}")

    stats = {}

    steps = {
        "dedup": ("去重保高音质", lambda: dedup(root)),
        "noise": ("噪声分类", lambda: classify_noise(root, noise_dir)),
        "meta": ("元数据修正", lambda: fix_metadata(root)),
        "cover": ("封面嵌入", lambda: embed_covers(root)),
        "organize": ("文件整理", lambda: organize_files(root)),
    }

    if args.only:
        name, fn = steps[args.only]
        stats[name] = fn()
    else:
        for key in ["dedup", "noise", "meta", "cover", "organize"]:
            name, fn = steps[key]
            stats[name] = fn()

    # 汇总（始终输出）
    print(f"\n{'=' * 50}", flush=True)
    print(f"  完成!", flush=True)
    for name, count in stats.items():
        print(f"  {name}: {count}", flush=True)
    print(f"{'=' * 50}", flush=True)


if __name__ == "__main__":
    main()
