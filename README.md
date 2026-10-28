# 録音から議事録（meeting-minutes-to-docs）

会議・講座の録音ファイルを AI で文字起こしし、議事録の形にまとめて、新しい Google ドキュメントに保存する Python プログラムです。

```
録音ファイル → ① 文字起こし（OpenAI Whisper）→ ② 要約（gpt-4o-mini）→ ③ Google ドキュメントに保存
```

議事録には次の項目が入ります。

- 概要／主な内容／決まったこと／次にやること／分からなかった点・あとで調べること
- 文字起こし全文

## 事前準備

1. Python 3 と、次のライブラリ
   ```
   pip install requests python-dotenv google-api-python-client google-auth-oauthlib
   ```
2. OpenAI の API キー（残高が入っていること。文字起こしは1分あたり約1円）
3. Google Cloud のプロジェクトで **Google Docs API** を有効にし、OAuth クライアント ID（デスクトップアプリ）を作る

> 個人の無料 Gmail では、Google Meet の録画・自動文字起こしは使えません（有料版 Google Workspace の機能）。
> そのため、会議の音声は Mac のボイスメモなどで録音し、そのファイルを渡します。

## 秘密ファイルの用意の仕方

どちらも GitHub に載せないよう `.gitignore` に入っています。

| ファイル | 用意の仕方 |
|---|---|
| `.env` | `.env.example` をコピーして `.env` という名前にし、`OPENAI_API_KEY=` の後ろに API キー（`sk-` で始まる）を書く |
| `credentials.json` | Google Cloud Console の「API とサービス」→「認証情報」で作った OAuth クライアント（デスクトップアプリ）の JSON をダウンロードし、この名前で `main.py` と同じフォルダに置く |

初めて実行するとブラウザで Google のログイン画面が開きます。許可すると `token.json` ができ、次からはログインなしで使えます。
Google に許可するのは `drive.file`（このアプリが作ったファイルだけを扱える）の範囲だけです。

## 使い方

```
python main.py
```

1. 録音ファイル（`.m4a` `.mp3` `.wav` `.mp4` `.webm` `.mpeg` `.mpga`、25MB まで）をターミナルにドラッグ＆ドロップして Enter
2. 文字起こし → 要約 → 保存が順に進み、最後に Google ドキュメントの URL が表示されます

```
録音ファイルをここにドラッグ＆ドロップして Enter：/Users/.../勉強会.m4a
文字起こししています（1.2MB。長い録音は数分かかります）…
議事録にまとめています…
Google ドキュメントに保存しています…

✅ 議事録を作りました：
https://docs.google.com/document/d/xxxxxxxx/edit
```

## うまくいかないとき

| 表示 | 直し方 |
|---|---|
| `.env が見つかりません` | 上の「秘密ファイルの用意の仕方」で `.env` を作る |
| `OpenAI の API キーが正しくありません` | `.env` のキーを貼り直す |
| `OpenAI の残高（クレジット）が足りません` | OpenAI の Billing で残高を入れる |
| `Google の許可が足りないか、切れています` | `token.json` を削除して、もう一度実行してログインする |
| `Google Docs API が有効になっていません` | Google Cloud Console で Google Docs API を有効にする |
