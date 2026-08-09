import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import md2speech


class ChapterSplitTests(unittest.TestCase):
    def test_preserves_content_before_first_h2(self):
        markdown = "# 本の題名\n\n前書きです。\n\n## 第一章\n本文です。\n"

        chapters = md2speech.split_by_chapter(markdown)

        self.assertEqual("はじめに", chapters[0][0])
        self.assertIn("前書きです。", chapters[0][1])
        self.assertEqual("第一章", chapters[1][0])

    def test_ignores_h2_inside_fenced_code(self):
        markdown = "## 第一章\n本文\n```md\n## 章ではない\n```\n続き\n"

        chapters = md2speech.split_by_chapter(markdown)

        self.assertEqual(1, len(chapters))
        self.assertIn("## 章ではない", chapters[0][1])

    def test_document_without_h2_remains_one_untitled_section(self):
        markdown = "# 題名\n\n本文です。\n"

        self.assertEqual([("", markdown)], md2speech.split_by_chapter(markdown))


class CacheTests(unittest.TestCase):
    def test_existing_content_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            chunk = "本文です。"
            chunk_id = md2speech.chunk_hash(chunk)
            part_path = os.path.join(directory, chunk_id + ".mp3")
            with open(part_path, "wb") as part_file:
                part_file.write(b"audio")

            plan = md2speech.get_part_generation_plan([chunk], directory)

            self.assertFalse(plan[0][1])
            self.assertEqual(part_path, plan[0][0])

    def test_audio_setting_change_invalidates_cache_key(self):
        original = md2speech.chunk_hash("本文です。")

        with mock.patch.object(
            md2speech,
            "AUDIO_CONFIG",
            {"audioEncoding": "MP3", "speakingRate": 1.25},
        ):
            changed = md2speech.chunk_hash("本文です。")

        self.assertNotEqual(original, changed)

    def test_manifest_is_valid_json_after_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            md2speech.save_manifest(path, {"version": md2speech.CACHE_VERSION})

            with open(path, encoding="utf-8") as manifest_file:
                self.assertEqual(md2speech.CACHE_VERSION, json.load(manifest_file)["version"])


class OutputSafetyTests(unittest.TestCase):
    def test_sanitized_filename_has_no_control_chars_or_oversized_utf8(self):
        sanitized = md2speech.sanitize_filename("章\x00" + "あ" * 200)

        self.assertNotIn("\x00", sanitized)
        self.assertLessEqual(len(sanitized.encode("utf-8")), md2speech.MAX_FILENAME_BYTES)

    def test_output_paths_are_based_on_source_location(self):
        source = os.path.join(os.sep, "tmp", "books", "sample.md")

        output = md2speech.build_output_path(source)
        chapters, cache = md2speech.build_work_paths(source)

        self.assertEqual(os.path.join(os.sep, "tmp", "books", "sample.mp3"), output)
        self.assertEqual(os.path.join(os.sep, "tmp", "books", "sample_chapters"), chapters)
        self.assertTrue(cache.startswith(os.path.join(os.sep, "tmp", "books", ".md2speech-cache")))

    def test_only_manifest_owned_stale_chapters_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = os.path.join(directory, "01_古い章.mp3")
            unrelated = os.path.join(directory, "利用者のファイル.txt")
            for path in (stale, unrelated):
                with open(path, "wb") as output_file:
                    output_file.write(b"data")

            md2speech.remove_stale_chapter_outputs(
                directory,
                [os.path.basename(stale)],
                [],
            )

            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(unrelated))

    def test_empty_input_stops_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "empty.md")
            with open(source, "w", encoding="utf-8"):
                pass

            with mock.patch.object(sys, "argv", ["md2speech.py", source]):
                with self.assertRaisesRegex(SystemExit, "読み上げ可能な本文がありません"):
                    md2speech.main()

            self.assertFalse(os.path.exists(os.path.join(directory, "empty.mp3")))

    def test_second_run_reuses_cached_audio_without_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "book.md")
            with open(source, "w", encoding="utf-8") as source_file:
                source_file.write("## 第一章\n本文です。\n")

            common_patches = (
                mock.patch.object(sys, "argv", ["md2speech.py", source]),
                mock.patch.dict(os.environ, {"GOOGLE_TTS_API_KEY": "test-key"}),
                mock.patch.object(md2speech, "MP3"),
                mock.patch.object(md2speech, "add_chapters"),
            )
            with common_patches[0], common_patches[1], common_patches[2] as mp3_mock, common_patches[3]:
                mp3_mock.return_value.info.length = 1.0
                with mock.patch.object(md2speech, "synthesize", return_value=b"audio") as synthesize:
                    md2speech.main()
                    synthesize.assert_called_once()

            with mock.patch.object(sys, "argv", ["md2speech.py", source]), \
                    mock.patch.dict(os.environ, {"GOOGLE_TTS_API_KEY": "test-key"}), \
                    mock.patch.object(md2speech, "MP3") as mp3_mock, \
                    mock.patch.object(md2speech, "add_chapters"), \
                    mock.patch("builtins.input", return_value="y"), \
                    mock.patch.object(md2speech, "synthesize") as synthesize:
                mp3_mock.return_value.info.length = 1.0
                md2speech.main()
                synthesize.assert_not_called()

    def test_retry_after_api_failure_keeps_completed_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "book.md")
            with open(source, "w", encoding="utf-8") as source_file:
                source_file.write("## 第一章\n最初です。\n## 第二章\n次です。\n")

            with mock.patch.object(sys, "argv", ["md2speech.py", source]), \
                    mock.patch.dict(os.environ, {"GOOGLE_TTS_API_KEY": "test-key"}), \
                    mock.patch.object(
                        md2speech,
                        "synthesize",
                        side_effect=[b"first-audio", SystemExit("API failure")],
                    ):
                with self.assertRaisesRegex(SystemExit, "API failure"):
                    md2speech.main()

            _, cache_dir = md2speech.build_work_paths(source)
            part_dir = os.path.join(cache_dir, "parts")
            self.assertEqual(1, len(os.listdir(part_dir)))

            with mock.patch.object(sys, "argv", ["md2speech.py", source]), \
                    mock.patch.dict(os.environ, {"GOOGLE_TTS_API_KEY": "test-key"}), \
                    mock.patch.object(md2speech, "MP3") as mp3_mock, \
                    mock.patch.object(md2speech, "add_chapters"), \
                    mock.patch.object(md2speech, "synthesize", return_value=b"second-audio") as synthesize:
                mp3_mock.return_value.info.length = 1.0
                md2speech.main()
                synthesize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
