"""
run.py — 一键运行音乐整理全流程

流程:
    1. 按歌手名匹配标签 (auto_tag_by_name)
    2. 按声纹匹配标签 (auto_tag_by_fingerprint)
    3. 整理音乐库 (auto_organize)

用法:
    python run.py /path/to/music
    python run.py /path/to/music --resume
    python run.py /path/to/music --dry-run
    python run.py /path/to/music --skip-fingerprint
"""

import sys
import os
import argparse
import time

from common import _fmt_time


def section(msg):
    print(f"\n{'='*50}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'='*50}", flush=True)


def run_step(name, script_name, args_list):
    """运行一个步骤"""
    import subprocess
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, script_name)
    cmd = [sys.executable, script_path] + args_list
    section(f"开始: {name}")
    print(f"  命令: {' '.join(cmd)}", flush=True)
    start = time.time()
    result = subprocess.run(cmd, cwd=script_dir)
    elapsed = time.time() - start
    status = "成功" if result.returncode == 0 else f"失败 (exit code {result.returncode})"
    print(f"\n  {name}: {status} ({_fmt_time(elapsed)})", flush=True)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="一键运行音乐整理全流程: 打标签 -> 声纹识别 -> 整理"
    )
    parser.add_argument("music_root", help="音乐目录路径")
    parser.add_argument("--artist-map", help="歌手名映射文件（YAML）")
    parser.add_argument("--fpcalc", help="fpcalc 可执行文件路径")
    parser.add_argument("--acoustid-key", help="AcoustID API key")
    parser.add_argument("--workers", type=int, default=4, help="声纹识别线程数")
    parser.add_argument("--resume", action="store_true", help="声纹识别断点续跑")
    parser.add_argument("--skip-tag", action="store_true", help="跳过按歌手名匹配")
    parser.add_argument("--skip-fingerprint", action="store_true", help="跳过声纹识别")
    parser.add_argument("--skip-organize", action="store_true", help="跳过文件整理")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    parser.add_argument("--verbose", action="store_true", help="详细模式")
    parser.add_argument("--log-file", help="日志输出文件")
    args = parser.parse_args()

    music_root = os.path.abspath(args.music_root)
    if not os.path.isdir(music_root):
        print(f"错误: 目录不存在: {music_root}", flush=True)
        sys.exit(1)

    total_start = time.time()
    results = {}

    # 构建通用参数
    common_args = [music_root]
    if args.dry_run:
        common_args.append("--dry-run")
    if args.quiet:
        common_args.append("--quiet")
    if args.verbose:
        common_args.append("--verbose")
    if args.log_file:
        common_args += ["--log-file", args.log_file]

    # 步骤 1: 按歌手名匹配
    if not args.skip_tag:
        tag_args = common_args[:]
        if args.artist_map:
            tag_args += ["--artist-map", args.artist_map]
        ok = run_step("按歌手名匹配标签", "auto_tag_by_name.py", tag_args)
        results["歌手名匹配"] = ok
        if not ok:
            print("\n  [警告] 步骤1失败，后续步骤可能缺少标签信息", flush=True)
    else:
        print("\n  [跳过] 按歌手名匹配", flush=True)

    # 步骤 2: 按声纹匹配
    if not args.skip_fingerprint:
        fp_args = common_args[:]
        if args.fpcalc:
            fp_args += ["--fpcalc", args.fpcalc]
        if args.acoustid_key:
            fp_args += ["--acoustid-key", args.acoustid_key]
        fp_args += ["--workers", str(args.workers)]
        if args.resume:
            fp_args.append("--resume")
        ok = run_step("声纹识别匹配标签", "auto_tag_by_fingerprint.py", fp_args)
        results["声纹识别"] = ok
        if not ok:
            print("\n  [警告] 步骤2失败，部分文件可能未匹配到标签", flush=True)
    else:
        print("\n  [跳过] 声纹识别", flush=True)

    # 步骤 3: 整理
    if not args.skip_organize:
        org_args = common_args[:]
        ok = run_step("整理音乐库", "auto_organize.py", org_args)
        results["整理"] = ok
        if not ok:
            print("\n  [警告] 步骤3失败", flush=True)
    else:
        print("\n  [跳过] 整理", flush=True)

    # 汇总
    total_elapsed = time.time() - total_start
    section("全部完成!")
    print(f"  总耗时: {_fmt_time(total_elapsed)}", flush=True)
    for step, ok in results.items():
        status = "✓ OK" if ok else "✗ FAILED"
        print(f"  {step}: {status}", flush=True)
    print(f"{'='*50}", flush=True)


if __name__ == "__main__":
    main()
