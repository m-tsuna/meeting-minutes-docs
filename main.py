"""
録音から議事録（5-1-2③ Google Meet 録音データから議事録を作成して Google ドキュメントに保存）

会議・講座の録音ファイルを OpenAI の Whisper で文字起こしし、gpt-4o-mini で議事録の形にまとめて、
新しい Google ドキュメントに保存する。

使い方：
  python main.py   … 録音ファイルのパスを聞かれるので、ファイルをドラッグ＆ドロップして Enter
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httplib2
import requests
from dotenv import load_dotenv
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ============================================================
# 設定（ここをまとめて書いておく）
# ============================================================

# このプログラム（main.py）が置いてあるフォルダ
BASE_DIR = Path(__file__).resolve().parent

# 鍵などを書いておくファイル
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"
OPENAI_KEY_NAME = "OPENAI_API_KEY"

# Google の OAuth クライアント（Google Cloud Console でダウンロードしたもの）と、ログイン後にできる通行証
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

# Google に許可をもらう範囲（このアプリが作ったファイルだけを扱える）
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Whisper に送れる録音の形式と大きさ（OpenAI の決まり）
AUDIO_EXTENSIONS = [".m4a", ".mp3", ".wav", ".mp4", ".webm", ".mpeg", ".mpga"]
MAX_AUDIO_MB = 25

# 文字起こし・要約に使う OpenAI のモデルと URL
TRANSCRIBE_MODEL = "whisper-1"
TRANSCRIBE_LANGUAGE = "ja"
# 声が入っていない確からしさ（no_speech_prob、0〜1）がこれより大きい区切りは捨てる。
# Whisper は無音でも「ご視聴ありがとうございました」のような文を作ることがあるため（無音は 0.94、声は 0.33 以下だった）
NO_SPEECH_THRESHOLD = 0.6
SUMMARY_MODEL = "gpt-4o-mini"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# 返事を待つ秒数（文字起こしは録音が長いと時間がかかるので長めにする）
TRANSCRIBE_TIMEOUT_SECONDS = 600
SUMMARY_TIMEOUT_SECONDS = 120

# 議事録の項目（JSON での名前, ドキュメントの見出し）。上から順に並べる
MINUTES_SECTIONS = [
    ("overview", "概要"),
    ("points", "主な内容"),
    ("decisions", "決まったこと"),
    ("todos", "次にやること"),
    ("questions", "分からなかった点・あとで調べること"),
]
TRANSCRIPT_HEADING = "文字起こし全文"

# 要約の頼み方（返事は JSON で受け取り、見出しの形はプログラムでそろえる）
SUMMARY_INSTRUCTION = (
    "あなたは会議や講座の録音の文字起こしから、議事録を作るアシスタントです。"
    "文字起こしを読み、次のキーを持つ JSON だけを返してください。\n"
    '- "overview"：全体の概要（3行ほどの文章。文字列）\n'
    '- "points"：主な内容（箇条書きの1項目ずつを文字列にした配列）\n'
    '- "decisions"：決まったこと（配列）\n'
    '- "todos"：次にやること・宿題。誰がいつまでにやるかが分かれば書く（配列）\n'
    '- "questions"：話の中で分からなかった点・あとで調べること（配列）\n'
    "文字起こしに出てこない内容は書かないでください。当てはまるものが無い項目は空の配列 [] にしてください。"
    "文字起こしの聞き間違いと思われる言葉は、話の流れから自然な言葉に直してかまいません。"
)

# 項目が空のときに書く言葉と、箇条書きの印
EMPTY_TEXT = "なし"
BULLET = "・"


# ============================================================
# F1 準備の確認
# ============================================================
def load_openai_key():
    """
    .env から OpenAI の鍵を読み込んで返す。無い・空のときは直し方を知らせて終了する。
    （鍵の中身は表示しない）
    """
    if not ENV_FILE.exists() and not os.getenv(OPENAI_KEY_NAME):
        show_error_and_exit(
            f"{ENV_FILE.name} が見つかりません。\n"
            f"{ENV_EXAMPLE_FILE.name} をコピーして「{ENV_FILE.name}」という名前にし、"
            f"{OPENAI_KEY_NAME}= の後ろに OpenAI の API キーを書いてください。\n"
            f"置き場所: {BASE_DIR}"
        )
    load_dotenv(ENV_FILE)
    key = os.getenv(OPENAI_KEY_NAME, "").strip()
    if not key:
        show_error_and_exit(
            f"{ENV_FILE.name} の {OPENAI_KEY_NAME} が空です。= の後ろに OpenAI の API キー（sk- で始まる）を書いてください。"
        )
    return key


# ============================================================
# F2 Google ログイン
# ============================================================
def login_google():
    """
    Google にログインして、Docs API を操作するための「サービス」を返す。
    - token.json があれば使い、期限切れなら自動で更新する
    - 更新できない（「テスト中」のアプリは約7日で切れる）ときや、token.json が無いときはブラウザでログインする
    """
    creds = None

    # token.json があれば読み込む（壊れていたら無視して、ログインからやり直す）
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except ValueError:
            creds = None

    # 期限切れだが更新できる場合は、自動で更新する
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            print("ログインの有効期限が切れていたため、もう一度ログインします。")
            creds = None

    # まだ使える通行証が無い場合は、ブラウザでログインする
    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            show_error_and_exit(
                f"{CREDENTIALS_FILE.name} が見つかりません。\n"
                "Google Cloud Console で OAuth クライアント ID（デスクトップアプリ）を作成し、\n"
                f"ダウンロードした JSON を「{CREDENTIALS_FILE.name}」という名前で次のフォルダに置いてください: {BASE_DIR}"
            )
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        except ValueError:
            show_error_and_exit(f"{CREDENTIALS_FILE.name} の中身が正しくありません。ダウンロードし直してください。")
        print("ブラウザで Google にログインして、許可してください。")
        creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return build("docs", "v1", credentials=creds)


# ============================================================
# F3 録音ファイルの入力
# ============================================================
def ask_audio_file():
    """
    録音ファイルのパスを聞き、Whisper に送れるファイルなら Path を返す。
    送れないとき（空欄・無い・形式違い・大きすぎる）は、理由を出してもう一度聞く。
    """
    while True:
        answer = input("録音ファイルをここにドラッグ＆ドロップして Enter：")
        path = clean_path(answer)
        if not path:
            print("  → 何も入力されていません。録音ファイルのパスを入れてください。")
            continue
        audio = Path(path).expanduser()
        if not audio.exists():
            print(f"  → ファイルが見つかりません：{audio}")
            continue
        if not audio.is_file():
            print("  → フォルダではなく、録音ファイルを入れてください。")
            continue
        if audio.suffix.lower() not in AUDIO_EXTENSIONS:
            print(f"  → 対応していない形式です。使える形式：{' '.join(AUDIO_EXTENSIONS)}")
            continue
        size_mb = audio.stat().st_size / (1024 * 1024)
        if size_mb > MAX_AUDIO_MB:
            print(
                f"  → ファイルが大きすぎます（{size_mb:.1f}MB）。Whisper に送れるのは {MAX_AUDIO_MB}MB までです。\n"
                "     録音を短く分けてから入れてください。"
            )
            continue
        return audio


def clean_path(text):
    """
    入力されたパスから、前後の空白と、ドラッグ＆ドロップで付く引用符（' "）を取り除く。
    ターミナルが空白の前に付ける「\\」も取り除く（例：My\\ Voice.m4a → My Voice.m4a）。
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text.replace("\\ ", " ").strip()


