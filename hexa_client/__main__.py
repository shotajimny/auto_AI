"""Run with python3 -m hexa_client; no pip install is required."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .api import APIError
from .game import initial_state, normalize_setting, normalize_state, simulate
from .planner import plan_actions
from .server import ClientServer
from .session import Session


def read_json(path):
    with open(path, encoding="utf-8-sig") as stream:
        return json.load(stream)


def comma_ints(value):
    try:
        return [int(v.strip()) for v in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("カンマ区切りの整数で指定してください。") from None


def parser():
    result = argparse.ArgumentParser(description="ヘキサうどん競技クライアント")
    sub = result.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="日本語の操作画面を起動")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--demo", action="store_true", help="練習マップを読み込んで起動")
    serve.add_argument("--config", help="試合設定JSONを練習用に読み込む")
    serve.add_argument("--open", action="store_true", help="ブラウザを開く")
    for name in ("plan", "validate"):
        command = sub.add_parser(name, help="計画を生成" if name == "plan" else "計画を検証")
        command.add_argument("--config", required=True, help="公式MatchSettingまたは試合設定JSON")
        command.add_argument("--state", help="公式MatchState。省略時は初日の初期位置")
        command.add_argument("--kinds", type=comma_ints, help="初期種別（例 0,0,0,1）")
        command.add_argument("--known-brands", type=comma_ints, default=[])
        if name == "plan":
            command.add_argument("--output", help="回答JSONの出力先。省略時は標準出力")
        else:
            command.add_argument("--actions", required=True, help="回答JSON")
    play = sub.add_parser("play", help="競技サーバーに接続し各日の計画を自動提出")
    play.add_argument("--url", default="http://127.0.0.1:8080")
    play.add_argument("--token-env", default="PROCON_TOKEN", help="トークンを読む環境変数名")
    play.add_argument("--kinds", type=comma_ints, help="開始前に提出する種別。省略時はサーバー設定を維持")
    play.add_argument("--poll", type=float, default=1.0, help="状態確認の間隔（秒、最小0.5）")
    play.add_argument("--once", action="store_true", help="1日分を提出したら終了")
    return result


def auto_play(args):
    token = os.environ.get(args.token_env)
    if not token:
        raise ValueError(f"環境変数 {args.token_env} にトークンを設定してください。")
    session = Session()
    session.connect(args.url, token)
    if args.kinds is not None:
        session.set_agents(args.kinds)
        print("エージェント種別を提出しました。", file=sys.stderr)
    submitted = set()
    last_error = None
    failures = 0
    while True:
        if session.state is not None and not session.state_error:
            failures = 0
            day = session.state["day"]
            if day not in submitted:
                # A transmission timeout has ambiguous acceptance; never retry
                # a POST automatically. End the command for explicit recovery.
                try:
                    report = session.plan()
                    result = session.submit(report["actions"], day)
                except APIError as exc:
                    # 409 is generated locally BEFORE an action POST: either
                    # the day changed or the deadline guard stopped the send.
                    # Upstream HTTP failures are 502 and are never retried.
                    if exc.status != 409:
                        raise
                    print(str(exc), file=sys.stderr)
                    time.sleep(max(0.5, args.poll))
                    session.refresh()
                    continue
                submitted.add(day)
                print(json.dumps({"day": day, "revision": result["revision"],
                                  "actions": report["actions"]}, ensure_ascii=False), flush=True)
                if args.once or day + 1 == len(session.setting["daySteps"]):
                    return 0
        elif session.state_error:
            if session.state_error != last_error:
                print(session.state_error, file=sys.stderr)
                last_error = session.state_error
            if "match has ended" in session.state_error.lower():
                return 0
            failures += 1
            if failures >= 10 and (session.setting.get("startsAt", 0) > 0
                                   and time.time() > session.setting["startsAt"]):
                raise APIError("状態を継続して取得できないため自動運転を終了しました。接続を確認してください。")
        time.sleep(max(0.5, args.poll))
        session.refresh()


def main(argv=None):
    args = parser().parse_args(argv if argv is not None else sys.argv[1:] or ["serve"])
    try:
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("ポート番号は1〜65535で指定してください。")
            session = Session()
            if args.config:
                session.import_config(read_json(args.config))
            elif args.demo:
                session.demo()
            with ClientServer(("127.0.0.1", args.port), session) as server:
                url = f"http://127.0.0.1:{args.port}"
                print(f"ヘキサうどん: {url}\n終了: Ctrl+C", flush=True)
                if args.open:
                    import webbrowser
                    webbrowser.open(url)
                server.serve_forever()
        elif args.command in ("plan", "validate"):
            setting = normalize_setting(read_json(args.config))
            state = (normalize_state(setting, read_json(args.state)) if args.state
                     else initial_state(setting, args.kinds))
            if args.command == "plan":
                actions = plan_actions(setting, state, args.known_brands)
                report = simulate(setting, state, actions, args.known_brands, include_trace=False)
                if not report["valid"]:
                    raise ValueError("生成した行動計画の検証に失敗しました。")
                data = json.dumps(actions, ensure_ascii=False, indent=2) + "\n"
                if args.output:
                    Path(args.output).write_text(data, encoding="utf-8")
                else:
                    print(data, end="")
            else:
                report = simulate(setting, state, read_json(args.actions), args.known_brands,
                                  include_trace=False)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["valid"] else 2
        elif args.command == "play":
            return auto_play(args)
        else:
            parser().print_help()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, APIError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
