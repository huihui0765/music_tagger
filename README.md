# music-tagger

批量给本地音乐文件打 [MusicBrainz](https://musicbrainz.org/) 标签，然后自动整理音乐库。

支持 `.flac` / `.mp3` / `.m4a`，不依赖文件名，直接通过声纹识别歌曲。

## 功能

- **按歌手名匹配** — 用文件夹名搜 MusicBrainz，快速匹配专辑
- **声纹识别** — 用 Chromaprint/AcoustID 识别歌曲内容，不依赖文件名
- **去重保高音质** — 同一首歌保留 FLAC > M4A > MP3
- **噪声分类** — 低码率/损坏文件自动隔离
- **元数据修正** — 从目录名补全缺失的 artist/album/title
- **封面嵌入** — 自动把文件夹里的封面图嵌入音频文件
- **文件整理** — 按 `歌手/专辑/01 - 歌名.flac` 重命名

## 快速开始

```bash
pip install -r requirements.txt

# 一键运行全流程
python run.py /path/to/music

# 先看看会改什么（不实际修改文件）
python run.py /path/to/music --dry-run
```

## 工作流程

```text
run.py 一键串联三个步骤：

  步骤 1: auto_tag_by_name.py        按歌手文件夹名搜 MusicBrainz
     ↓ 没匹配到的文件
  步骤 2: auto_tag_by_fingerprint.py  用声纹识别兜底
     ↓
  步骤 3: auto_organize.py            去重、修标签、嵌封面、整理目录
```

也可以单独运行每个步骤：

```bash
python auto_tag_by_name.py /path/to/music --artist-map artists.yaml
python auto_tag_by_fingerprint.py /path/to/music
python auto_organize.py /path/to/music --only dedup
```

## 安装

```bash
pip install -r requirements.txt
```

声纹识别还需要 `fpcalc`（Chromaprint 命令行工具）：

```bash
# macOS
brew install chromaprint

# Ubuntu/Debian
apt install libchromaprint-tools

# Windows
# 从 https://github.com/acoustid/chromaprint/releases 下载
```

## 脚本说明

### auto_tag_by_name.py

读取 `歌手/专辑/文件` 目录结构，用歌手文件夹名搜 MusicBrainz，匹配后写入标签。

```bash
python auto_tag_by_name.py /path/to/music
python auto_tag_by_name.py /path/to/music --artist-map artists.yaml
python auto_tag_by_name.py /path/to/music --start-from "周杰伦"  # 断点续跑
```

### auto_tag_by_fingerprint.py

用 Chromaprint 生成音频指纹，发到 AcoustID 服务器识别。不依赖文件名或目录结构。

```bash
python auto_tag_by_fingerprint.py /path/to/music
python auto_tag_by_fingerprint.py /path/to/music --fpcalc /path/to/fpcalc
python auto_tag_by_fingerprint.py /path/to/music --workers 8
```

### auto_organize.py

| 步骤 | 功能 | 说明 |
| --- | --- | --- |
| dedup | 去重保高音质 | 同一首歌保留 FLAC > M4A > MP3 |
| noise | 噪声分类 | 低码率/损坏文件移到 `noise/` |
| meta | 元数据修正 | 从目录名补全缺失的 artist/album/title |
| cover | 封面嵌入 | 把文件夹里的封面图嵌入音频 |
| organize | 文件整理 | 按 `歌手/专辑/01 - 歌名.flac` 重命名 |

```bash
python auto_organize.py /path/to/music
python auto_organize.py /path/to/music --only dedup   # 只跑某一步
python auto_organize.py /path/to/music --dry-run       # 试运行
```

## 通用参数

三个脚本都支持：

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 试运行，只看不改 |
| `--quiet` | 安静模式，只输出统计 |
| `--verbose` | 详细模式，输出调试信息 |
| `--log-file FILE` | 日志输出到文件 |

## 进度条

脚本运行时会显示实时进度条：

```text
  声纹识别 |████████████░░░░░░░░░░░░░░| 1200/3021 (39%) ✓1050 ✗120 ⏭30 ETA 12:30
```

- `✓` 匹配成功
- `✗` 匹配失败
- `⏭` 跳过（已有标签/指纹失败）
- `ETA` 预估剩余时间

## 断点续跑

声纹识别跑到一半断了？用 `--resume` 接着来：

```bash
# 第一次跑（中断了）
python auto_tag_by_fingerprint.py /path/to/music

# 接着跑（跳过已处理的文件）
python auto_tag_by_fingerprint.py /path/to/music --resume
```

进度文件保存在音乐目录下的 `.fingerprint_progress.json`。

```bash
# 用 run.py 也可以
python run.py /path/to/music --resume
```

## 歌手名映射

`artists.example.yaml` 是模板，复制改名后使用：

```bash
cp artists.example.yaml artists.yaml
```

格式：

```yaml
"周杰伦":
  - "周杰伦"
  - "周杰倫"
  - "Jay Chou"
```

脚本会依次尝试每个关键词，直到在 MusicBrainz 上找到匹配。

不指定 `--artist-map` 时，脚本会用文件夹名作为歌手名直接搜索 MusicBrainz（支持简繁中文和英文名）。如果搜索结果不理想，再用 `--artist-map` 精确指定。

## 支持的格式

| 格式 | 读标签 | 写标签 |
| --- | --- | --- |
| .flac | Yes | Yes |
| .mp3 | Yes | Yes |
| .m4a | Yes | Yes |

## 写入的标签

| 标签 | 说明 |
| --- | --- |
| title | 歌曲标题 |
| artist | 艺术家 |
| album | 专辑名 |
| date | 发行日期 |
| tracknumber | 曲目号 |
| musicbrainz_trackid | MusicBrainz 录音 ID |
| musicbrainz_albumid | MusicBrainz 专辑 ID |

## 性能参考

实测 3796 首华语音乐：

| 步骤 | 匹配数 | 耗时 |
| --- | --- | --- |
| 已有标签 | 681 | - |
| 按歌手名 | 249 | ~10 分钟 |
| 声纹识别 | 1191 | ~20 分钟 |
| 整理文件 | - | ~1 分钟 |
| **合计** | **2121 (56%)** | **~30 分钟** |

未匹配的主要是翻唱、Live、综艺现场、纯音乐等，这些在 MusicBrainz/AcoustID 数据库里本身没有。

## 依赖

- [mutagen](https://mutagen.readthedocs.io/) — 音频标签读写
- [requests](https://docs.python-requests.org/) — HTTP 请求
- [pyyaml](https://pyyaml.org/) — YAML 配置解析
- [Chromaprint](https://acoustid.org/chromaprint) — 声纹生成（需单独安装）
- [MusicBrainz](https://musicbrainz.org/) — 开放音乐数据库
- [AcoustID](https://acoustid.org/) — 声纹识别服务

## License

MIT