# ============================================================
# F4 文字起こし
# ============================================================
def transcribe(api_key, audio):
    """
    OpenAI の文字起こし API（Whisper）に録音ファイルを送り、文字起こしの文を返す。
    ファイルは「multipart/form-data」（ファイルを添付して送る形）で送る。
    """
    with audio.open("rb") as file:
        response = send_request(
            OPENAI_TRANSCRIBE_URL,
            api_key,
            TRANSCRIBE_TIMEOUT_SECONDS,
            files={"file": (audio.name, file)},
            # verbose_json：区切り（segments）ごとに、声が入っていない確からしさも返してもらう
            data={"model": TRANSCRIBE_MODEL, "language": TRANSCRIBE_LANGUAGE, "response_format": "verbose_json"},
        )
    if response.status_code != 200:
        show_error_and_exit(explain_openai_error(response, "文字起こし"))
    # 返事の形：{"text": "全文", "segments": [{"text": "区切りの文", "no_speech_prob": 0.02, ...}, ...]}
    # 声が入っていない区切りを捨て、残りをつなげる
    segments = response.json().get("segments", [])
    texts = [s.get("text", "").strip() for s in segments if s.get("no_speech_prob", 0) <= NO_SPEECH_THRESHOLD]
    return " ".join(t for t in texts if t)


