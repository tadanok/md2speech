#!/usr/bin/env python3
"""Markdownファイルを Google Cloud Text-to-Speech で MP3 に変換するスクリプト。

使い方:
  export GOOGLE_TTS_API_KEY="あなたのAPIキー"
  python3 md2speech.py 00_full_book.md

出力: 入力ファイルと同じ場所の 00_full_book.mp3
      (途中経過は .md2speech-cache/ に保存され、再実行時に再利用されます)
      "## " 見出し単位でID3チャプターマーカーが埋め込まれ、対応プレイヤーで章ジャンプできます。
"""

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request

from mutagen.id3 import CHAP, CTOC, CTOCFlags, ID3, TIT2
from mutagen.mp3 import MP3

API_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
VOICE = {"languageCode": "ja-JP", "name": "ja-JP-Neural2-B"}
AUDIO_CONFIG = {"audioEncoding": "MP3", "speakingRate": 1.0}
MAX_BYTES = 4500  # APIの上限5000バイトに対して余裕を持たせる
CHAPTER_RE = re.compile(r"^ {0,3}##[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
CACHE_VERSION = 2
MAX_FILENAME_BYTES = 180


def split_by_chapter(text: str) -> list:
    """H2見出し単位で章に分割し、最初のH2より前も保持する。

    戻り値: [(章タイトル, 章の本文Markdown), ...]
    """
    lines = text.splitlines(keepends=True)
    sections = []
    current_lines = []
    current_title = None
    found_heading = False
    fence = None

    for line in lines:
        fence_match = FENCE_RE.match(line)
        if fence:
            current_lines.append(line)
            if fence_match and fence_match.group(1)[0] == fence[0] \
                    and len(fence_match.group(1)) >= fence[1]:
                fence = None
            continue
        if fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            current_lines.append(line)
            continue

        heading = CHAPTER_RE.match(line)
        if heading:
            found_heading = True
            body = "".join(current_lines)
            if current_title is not None:
                sections.append((current_title, body))
            elif body.strip():
                sections.append(("はじめに", body))
            current_title = heading.group(1).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if not found_heading:
        return [("", text)]
    sections.append((current_title, "".join(current_lines)))
    return sections


def clean_markdown(text: str) -> str:
    """読み上げに不要なMarkdown記法を除去する。"""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)   # 見出しの # を除去
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # 太字
    text = re.sub(r"`([^`]+)`", r"\1", text)              # インラインコード
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.M)     # 箇条書き記号
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # リンク → テキストのみ
    text = re.sub(r"https?://\S+", "", text)              # 裸のURLは読まない
    text = re.sub(r"\n{3,}", "\n\n", text)                # 空行の連続を圧縮
    return text.strip()


def split_chunks(text: str, max_bytes: int = MAX_BYTES) -> list:
    """文の区切り(。や改行)を優先して max_bytes 以下のチャンクに分割する。"""
    sentences = re.split(r"(?<=[。！？\n])", text)
    chunks, current = [], ""
    for s in sentences:
        if len((current + s).encode("utf-8")) > max_bytes:
            if current.strip():
                chunks.append(current)
            current = s
            # 1文が上限を超える場合は強制分割
            while len(current.encode("utf-8")) > max_bytes:
                b = current.encode("utf-8")[:max_bytes]
                cut = b.decode("utf-8", errors="ignore")
                chunks.append(cut)
                current = current[len(cut):]
        else:
            current += s
    if current.strip():
        chunks.append(current)
    return chunks


def synthesize(text: str, api_key: str) -> bytes:
    body = json.dumps({
        "input": {"text": text},
        "voice": VOICE,
        "audioConfig": AUDIO_CONFIG,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL}?key={api_key}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"APIエラー (HTTP {e.code}):\n{detail}")
    return base64.b64decode(data["audioContent"])


