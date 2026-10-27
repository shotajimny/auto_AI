# ヘキサうどん クライアント

第37回高専プロコン競技部門「ヘキサうどん」の競技者用クライアントです。公式HTTP APIへの接続、六角マップの表示、種別決定、行動計画の編集・検証・提出、自動計画を行えます。

**Python 3.10以上があれば利用できます。外部Pythonライブラリ、Node.js、ビルド処理は不要です。**

```sh
git clone https://github.com/shotajimny/auto_AI.git
cd auto_AI
python3 -m hexa_client serve --demo
```

ブラウザで [http://127.0.0.1:8765](http://127.0.0.1:8765) を開いてください。Windowsでは `python3` の代わりに `py` を使えます。終了はターミナルで `Ctrl+C` です。

## 画面の使い方

1. 「サンプルで試す」、または「試合設定を読み込む」で練習を開始します。
2. エージェント種別を選びます。`0` はうどんを集める巡回車、`1` は燃料を補給する補給車です。
3. 「自動でプランを生成」、方向ボタン、待機、JSON編集で全車の計画を作ります。
4. 「プランを検証」で盤外・池・燃料不足・ステップ数・収集見込みを確認します。
5. 本番接続では「回答を提出」、練習では「翌日へ」で進めます。回答JSONの保存・読み込みもできます。

種別は全車分をまとめて送信します。手動編集した計画は、自動更新で日が変わった場合にも無断で提出されません。現在の日に合わせて再生成・検証してください。

## 公式サーバーへの接続

画面の接続設定に競技サーバーURLとチームトークンを入力します。画面からはローカルPythonサーバーを経由して公式APIを呼び出すため、競技サーバー側のCORS設定は不要です。

同梱の動作確認サーバーを利用する場合の例です。

```sh
# 別ターミナル。サーバーと試合設定は大会配布ファイルを利用
./procon-server-darwin-arm64 \
  -config example.json \
  -addr 127.0.0.1:8080 \
  -kind-deadline 60s \
  -match-start-delay 10s

# クライアント
python3 -m hexa_client serve
```

画面に `http://127.0.0.1:8080` と設定JSON内の自チームの `token` を入力します。Linux・Windowsでは対応する配布バイナリに置き換えてください。開始前でも設定と初期位置を表示でき、当日の状態は開始後に取得します。

トークンは `Procon-Token` ヘッダーで送り、サーバーのメモリ内だけで保持します。ファイル・ブラウザのローカルストレージ・アクセスログには保存しません。クライアント画面はループバックアドレスだけで公開します。

公式サーバーへの要求は250ms以上間隔を空けて直列化します。状態の自動更新は設定と当日状態の2要求です。他クライアントとの合計頻度にも注意してください。POSTがタイムアウトした場合は受理状況が不明なため自動再送しません。

提出直前に当日の状態を取得し直し、送信待ちの後にも締切を確認します。残り750ms以下では提出を中止します。公式APIに日付を指定する欄がないため、ネットワーク遅延による日付またぎを完全に排除することはできません。端末の時計を合わせ、締切に余裕を持って提出してください。

## コマンドライン

GUIを起動せず、設定JSONから回答を生成できます。配布の `{ "problem": ..., "teams": ... }` 形式、問題オブジェクト単体、公式の `MatchSetting` 形式を受け付けます。

```sh
python3 -m hexa_client plan --config examples/practice.json --output answer.json
python3 -m hexa_client validate --config examples/practice.json --actions answer.json

# 2日目以降は、その日に取得した公式MatchStateを指定
python3 -m hexa_client plan --config setting.json --state state.json --output answer.json

# 種別・既に取得した系列を指定
python3 -m hexa_client plan --config examples/practice.json --kinds 0,0,0,1 --known-brands 0,2
```

`validate` は正常な計画なら終了コード0、不正なら2を返します。JSONの形式不正・接続失敗等は1です。生成する回答は公式形式の整数二次元配列です。

各日の計画生成と提出を自動で行う場合は次を実行します。**`play` は実際に競技サーバーへ提出します。** 同じ日に一度だけ送信し、次の日に切り替わるまで待ちます。種別を省略すると既存の種別を維持します。

```sh
# トークンをコマンド引数に含めず設定（bash / zsh）
read -s PROCON_TOKEN
export PROCON_TOKEN
python3 -m hexa_client play --url http://127.0.0.1:8080 --kinds 0,0,0,1

# 当日分の提出だけで終了
python3 -m hexa_client play --url http://127.0.0.1:8080 --once
```

`--kinds` は試合開始前の種別受付中にだけ指定してください。既に試合が始まっている場合は省略します。GUIと自動運転を同時に同じトークンで利用すると、後から受理された回答が上書きするため、一方を提出担当にしてください。

## ルールと自動計画の範囲

- マップは偶数行が右にずれる六角格子です。移動方向は `0:左上, 1:右上, 2:右, 3:右下, 4:左下, 5:左`、負数 `-n` はnステップ待機です。
- 出発セルにより移動の時間・燃料を計算します。平地2ステップ/燃料1、山地3/2、道路は順調1・混雑2・渋滞4ステップ/燃料2です。補給車は燃料を消費しません。
- 移動の完了時に燃料を消費し、その後にうどん取得・燃料補給を反映します。全車を同時にシミュレートします。
- 全車それぞれの所要ステップ数が、その日の `daySteps[day]` と一致しなければ提出しません。
- 自動計画は未収集系列を優先する最短路ベースの貪欲法です。生成後にもルール検証を行います。最適解・大会での勝利を保証するものではありません。
- 練習の翌日進行では他チームを動かさず、道路状態を固定します。本番では毎日の公式状態を取得し直します。
- 画面の獲得数・系列数はローカル予測です。公式APIは確定した得点を返さないため、公式順位・確定得点の表示には使えません。

仕様の出典・PDFとOpenAPIの相違点は [docs/protocol.md](docs/protocol.md) に記載しています。大会配布のPDF・サーバーバイナリ・実際のチームトークンはこのリポジトリに含めません。

## 開発・検証

```sh
python3 -m unittest discover -v
python3 -m compileall -q hexa_client
node --check hexa_client/static/app.js  # JSを変更する場合。実行時にはNode不要
```

公式サーバーとの結合テストは `HEXA_OFFICIAL_SERVER` に手元の配布バイナリの絶対パスを指定して実行します。通常のテストではスキップされます。

```sh
HEXA_OFFICIAL_SERVER=/path/to/procon-server-darwin-arm64 \
  python3 -m unittest tests.test_official_integration -v
```

`hexa_client/game.py` がルールと同時シミュレーション、`planner.py` が自動計画、`api.py` が公式通信、`session.py` が日付・受理状態の管理、`server.py` と `static/` が画面です。探索を改善する場合は `plan_actions(setting, state, known_brands)` を差し替えられます。