# ============================================================
# F5 要約
# ============================================================
def summarize(api_key, transcript):
    """
    gpt-4o-mini に、議事録の頼み方（system）と文字起こし（user）を送り、
    議事録の項目を {"overview": "...", "points": [...], ...} の辞書で返す。
    """
    response = send_request(
        OPENAI_CHAT_URL,
        api_key,
        SUMMARY_TIMEOUT_SECONDS,
        json={
            "model": SUMMARY_MODEL,
            # JSON だけを返すように指定する（見出しの形をプログラムでそろえるため）
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SUMMARY_INSTRUCTION},
                {"role": "user", "content": transcript},
            ],
        },
    )
    if response.status_code != 200:
        show_error_and_exit(explain_openai_error(response, "要約"))
    # 返事の形：{"choices": [{"message": {"content": "議事録の JSON（文字列）"}}]}
    content = response.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        show_error_and_exit("要約の返事を読み取れませんでした。もう一度実行してください。")


def explain_openai_error(response, step):
    """
    OpenAI が返した失敗の返事を、原因と直し方が分かる日本語の説明に直して返す。
    step は「文字起こし」「要約」のどちらで失敗したか。
    """
    # OpenAI は失敗の理由を {"error": {"message": ..., "code": ...}} の形で返す
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code = error.get("code") or error.get("type") or ""
    detail = f"詳細: HTTP {response.status_code} {code}".rstrip()

    if response.status_code == 401:
        return (
            f"OpenAI の API キーが正しくありません。{ENV_FILE.name} の {OPENAI_KEY_NAME} を、\n"
            "OpenAI の API keys の画面で作ったキー（sk- で始まる）で貼り直してください。\n" + detail
        )
    if response.status_code == 429 and code == "insufficient_quota":
        return "OpenAI の残高（クレジット）が足りません。OpenAI の Billing の画面で残高を確かめてください。\n" + detail
    if response.status_code == 429:
        return "OpenAI に短い時間に頼みすぎました。少し待ってからやり直してください。\n" + detail
    if response.status_code == 400 and step == "文字起こし":
        return "録音ファイルを読み取れませんでした。ファイルが壊れていないか、録音し直して確かめてください。\n" + detail
    return f"OpenAI で{step}できませんでした。\n" + detail


# ============================================================
# F6 ドキュメント保存
# ============================================================
def build_minutes_text(title, audio, minutes, transcript):
    """
    ドキュメントに入れる本文を作る。
    返すもの：(本文, 見出しの一覧 [(始まりの位置, 終わりの位置, 見出しの種類), ...])
    位置は Docs API の数え方（本文の先頭が 1。文字数は UTF-16 で数える）。
    """
    paragraphs = [(title, "TITLE"), (f"録音ファイル：{audio.name}", None)]
    for key, heading in MINUTES_SECTIONS:
        paragraphs.append((heading, "HEADING_2"))
        value = minutes.get(key)
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            paragraphs += [(BULLET + item, None) for item in items] or [(EMPTY_TEXT, None)]
        else:
            paragraphs.append((str(value or "").strip() or EMPTY_TEXT, None))
    paragraphs += [(TRANSCRIPT_HEADING, "HEADING_2"), (transcript, None)]

    text = ""
    headings = []
    index = 1
    for paragraph, style in paragraphs:
        line = paragraph + "\n"
        length = len(line.encode("utf-16-le")) // 2
        if style:
            headings.append((index, index + length, style))
        text += line
        index += length
    return text, headings


def save_to_docs(docs, title, text, headings):
    """
    新しい Google ドキュメントを作り（documents.create）、本文と見出しを書き込んで（documents.batchUpdate）、URL を返す。
    create は題名しか使わないので、本文は作ったあとに batchUpdate で入れる。
    """
    document = docs.documents().create(body={"title": title}).execute()
    document_id = document["documentId"]
    url = f"https://docs.google.com/document/d/{document_id}/edit"

    # 本文を先頭（位置 1）に入れ、そのあと見出しの段落に「タイトル」「見出し 2」の形を付ける
    requests_body = [{"insertText": {"location": {"index": 1}, "text": text}}]
    for start, end, style in headings:
        requests_body.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": style},
                    "fields": "namedStyleType",
                }
            }
        )
    try:
        docs.documents().batchUpdate(documentId=document_id, body={"requests": requests_body}).execute()
    except (HttpError, httplib2.HttpLib2Error, OSError):
        print(f"エラー: ドキュメントは作りましたが、本文を書き込めませんでした（空のドキュメント）：{url}", file=sys.stderr)
        raise
    return url


