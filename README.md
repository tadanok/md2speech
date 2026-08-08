# md2speech

Markdown ファイルを [Google Cloud Text-to-Speech](https://cloud.google.com/text-to-speech) API で日本語音声(MP3)に変換する CLI スクリプトです。

## 特徴

- Markdown の見出し・太字・インラインコード・箇条書き・リンク・裸URLなどを読み上げ用に自動整形
- `## ` 見出し(章)単位でテキストを分割したうえで、API の1リクエストあたりの上限バイト数を考慮して自動でチャンク分割(章をまたいでチャンクを結合しない)
- チャンクごとに `tts_parts/` へ保存するため、途中でエラーが発生しても再実行時に続きから再開可能(レジューム機能)
- 生成した音声パートを結合して1つの MP3 ファイルを出力し、各パートの実測再生時間から章の開始/終了時刻を算出してID3チャプターマーカー(CHAP/CTOC)を埋め込む
  - 対応プレイヤー(Podcastアプリ、iTunes/Music、Audible系アプリなど)で章単位のジャンプが可能になる
- 章ごとの個別 MP3 ファイルも `chapters/` ディレクトリに `01_章タイトル.mp3` の形式で出力(ID3非対応の環境でもファイル単位で章を選べる)

## 必要なもの

- Python 3.7 以上
- [mutagen](https://mutagen.readthedocs.io/)(ID3チャプターの埋め込みに使用)

  ```bash
  pip install -r requirements.txt
  ```

- Google Cloud Text-to-Speech の API キー

## セットアップ

1. [Google Cloud Console](https://console.cloud.google.com/) で Text-to-Speech API を有効化し、APIキーを発行します。
2. 依存パッケージをインストールします。

   ```bash
   pip install -r requirements.txt
   ```

3. 環境変数にAPIキーを設定します。

   ```bash
   export GOOGLE_TTS_API_KEY="あなたのAPIキー"
   ```

## 使い方

```bash
python3 md2speech.py 入力ファイル.md
```

実行すると、入力ファイルと同じディレクトリに以下が生成されます。

- `入力ファイル.mp3`: 全体を結合し、ID3チャプターマーカーを埋め込んだ完成版
- `chapters/01_章タイトル.mp3`, `chapters/02_章タイトル.mp3`, ...: 章ごとの個別ファイル
- `tts_parts/part001.mp3`, `part002.mp3`, ...: 変換途中の音声パート(中間生成物、再実行時のレジューム用キャッシュ)

生成後は以下で再生できます(macOS の場合)。

```bash
afplay 入力ファイル.mp3
```

## 設定のカスタマイズ

`md2speech.py` 冒頭の定数を編集することで、音声の設定を変更できます。

| 定数 | 説明 | デフォルト |
| --- | --- | --- |
| `VOICE` | 言語・話者(音声モデル) | `ja-JP` / `ja-JP-Neural2-B` |
| `AUDIO_CONFIG` | 音声形式・話速など | MP3 / 標準速度(`1.0`) |
| `MAX_BYTES` | 1リクエストあたりの最大バイト数 | `4500` |

利用可能な音声の一覧は [Text-to-Speech の音声リスト](https://cloud.google.com/text-to-speech/docs/voices) を参照してください。

## 章の分割について

入力Markdown内の `## ` 見出し(レベル2)を章の区切りとして扱います。`### ` 以下の見出しは章とは見なされず、本文として読み上げられます。見出しが1つも無いファイルを渡した場合は、全体を1つの章として扱い、チャプターマーカーの埋め込みと `chapters/` への個別出力は行いません。

`chapters/` 内のファイル名は見出しテキストをそのまま使用しますが、ファイル名に使えない文字(`\ / : * ? " < > |`)は `_` に置換されます。

## 注意事項

- Google Cloud Text-to-Speech API は利用量に応じて課金されます。事前に[料金体系](https://cloud.google.com/text-to-speech/pricing)をご確認ください。
- `tts_parts/` に生成済みのファイルが残っていると、同名のパートはスキップされます。音声設定やチャンク分割ロジック(`MAX_BYTES`や章の見出し構成)を変更した場合は、チャンクの区切り位置がずれてパート番号と本文が対応しなくなるため、`tts_parts/` を削除してから再実行してください。
- 出力先(`入力ファイル.mp3` や `chapters/` 内のファイル)が既に存在する場合、API呼び出しを行う前に上書きしてよいか確認を求められます。`y` 以外を入力すると、既存ファイルを変更せずに中断します。

## ライセンス

MIT License