def add_chapters(mp3_path: str, chapters: list) -> None:
    """chapters: [(タイトル, 開始ms, 終了ms), ...] をID3チャプターとして埋め込む。"""
    audio = MP3(mp3_path)
    audio.tags = ID3()
    child_ids = []
    for i, (title, start_ms, end_ms) in enumerate(chapters, 1):
        elem_id = f"chp{i}"
        child_ids.append(elem_id)
        audio.tags.add(CHAP(
            element_id=elem_id,
            start_time=start_ms,
            end_time=end_ms,
            sub_frames=[TIT2(text=[title or f"パート{i}"])],
        ))
    audio.tags.add(CTOC(
        element_id="toc",
        flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
        child_element_ids=child_ids,
        sub_frames=[TIT2(text=["目次"])],
    ))
    audio.tags.save(mp3_path, v2_version=3)


def sanitize_filename(name: str) -> str:
    sanitized = INVALID_FILENAME_RE.sub("_", name).strip(" .")
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= MAX_FILENAME_BYTES:
        return sanitized
    return encoded[:MAX_FILENAME_BYTES].decode("utf-8", errors="ignore").rstrip(" .")


def extract_book_title(markdown_text: str) -> str:
    """最初のH1見出しを書籍名として抽出する。"""
    match = re.search(r"^#\s+(.+)$", markdown_text, re.M)
    if not match:
        return ""
    return match.group(1).strip()


def build_output_name(src_path: str, markdown_text: str) -> str:
    """旧API互換: H1があればタイトル、それ以外は入力名から出力名を作る。"""
    title = extract_book_title(markdown_text)
    safe_title = sanitize_filename(title)
    if safe_title:
        return safe_title + ".mp3"
    return os.path.splitext(os.path.basename(src_path))[0] + ".mp3"


def build_output_path(src_path: str) -> str:
    src_dir = os.path.dirname(os.path.abspath(src_path))
    stem = os.path.splitext(os.path.basename(src_path))[0]
    return os.path.join(src_dir, stem + ".mp3")


def build_work_paths(src_path: str) -> tuple:
    """入力ごとに分離した章ディレクトリとキャッシュパスを返す。"""
    absolute_src = os.path.abspath(src_path)
    src_dir = os.path.dirname(absolute_src)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    safe_stem = sanitize_filename(stem) or "document"
    source_id = hashlib.sha256(absolute_src.encode("utf-8")).hexdigest()[:12]
    chapter_dir = os.path.join(src_dir, f"{safe_stem}_chapters")
    cache_dir = os.path.join(src_dir, ".md2speech-cache", f"{safe_stem}-{source_id}")
    return chapter_dir, cache_dir


def synthesis_config_hash() -> str:
    config = {
        "cache_version": CACHE_VERSION,
        "voice": VOICE,
        "audio_config": AUDIO_CONFIG,
    }
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def chunk_hash(text: str) -> str:
    payload = f"{synthesis_config_hash()}\0{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_manifest(path: str) -> dict:
    if not os.path.exists(path):
        return {"version": CACHE_VERSION, "parts": {}, "chapter_outputs": []}
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"警告: キャッシュ情報を読み込めないため再構築します: {exc}", file=sys.stderr)
        return {"version": CACHE_VERSION, "parts": {}, "chapter_outputs": []}
    if not isinstance(manifest, dict) or manifest.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "parts": {}, "chapter_outputs": []}
    return manifest


