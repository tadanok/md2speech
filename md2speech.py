#!/usr/bin/env python3
"""Markdownファイルを Google Cloud Text-to-Speech で MP3 に変換するスクリプト。

使い方:
  export GOOGLE_TTS_API_KEY="あなたのAPIキー"
  python3 md2speech.py 00_full_book.md

出力: 00_full_book.mp3 (途中経過は tts_parts/ に保存され、再実行時はスキップされます)
      "## " 見出し単位でID3チャプターマーカーが埋め込まれ、対応プレイヤーで章ジャンプできます。
"""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.request

from mutagen.id3 import CHAP, CTOC, CTOCFlags, ID3, TIT2
from mutagen.mp3 import MP3

API_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
VOICE = {"languageCode": "ja-JP", "name": "ja-JP-Neural2-B"}
AUDIO_CONFIG = {"audioEncoding": "MP3", "speakingRate": 1.0}
MAX_BYTES = 4500  # APIの上限5000バイトに対して余裕を持たせる
CHAPTER_RE = re.compile(r"^## (.+)$", re.M)
INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def split_by_chapter(text: str) -> list:
    """"## "見出し単位で章に分割する。見出しが無ければ全体を1章として扱う。

    戻り値: [(章タイトル, 章の本文Markdown), ...]
    """
    matches = list(CHAPTER_RE.finditer(text))
    if not matches:
        return [("", text)]
    chapters = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chapters.append((m.group(1).strip(), text[start:end]))
    return chapters


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
    return INVALID_FILENAME_RE.sub("_", name).strip()


def extract_book_title(markdown_text: str) -> str:
    """最初の H1 見出しを書籍名として抽出する。"""
    match = re.search(r"^#\s+(.+)$", markdown_text, re.M)
    if not match:
        return ""
    return match.group(1).strip()


def build_output_name(src_path: str, markdown_text: str) -> str:
    """Markdown の H1 見出しがあれば、それをベースに出力名を作る。"""
    title = extract_book_title(markdown_text)
    if title:
        safe_title = sanitize_filename(title)
        if safe_title:
            return f"{safe_title}.mp3"
    return os.path.splitext(os.path.basename(src_path))[0] + ".mp3"


def chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest(path: str) -> dict:
    if not os.path.exists(path):
        return {"version": 1, "parts": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(path: str, manifest: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_part_generation_plan(chunks: list, part_dir: str, manifest_path: str) -> list:
    manifest = load_manifest(manifest_path)
    stored_parts = manifest.get("parts", {})
    plan = []
    for i, chunk in enumerate(chunks, 1):
        part_name = f"part{i:03d}.mp3"
        part_path = os.path.join(part_dir, part_name)
        chunk_id = chunk_hash(chunk)
        stored_hash = stored_parts.get(part_name, {}).get("chunk_hash")
        should_generate = (
            not os.path.exists(part_path)
            or os.path.getsize(part_path) <= 0
            or stored_hash != chunk_id
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


def reset_output_dirs(part_dir: str, chapters_dir: str) -> None:
    """前回実行時の中間ファイルや章別出力を削除して、クリーンな状態に戻す。"""
    for directory in [part_dir, chapters_dir]:
        if os.path.exists(directory):
            for entry in os.listdir(directory):
                path = os.path.join(directory, entry)
                if os.path.isdir(path):
                    continue
                os.remove(path)


def main():
    if len(sys.argv) != 2:
        sys.exit("使い方: python3 md2speech.py 入力ファイル.md")

    api_key = os.environ.get("GOOGLE_TTS_API_KEY")
    if not api_key:
        sys.exit("環境変数 GOOGLE_TTS_API_KEY を設定してください。\n"
                 '例: export GOOGLE_TTS_API_KEY="AIza..."')

    src = sys.argv[1]
    chapters = split_by_chapter(open(src, encoding="utf-8").read())

    # 章ごとにMarkdownを整形してからAPI上限バイト数以下のチャンクに分割する。
    # チャプター境界とチャンク境界を揃えるため、章をまたぐ結合はしない。
    chunks, chapter_chunk_counts = [], []
    for _, chapter_text in chapters:
        chapter_chunks = split_chunks(clean_markdown(chapter_text))
        chunks.extend(chapter_chunks)
        chapter_chunk_counts.append(len(chapter_chunks))

    total_chars = sum(len(c) for c in chunks)
    print(f"{len(chapters)} 章 / {len(chunks)} 個のチャンクに分割しました (合計 {total_chars:,} 文字)")

    markdown_text = open(src, encoding="utf-8").read()
    out = build_output_name(src, markdown_text)
    has_real_chapters = len(chapters) > 1 or chapters[0][0]

    part_dir = "tts_parts"
    chapters_dir = "chapters"
    chapter_out_paths = []
    if has_real_chapters:
        digits = len(str(len(chapters)))
        for n, (title, _) in enumerate(chapters, 1):
            name = sanitize_filename(title) or f"part{n}"
            chapter_out_paths.append(os.path.join(chapters_dir, f"{n:0{digits}d}_{name}.mp3"))

    # API呼び出し(課金)の前に、既存の出力ファイルを上書きしてよいか確認する
    confirm_overwrite([out] + chapter_out_paths)
    os.makedirs(part_dir, exist_ok=True)
    os.makedirs(chapters_dir, exist_ok=True)
    reset_output_dirs(part_dir, chapters_dir)
    manifest_path = os.path.join(part_dir, "manifest.json")

    part_plan = get_part_generation_plan(chunks, part_dir, manifest_path)
    manifest = {"version": 1, "parts": {}}
    part_files = []
    for i, (part, should_generate, chunk_id) in enumerate(part_plan, 1):
        part_files.append(part)
        if not should_generate:
            print(f"[{i}/{len(chunks)}] スキップ (内容未変更)")
            continue
        print(f"[{i}/{len(chunks)}] 変換中... ({len(chunks[i - 1].encode('utf-8')):,} バイト)")
        audio = synthesize(chunks[i - 1], api_key)
        with open(part, "wb") as f:
            f.write(audio)
        manifest["parts"][os.path.basename(part)] = {"chunk_hash": chunk_id}

    save_manifest(manifest_path, manifest)

    # 章ごとに使うパートファイルの範囲 (part_files のスライス) を求める
    chapter_part_ranges, idx = [], 0
    for count in chapter_chunk_counts:
        chapter_part_ranges.append((idx, idx + count))
        idx += count

    with open(out, "wb") as w:
        for part in part_files:
            with open(part, "rb") as r:
                w.write(r.read())

    if has_real_chapters:
        # 各パートの再生時間(mutagenで実測)を積み上げて章の開始/終了時刻を求める
        part_durations_ms = [round(MP3(p).info.length * 1000) for p in part_files]
        chapter_marks, offset = [], 0
        for (title, _), (start, end) in zip(chapters, chapter_part_ranges):
            start_ms = offset
            offset += sum(part_durations_ms[start:end])
            chapter_marks.append((title, start_ms, offset))
        add_chapters(out, chapter_marks)
        print(f"{len(chapter_marks)} 章分のチャプターマーカーを埋め込みました")

        # 章ごとの個別mp3も chapters/ に出力する
        os.makedirs(chapters_dir, exist_ok=True)
        for chapter_out, (start, end) in zip(chapter_out_paths, chapter_part_ranges):
            with open(chapter_out, "wb") as cw:
                for part in part_files[start:end]:
                    with open(part, "rb") as r:
                        cw.write(r.read())
        print(f"{len(chapters)} 個の章別ファイルを {chapters_dir}/ に出力しました")

    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"\n完了: {out} ({size_mb:.1f} MB)")
    print(f"再生: afplay {out}")


if __name__ == "__main__":
    main()