def explain_google_error(error):
    """
    Google が返した失敗（HttpError）を、原因と直し方が分かる日本語の説明に直して返す。
    """
    status = error.resp.status
    reason = error.reason or ""
    detail = f"詳細: HTTP {status} {reason}".rstrip()
    if status == 403 and ("has not been used" in str(error) or "disabled" in str(error)):
        return (
            "Google Docs API が有効になっていません。Google Cloud Console の「API とサービス」で\n"
            "Google Docs API を有効にしてください。\n" + detail
        )
    if status in (401, 403):
        return (
            f"Google の許可が足りないか、切れています。{TOKEN_FILE.name} を削除して、もう一度実行してログインしてください。\n"
            + detail
        )
    return "Google ドキュメントに保存できませんでした。\n" + detail


# ============================================================
# F7 エラー表示（共通）
# ============================================================
def send_request(url, api_key, timeout, **kwargs):
    """
    OpenAI に POST で頼みごとを送り、返事を返す（文字起こし・要約で共通）。
    つながらない・時間切れのときは、日本語のメッセージを出して終了する。
    """
    try:
        return requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout, **kwargs)
    except requests.Timeout:
        show_error_and_exit(f"OpenAI から {timeout} 秒たっても応答がありませんでした。時間をおいてやり直してください。")
    except requests.RequestException as error:
        # エラーの文字列をそのまま出すと鍵などが出ることがあるので、種類の名前だけを出す
        show_error_and_exit(
            f"OpenAI につながりませんでした。インターネット接続を確認してください。\n詳細: {type(error).__name__}"
        )


def show_error_and_exit(message):
    """
    原因が分かる日本語のエラーメッセージを表示して、プログラムを終了する。
    """
    print(f"エラー: {message}", file=sys.stderr)
    sys.exit(1)


# ============================================================
# メインの処理（上の関数を順番に呼ぶ）
# ============================================================
def main():
    """
    プログラム全体の流れ：
    準備の確認 → Google ログイン → 録音ファイルの入力 → 文字起こし → 要約 → ドキュメント保存
    （料金のかかる文字起こしの前に Google にログインし、ログインの失敗で料金が無駄にならないようにする）
    """
    try:
        # F1 準備の確認
        api_key = load_openai_key()

        # F2 Google ログイン
        print("Google にログインしています…（初回はブラウザが開きます）")
        docs = login_google()

        # F3 録音ファイルの入力
        audio = ask_audio_file()

        # F4 文字起こし
        size_mb = audio.stat().st_size / (1024 * 1024)
        print(f"文字起こししています（{size_mb:.1f}MB。長い録音は数分かかります）…")
        transcript = transcribe(api_key, audio)
        if not transcript:
            # 何も聞き取れなければ、要約もドキュメント作成もしない（料金と空のドキュメントを増やさない）
            show_error_and_exit("声が聞き取れませんでした。録音に声が入っているか確かめてください。")

        # F5 要約
        print("議事録にまとめています…")
        minutes = summarize(api_key, transcript)

        # F6 ドキュメント保存
        print("Google ドキュメントに保存しています…")
        title = f"議事録 {datetime.now():%Y-%m-%d %H:%M}（{audio.stem}）"
        text, headings = build_minutes_text(title, audio, minutes, transcript)
        url = save_to_docs(docs, title, text, headings)
        print(f"\n✅ 議事録を作りました：\n{url}")

    except KeyboardInterrupt:
        # Ctrl + C で途中でやめた場合
        print("\n中断しました。")
        sys.exit(1)
    except EOFError:
        # 入力が途中で終わった場合（Ctrl + D など）
        print("\n入力が終わったため、中断しました。")
        sys.exit(1)
    except TransportError:
        # Google のログイン（通行証の更新）で通信に失敗した（GoogleAuthError の仲間なので先に書く）
        show_error_and_exit("Google につながりませんでした。インターネット接続を確認してください。")
    except GoogleAuthError:
        show_error_and_exit(
            f"Google にログインできませんでした。{TOKEN_FILE.name} を削除して、もう一度実行してください。"
        )
    except HttpError as error:
        show_error_and_exit(explain_google_error(error))
    except (httplib2.HttpLib2Error, OSError) as error:
        # Docs API の通信（httplib2）がつながらなかった
        show_error_and_exit(
            f"Google につながりませんでした。インターネット接続を確認してください。\n詳細: {type(error).__name__}"
        )


if __name__ == "__main__":
    main()