def save_manifest(path: str, manifest: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="manifest-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def get_part_generation_plan(chunks: list, part_dir: str, manifest_path: str = "") -> list:
    """内容と音声設定のハッシュを使い、再利用可能なパートを判定する。"""
    del manifest_path  # 旧APIとの互換用。ファイル名自体がキャッシュキーになる。
    plan = []
    for chunk in chunks:
        chunk_id = chunk_hash(chunk)
        part_name = f"{chunk_id}.mp3"
        part_path = os.path.join(part_dir, part_name)
        should_generate = (
            not os.path.exists(part_path)
            or os.path.getsize(part_path) <= 0
        )
        plan.append((part_path, should_generate, chunk_id))
    return plan


def confirm_overwrite(paths: list) -> None:
    """既に存在する出力ファイルがあれば一覧表示し、上書き(削除)してよいか確認する。"""
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return
    print("以下の音声ファイルは既に存在します。実行すると上書き(削除)されます。")
    for p in existing:
        print(f"  {p}")
    answer = input("続行しますか? [y/n]: ").strip().lower()
    if answer not in ("y", "yes"):
        sys.exit("中断しました。既存ファイルは変更していません。")


def write_joined_mp3(path: str, part_files: list) -> None:
    with open(path, "wb") as output_file:
        for part in part_files:
            with open(part, "rb") as part_file:
                output_file.write(part_file.read())


def make_temp_path(directory: str, suffix: str = ".mp3") -> str:
    fd, path = tempfile.mkstemp(prefix=".md2speech-", suffix=suffix, dir=directory)
    os.close(fd)
    return path


def save_audio_part(path: str, audio: bytes) -> None:
    """音声パートを同じディレクトリの一時ファイル経由で安全に保存する。"""
    temp_path = make_temp_path(os.path.dirname(path))
    try:
        with open(temp_path, "wb") as output_file:
            output_file.write(audio)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def remove_stale_chapter_outputs(chapters_dir: str, old_names: list, new_names: list) -> None:
    """以前のマニフェストに記録された不要な章ファイルだけを削除する。"""
    new_set = set(new_names)
    for name in old_names:
        if name in new_set or os.path.basename(name) != name:
            continue
        path = os.path.join(chapters_dir, name)
        if os.path.isfile(path):
            os.remove(path)


def main():
    if len(sys.argv) != 2:
        sys.exit("使い方: python3 md2speech.py 入力ファイル.md")

    src = sys.argv[1]
    try:
        with open(src, encoding="utf-8") as source_file:
            markdown_text = source_file.read()
    except (OSError, UnicodeError) as exc:
        sys.exit(f"入力ファイルを読み込めません: {exc}")

    chapters = split_by_chapter(markdown_text)

    # 章ごとにMarkdownを整形してからAPI上限バイト数以下のチャンクに分割する。
    # チャプター境界とチャンク境界を揃えるため、章をまたぐ結合はしない。
    chunks, chapter_chunk_counts = [], []
    for _, chapter_text in chapters:
        chapter_chunks = split_chunks(clean_markdown(chapter_text))
        chunks.extend(chapter_chunks)
        chapter_chunk_counts.append(len(chapter_chunks))

    if not chunks:
        sys.exit("読み上げ可能な本文がありません。空のMP3は生成しません。")

    total_chars = sum(len(c) for c in chunks)
    print(f"{len(chapters)} 章 / {len(chunks)} 個のチャンクに分割しました (合計 {total_chars:,} 文字)")

    api_key = os.environ.get("GOOGLE_TTS_API_KEY")
    if not api_key:
        sys.exit("環境変数 GOOGLE_TTS_API_KEY を設定してください。\n"
                 '例: export GOOGLE_TTS_API_KEY="AIza..."')

    out = build_output_path(src)
    has_real_chapters = len(chapters) > 1 or chapters[0][0]

    chapters_dir, cache_dir = build_work_paths(src)
    part_dir = os.path.join(cache_dir, "parts")
    manifest_path = os.path.join(cache_dir, "manifest.json")
    manifest = load_manifest(manifest_path)
    old_chapter_names = manifest.get("chapter_outputs", [])
    if not isinstance(old_chapter_names, list):
        old_chapter_names = []
    old_chapter_names = [name for name in old_chapter_names if isinstance(name, str)]
    chapter_out_paths = []
    if has_real_chapters:
        digits = max(2, len(str(len(chapters))))
        for n, (title, _) in enumerate(chapters, 1):
            name = sanitize_filename(title) or f"part{n}"
            chapter_out_paths.append(os.path.join(chapters_dir, f"{n:0{digits}d}_{name}.mp3"))

    # API呼び出し(課金)の前に、既存の出力ファイルを上書きしてよいか確認する
    stale_chapter_paths = [
        os.path.join(chapters_dir, name)
        for name in old_chapter_names
        if os.path.basename(name) == name
        and name not in {os.path.basename(path) for path in chapter_out_paths}
    ]
    confirm_overwrite([out] + chapter_out_paths + stale_chapter_paths)
    os.makedirs(part_dir, exist_ok=True)
    if has_real_chapters or os.path.isdir(chapters_dir):
        os.makedirs(chapters_dir, exist_ok=True)

    part_plan = get_part_generation_plan(chunks, part_dir, manifest_path)
    manifest.update({
        "version": CACHE_VERSION,
        "source": os.path.abspath(src),
        "config_hash": synthesis_config_hash(),
    })
    if not isinstance(manifest.get("parts"), dict):
        manifest["parts"] = {}
    part_files = []
    for i, (part, should_generate, chunk_id) in enumerate(part_plan, 1):
        part_files.append(part)
        manifest["parts"][os.path.basename(part)] = {"chunk_hash": chunk_id}
        if not should_generate:
            print(f"[{i}/{len(chunks)}] スキップ (内容未変更)")
            continue
        print(f"[{i}/{len(chunks)}] 変換中... ({len(chunks[i - 1].encode('utf-8')):,} バイト)")
        audio = synthesize(chunks[i - 1], api_key)
        save_audio_part(part, audio)
        save_manifest(manifest_path, manifest)

    # 再利用したパートもマニフェストへ記録し、処理順を復元できるようにする。
    manifest["ordered_parts"] = [os.path.basename(path) for path in part_files]
    save_manifest(manifest_path, manifest)

    # 章ごとに使うパートファイルの範囲 (part_files のスライス) を求める
    chapter_part_ranges, idx = [], 0
    for count in chapter_chunk_counts:
        chapter_part_ranges.append((idx, idx + count))
        idx += count

    output_dir = os.path.dirname(out)
    temp_paths = []
    chapter_temp_paths = []
    try:
        temp_out = make_temp_path(output_dir)
        temp_paths.append(temp_out)
        write_joined_mp3(temp_out, part_files)

        if has_real_chapters:
            # 各パートの再生時間(mutagenで実測)を積み上げて章の開始/終了時刻を求める
            part_durations_ms = [round(MP3(p).info.length * 1000) for p in part_files]
            chapter_marks, offset = [], 0
            for (title, _), (start, end) in zip(chapters, chapter_part_ranges):
                start_ms = offset
                offset += sum(part_durations_ms[start:end])
                chapter_marks.append((title, start_ms, offset))
            add_chapters(temp_out, chapter_marks)

            # すべての章ファイルを一時ファイルとして完成させてから置き換える。
            for start, end in chapter_part_ranges:
                temp_chapter = make_temp_path(chapters_dir)
                temp_paths.append(temp_chapter)
                write_joined_mp3(temp_chapter, part_files[start:end])
                chapter_temp_paths.append(temp_chapter)

        os.replace(temp_out, out)
        temp_paths.remove(temp_out)
        for temp_chapter, chapter_out in zip(chapter_temp_paths, chapter_out_paths):
            os.replace(temp_chapter, chapter_out)
            temp_paths.remove(temp_chapter)
    finally:
        for temp_path in temp_paths:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    new_chapter_names = [os.path.basename(path) for path in chapter_out_paths]
    remove_stale_chapter_outputs(chapters_dir, old_chapter_names, new_chapter_names)
    manifest["chapter_outputs"] = new_chapter_names
    save_manifest(manifest_path, manifest)

    if has_real_chapters:
        print(f"{len(chapters)} 章分のチャプターマーカーを埋め込みました")
        print(f"{len(chapters)} 個の章別ファイルを {chapters_dir}/ に出力しました")

    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"\n完了: {out} ({size_mb:.1f} MB)")
    print(f"再生: afplay {out}")


if __name__ == "__main__":
    main()
