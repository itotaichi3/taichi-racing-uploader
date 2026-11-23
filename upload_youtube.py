"""出来上がった動画を、YouTubeへ「非公開」でアップロードする。

============================================================
【公開はしない。ここは変えられないようにしてある】
============================================================
  privacyStatus は "private" で固定である。
  引数でも設定ファイルでも変えられない。

  理由は単純で、中身を見ずに世に出る経路を作らないためである。
  自動で作った動画には、読み間違いも、言い回しの事故も混じる。
  それを人が一度も見ないまま公開する仕組みは、
  いつか必ず事故を起こす。

  公開ボタンは YouTube Studio で自分で押すこと。

============================================================
【同じ動画を2回上げない】
============================================================
  上げたものは data/youtube_〈日付〉/投稿済み.json に記録する。
  2回目からは飛ばす。

  失敗して途中で止まったときに、頭から流し直しても
  重複が出ないようにするためである。

============================================================
【認証について】
============================================================
  初回だけブラウザで許可する。

    python scripts/upload_youtube.py --auth

  secrets/client_secret.json （Google Cloud から落としたもの）が要る。
  許可すると secrets/token.json が作られ、以降は無人で動く。

  この2つはパスワードと同じ重さである。
  .gitignore に入れてあるが、画面共有やスクリーンショットにも
  映さないこと。一度でも外に出たら Google Cloud で作り直すしかない。

使い方:
  python scripts/upload_youtube.py --auth
  python scripts/upload_youtube.py --date 20260924
  python scripts/upload_youtube.py --date 20260924 --jyo 門別
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
TOKEN = SECRETS / "token.json"

# ============================================================
# 【1日の割り当てを、すべての種類で分け合う（2026-10-11に足した）】
# ============================================================
#   videos.insert は1本1,600単位、サムネ設定が50単位。
#   1日10,000単位なので、地方・中央・傾向・検証を全部合わせて6本まで。
#
#   以前は「1回の実行で6本まで」しか見ていなかった。
#   地方で5本上げたあとに傾向動画を3本流すと、2本は quotaExceeded で落ちる。
#   落ちた動画は投稿済みにならないので次の日に回るが、
#   どれが落ちるかは順番次第になる。
#
#   そこで、上げた記録を1か所に残し、今日あと何本上げられるかを数える。
#   「今日」は太平洋時間の日付である（割り当てがそこで戻るため）。
#   日本時間では、夏は16時、冬は17時に切り替わる。
DAILY_UPLOADS = 6
LEDGER = ROOT / "data" / "youtube_upload_log.json"


def pacific_day(now: datetime | None = None) -> str:
    """割り当てが戻る太平洋時間の日付 YYYY-MM-DD。

    Windows の Python は tzdata が無いと zoneinfo が使えないので、
    夏時間の決まり（3月第2日曜〜11月第1日曜の午前2時）で自分で出す。
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001  tzdata が無い
        pass

    def nth_sunday(year: int, month: int, n: int) -> datetime:
        first = datetime(year, month, 1)
        offset = (6 - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    standard = now - timedelta(hours=8)
    year = standard.year
    dst_start = nth_sunday(year, 3, 2) + timedelta(hours=2)
    dst_end = nth_sunday(year, 11, 1) + timedelta(hours=1)
    naive = standard.replace(tzinfo=None)
    local = standard + timedelta(hours=1) if dst_start <= naive < dst_end else standard
    return local.strftime("%Y-%m-%d")


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def record_upload(entry: dict) -> None:
    rows = load_ledger()
    rows.append({"PT日": pacific_day(),
                 "日本時間": datetime.now().strftime("%Y-%m-%d %H:%M"), **entry})
    # 古い記録は要らない。直近200件だけ残す
    LEDGER.write_text(json.dumps(rows[-200:], ensure_ascii=False, indent=1),
                      encoding="utf-8")


def remaining_today() -> int:
    """今日（太平洋時間）あと何本上げられるか。"""
    today = pacific_day()
    rows = [r for r in load_ledger() if r.get("PT日") == today]
    if any(r.get("上限") for r in rows):
        return 0
    return max(0, DAILY_UPLOADS - sum(1 for r in rows if r.get("動画")))


class QuotaExceeded(RuntimeError):
    pass


def find_client_secret() -> Path | None:
    """secrets/ の中から client_secret で始まるJSONを探す。

    【改名を求めるのをやめた（2026-09-23）】
      Google Cloud から落ちてくるのは
        client_secret_XXXXXXXX.apps.googleusercontent.com.json
      という長い名前である。
      「client_secret.json に改名してください」と書いたが、
      その一手間が抜けて「ファイルがありません」で止まった。

      こちらで探せば済む話だった。
      人に余計な手順を課すほど、その手順は飛ばされる。
    """
    if not SECRETS.exists():
        return None
    found = sorted(SECRETS.glob("client_secret*.json"))
    return found[0] if found else None

# アップロードだけ。動画の削除や視聴者データの取得は求めない。
# 要る権限だけを求めるのが、万一漏れたときの被害を小さくする唯一の方法。
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# 【ここは固定】引数で上書きできるようにしないこと
PRIVACY = "private"

CATEGORY_SPORTS = "17"


def need_libraries() -> None:
    # 【32ビットPythonでは cryptography の新しい版が入らない】
    #   このVPSのPythonは32ビットである（JV-Link が32ビットのCOMなので、
    #   中央競馬のデータ取得に合わせてそうしてある）。
    #
    #   cryptography は 49 以降、32ビットWindows向けの
    #   出来合いのファイルを配らなくなった。
    #   そのため pip がソースから組み立てようとし、
    #   Rust と Visual C++ のリンカが無いと言って失敗する。
    #
    #     error: linker `link.exe` not found
    #
    #   48 までは 32ビット向けが配られているので、そちらを入れる。
    print(
        "必要なライブラリが入っていません。\n"
        "  pip install \"cryptography<49\"\n"
        "  pip install google-auth-oauthlib google-api-python-client\n"
        "\n"
        "  1行目を先に実行してください。\n"
        "  このPythonは32ビットなので、cryptography の49以降は\n"
        "  組み立てに失敗します（48までは出来合いのものが配られています）。",
        file=sys.stderr)


def get_service(interactive: bool):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        need_libraries()
        return None

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as error:
            print(f"トークンを更新できませんでした（{error}）。"
                  "\n  --auth でやり直してください。"
                  "\n  同意画面が「テスト中」のままだと、"
                  "トークンは7日で切れます。", file=sys.stderr)
            creds = None

    if not creds or not creds.valid:
        if not interactive:
            print("認証がまだです。先にこれを実行してください。"
                  "\n  python scripts/upload_youtube.py --auth",
                  file=sys.stderr)
            return None
        client_secret = find_client_secret()
        if client_secret is None:
            print(f"{SECRETS} に client_secret で始まるJSONがありません。"
                  "\n  Google Cloud の「クライアント」で作った"
                  "OAuthクライアント（デスクトップアプリ）の"
                  "\n  JSONをこのフォルダに置いてください。"
                  "\n  名前は変えなくて構いません。", file=sys.stderr)
            if SECRETS.exists():
                names = [p.name for p in SECRETS.iterdir()]
                print(f"  いま入っているもの: {names or '（空）'}",
                      file=sys.stderr)
            return None
        print(f"認証情報: {client_secret.name}")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
        SECRETS.mkdir(exist_ok=True)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        print(f"許可を保存しました → {TOKEN}")
        print("  このファイルは外に出さないこと。")

    return build("youtube", "v3", credentials=creds)


def load_done(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def upload_one(service, info: dict, folder: Path) -> str | None:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    video = folder / info["動画"]
    if not video.exists():
        print(f"    {video.name} がありません。", file=sys.stderr)
        return None

    body = {
        "snippet": {
            "title": info["タイトル"],
            "description": info["概要欄"],
            "tags": info["タグ"],
            "categoryId": CATEGORY_SPORTS,
            "defaultLanguage": "ja",
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }

    # 1本20〜30MBある。途中で切れても続きから送れるようにする
    media = MediaFileUpload(str(video), chunksize=4 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = service.videos().insert(
        part="snippet,status", body=body, media_body=media)

    try:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"\r    送信中 {int(status.progress() * 100)}%",
                      end="", flush=True)
        print("\r    送信しました        ")
    except HttpError as error:
        print(f"\n    アップロードに失敗しました: {error}", file=sys.stderr)
        if "quotaExceeded" in str(error) or "uploadLimitExceeded" in str(error):
            raise QuotaExceeded(str(error)) from error
        return None

    video_id = response["id"]

    set_thumbnail(service, video_id, info, folder)
    return video_id


def set_thumbnail(service, video_id: str, info: dict, folder: Path) -> bool:
    """サムネイルを設定する。

    【無いときに黙って飛ばさない（2026-09-23 に直した）】
      最初は if thumb.exists() で囲んでいただけだった。
      4本上げて、1本にしかサムネが付かなかったのに、
      画面には何も出なかった。
      「起きなかったこと」は、言わなければ気づけない。
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    # 【Shortsにサムネイルは要らない】
    #   縦の一覧では動画の1コマ目が使われる。
    #   「サムネ」の項目が無いものは、付けないのが正しい。
    #   無いことを失敗として報せると、毎本うるさいだけになる。
    if "サムネ" not in info:
        return True

    thumb = folder / info.get("サムネ", "")
    if not thumb.exists():
        print(f"    サムネイルがありません（{thumb.name}）。"
              "\n      build_youtube_video.py --thumbnail-only で作れます。",
              file=sys.stderr)
        return False
    try:
        service.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb))).execute()
        print("    サムネイルを設定しました")
        return True
    except HttpError as error:
        # サムネの失敗で動画まで無かったことにはしない
        print(f"    サムネイルは設定できませんでした: {error}",
              file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="動画をYouTubeへ非公開でアップロードする")
    parser.add_argument("--date", help="開催日 YYYYMMDD")
    parser.add_argument("--jyo", help="1つの競馬場だけ")
    # 【Shortsは1レース1本なので、競馬場ではなくレース名で管理する】
    #   data/shorts_〈日付〉/ を見る。
    #   投稿済み.json の鍵も「門別08R」のようなレース名になる。
    #   長い動画とは別のフォルダなので、記録が混ざることはない。
    parser.add_argument("--shorts", action="store_true",
                        help="Shorts（1レース1本）を上げる")
    # 【1日6本しか上げられない】
    #   videos.insert は1回1,600単位。
    #   既定の割り当ては1日10,000単位なので、6本で尽きる。
    #   7本目からは quotaExceeded で弾かれ、太平洋時間の0時まで戻らない。
    #
    #   50本のShortsを一度に流すことはできない。
    #   増やすにはGoogleに申請が要る（審査がある）。
    parser.add_argument("--limit", type=int, default=6,
                        help="1回に上げる本数の上限（既定6＝1日の割り当て）")
    # 【中央と地方でフォルダが違う】
    #   指定を忘れると、中央のつもりで地方のフォルダを見に行く。
    #   投稿済み.json も別なので、二重に上げる事故にはならないが、
    #   「上がっていない」と気づくのが遅れる。
    parser.add_argument("--kind", choices=("nar", "jra", "trend", "topic"), default="nar",
                        help="nar=地方（youtube_）jra=中央（youtube_jra_）"
                             "trend=重賞の傾向（youtube_trend_）"
                             "topic=検証・コース解説（youtube_topic_）")
    parser.add_argument("--only", help="この名前の動画だけ（投稿情報の「競馬場」の値）")
    parser.add_argument("--auth", action="store_true",
                        help="初回の許可だけを行う")
    # 【夜の通し実行の最初に、ログインが生きているかだけ確かめる】
    #   切れていると、2〜3時間走ったあとで「上がっていません」になる。
    parser.add_argument("--check", action="store_true",
                        help="ログインが生きているか確かめるだけ（上げない）")
    parser.add_argument(
        "--thumbnail-only", action="store_true",
        help="すでに上げた動画に、サムネイルだけを付け直す",
    )
    # 【上げ直しは明示的にだけ許す】
    #   YouTube側で処理に失敗することがある（園田がそうだった）。
    #   その場合は上げ直すしかないが、うっかり流して
    #   同じ動画が2本並ぶのは避けたい。--redo と競馬場名を要求する。
    #   古いほうは YouTube Studio で自分で消すこと。
    parser.add_argument(
        "--redo", metavar="競馬場",
        help="その競馬場だけ、記録を消して上げ直す（例: --redo 園田）",
    )
    args = parser.parse_args()

    service = get_service(interactive=args.auth)
    if service is None:
        return 1
    if args.check:
        print(f"YouTubeのログインは生きています。今日の残り枠：{remaining_today()}本")
        return 0
    if args.auth:
        print("\n認証できました。次はこれを実行してください。")
        print("  python scripts/upload_youtube.py --date 20260924")
        return 0
    if not args.date:
        print("--date が要ります。", file=sys.stderr)
        return 1

    if args.shorts:
        folder = ROOT / "data" / f"shorts_{args.date}"
    else:
        folder = ROOT / "data" / (
            f"youtube_jra_{args.date}" if args.kind == "jra"
            else f"youtube_trend_{args.date}" if args.kind == "trend"
            else f"youtube_topic_{args.date}" if args.kind == "topic"
            else f"youtube_{args.date}")
    if not folder.exists():
        print(f"{folder} がありません。", file=sys.stderr)
        return 1

    # Shortsは1レース1本なので、記録の鍵はレース名になる。
    key_of = (lambda info: info["レース"]) if args.shorts \
        else (lambda info: info["競馬場"])

    infos = sorted(folder.glob("*_投稿情報.json"))
    if args.jyo:
        if args.shorts:
            # 門別08R_投稿情報.json → 競馬場で絞る
            infos = [p for p in infos if p.name.startswith(args.jyo)]
        else:
            infos = [p for p in infos
                     if p.stem.replace("_投稿情報", "") == args.jyo]
    if not infos:
        maker = ("build_short_video.py" if args.shorts
                 else "build_youtube_meta.py")
        print(f"投稿情報がありません。\n  先に {maker} を実行してください。",
              file=sys.stderr)
        return 1

    if args.shorts:
        print(f"Shorts {len(infos)}本ぶんの投稿情報があります。")
        print(f"  1回に上げるのは{args.limit}本までです"
              "（videos.insert は1本1,600単位、1日10,000単位）。")
        print("  残りは翌日（太平洋時間の0時に戻ります）に流してください。")
        print("  すでに上げたものは飛ばすので、同じ命令で続きから進みます。\n")

    done_path = folder / "投稿済み.json"
    done = load_done(done_path)

    if args.redo:
        old = done.pop(args.redo, None)
        if old is None:
            print(f"{args.redo} は記録にありません。", file=sys.stderr)
            return 1
        done_path.write_text(
            json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{args.redo} の記録を消しました（前回 {old}）。")
        print(f"  古いほうは YouTube Studio で消してください。"
              f"\n  https://studio.youtube.com/video/{old}/edit\n")
        infos = [p for p in infos
                 if p.stem.replace("_投稿情報", "") == args.redo]

    if args.thumbnail_only:
        # 動画は上げ直さない。1本20〜30MBを送り直す理由が無い。
        if not done:
            print("まだ何も上げていません。", file=sys.stderr)
            return 1
        made = 0
        for path in infos:
            info = json.loads(path.read_text(encoding="utf-8"))
            jyo = key_of(info)
            if jyo not in done:
                print(f"■ {jyo}　まだ上げていません")
                continue
            print(f"■ {jyo}")
            if set_thumbnail(service, done[jyo], info, folder):
                made += 1
        print(f"\n{made}本にサムネイルを設定しました。")
        return 0

    # 今日の残り枠。ほかの種類で使った分を引く
    left = remaining_today()
    limit = min(args.limit, left)
    if left < DAILY_UPLOADS:
        print(f"今日（太平洋時間 {pacific_day()}）の残り枠：{left}本"
              f"（1日{DAILY_UPLOADS}本をすべての種類で分け合います）")

    uploaded = []
    pending = 0
    for path in infos:
        info = json.loads(path.read_text(encoding="utf-8"))
        jyo = key_of(info)
        if args.only and jyo != args.only:
            continue
        if jyo in done:
            print(f"■ {jyo}　すでに上げてあります（{done[jyo]}）")
            continue
        if len(uploaded) >= limit:
            pending += 1
            continue
        print(f"■ {jyo}　{info['タイトル']}")
        try:
            video_id = upload_one(service, info, folder)
        except QuotaExceeded:
            # 記録に無い分（この仕組みの前に上げた分など）で尽きていた。
            # 今日はもう上げない、と記録しておく
            record_upload({"上限": True, "フォルダ": folder.name})
            print("  今日の割り当てが尽きていました。残りは明日に回ります。",
                  file=sys.stderr)
            pending += 1
            break
        if not video_id:
            continue
        done[jyo] = video_id
        done_path.write_text(
            json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
        record_upload({"動画": video_id, "フォルダ": folder.name, "名前": jyo})
        uploaded.append((jyo, video_id))

    if pending:
        print(f"\n枠が足りず、{pending}本は上げていません。")
        print("  残りは明日、同じ命令で続きから上がります（上げたものは飛ばします）。")

    if not uploaded:
        print("\n新しく上げたものはありません。")
        return 0

    print(f"\n{len(uploaded)}本を「非公開」で上げました。")
    for jyo, video_id in uploaded:
        print(f"  {jyo}　https://studio.youtube.com/video/{video_id}/edit")

    print("\n  【公開する前に必ず】")
    print("    ・動画を通しで見ること（馬名の読み間違い、テロップのずれ）")
    print("    ・概要欄に VOICEVOX:春日部つむぎ が入っているか")
    print("    ・サムネイルが正しい競馬場のものか")
    print("\n  公開ボタンは YouTube Studio で自分で押してください。")
    print("  この道具は非公開までしか行いません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
