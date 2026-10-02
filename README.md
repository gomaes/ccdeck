# ccdeck

Linux 上で複数の Claude Code セッションを並行稼働させ、ブラウザ(スマホ含む)から一覧・操作するためのツールです。

- **死なない**: セッションは専用の tmux サーバー (`tmux -L ccdeck`) 上で動くので、SSH / ブラウザ切断、ネットワーク断、Web サーバー再起動の影響を受けません
- **戻せる**: claude が落ちた・マシンが再起動した場合も、`sessions.json` から同じ cwd で `claude --resume <id>` して会話ごと復旧します
- **見える**: running / idle / waiting-input / rate-limited / dead の状態、最終出力からの経過時間、使用量制限の解除時刻を一覧表示
- **軽い**: Python 標準ライブラリ + Flask (apt の `python3-flask`) + tmux + ttyd のみ。ビルド不要、外部通信・テレメトリなし

設計(構成図・状態遷移図・API 一覧)は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。

## 動作環境

- Ubuntu 22.04+ / Debian 12+(systemd、apt)
- 依存: `tmux` (3.0+) / `ttyd` / `python3` (3.10+) / `python3-flask` — すべて apt で入ります
- Claude Code 本体はインストール済みのものを使います(`claude` が PATH にあること)

## セットアップ

```bash
git clone https://github.com/gomaes/ccdeck && cd ccdeck
./install.sh
```

`install.sh` が行うこと:

1. `sudo apt-get install tmux ttyd python3 python3-flask`(不足分のみ)
   - パッケージの ttyd は `ttyd -O login` を localhost:7681 で動かす system サービスを有効化するため、
     install.sh が新規に ttyd を入れた場合はこれを無効化します(残したい場合は `--keep-system-ttyd`)
2. `ccdeck/` パッケージを `python3 -m zipapp` で 1 ファイルにまとめ `~/.local/bin/ccdeck` に配置
3. `ccdeck setup`: `~/.config/ccdeck/{config.toml,tmux.conf,env}` と `~/.config/systemd/user/ccdeck.service` を生成
   - `config.toml` にはランダムなトークンが書かれます (パーミッション 600)
   - `env` にはインストール時の `PATH` / `SHELL` が保存され、サービスから claude が見つかるようにします
4. `sudo loginctl enable-linger $USER`(ログアウト後・再起動後もサービスが動くように)
5. `systemctl --user enable --now ccdeck` → `ccdeck doctor`

オプション: `--no-apt` / `--no-service` / `--no-linger` / `--keep-system-ttyd` / `--uninstall`

インストール後:

```bash
ccdeck              # 状態表示 (= ccdeck status)
ccdeck start        # Web UI サーバーを起動
ccdeck status       # サーバー / セッションの状態
ccdeck stop         # Web UI サーバーを停止 (Claude セッションは tmux で動き続ける)
ccdeck stop --all   # セッションも含めてすべて停止 (再開: ccdeck start && ccdeck resume --all)
ccdeck url          # トークン付きのログイン URL を表示 (http://<このマシンのIP>:8787/?token=...)
ccdeck doctor       # 環境診断
```

URL を一度開くと Cookie が設定され、以後はトークン無しでアクセスできます。
トークン入力フォームからログインすることもできます。

## 使い方

### CLI

```bash
ccdeck new api                                  # ~/claude/<ランダム> を作って claude を起動
ccdeck new api2 --dir ~/src/api                 # 既存ディレクトリで起動 (--session-id を付与して ID を記録)
ccdeck new web --dir ~/src/web --cmd "claude --model opus" --auto-continue
ccdeck ls                                       # 一覧 (状態 / 無出力時間 / claude セッションID / cwd)
ccdeck attach api                               # ターミナルから接続 (Ctrl-b d で離脱)
ccdeck send api "continue" --enter              # テキスト送信
ccdeck send api --key Escape                    # キー送信 (Enter, Escape, C-c, Up, Down, Tab, BTab ...)
ccdeck log api -n 500                           # 画面履歴 (tmux capture-pane)
ccdeck restart api [--fresh]                    # 再起動 (既定は会話を --resume で引き継ぐ)
ccdeck resume api | ccdeck resume --all         # dead を復旧
ccdeck kill api [--keep] [--keep-dir]           # 停止して記録と作業ディレクトリを削除 (--keep は記録を残す)
ccdeck workspace [PATH]                         # 作業ディレクトリのルートを表示 / 変更
ccdeck rename api backend
ccdeck start / stop [--all] / status [--json]    # Web UI サーバーの起動・停止・状態確認
ccdeck serve                                    # Web UI + watchdog をフォアグラウンドで実行 (start / systemd が使用)
```

### セッション名(日本語可)と claude 側の名前

- 名前には日本語など任意の文字が使えます(64 文字まで、制御文字不可、重複不可)。
  内部 ID(tmux セッション名・URL)は `^[a-zA-Z0-9_-]{1,32}$` に制限され、
  名前がそのまま ID に使えない場合は自動生成されます(例: `テスト` → ID `s-5d520e`、`api サーバー` → `api`)
- CLI では名前・ID のどちらでも指定できます: `ccdeck attach テスト` / `ccdeck attach s-5d520e`
- ccdeck の名前は claude にも渡されます(`[claude] name_sessions = true`、既定で有効)
  - `claude rc` / `claude remote-control` → `--name <名前>`: **claude.ai/code の一覧に同じ名前で表示**
  - 対話の `claude` → `--name <名前>`(プロンプト欄・/resume 一覧・端末タイトル)。`--rc` 単体なら Remote Control 名にも
  - `--cmd` に `--name` を自分で書いた場合はそちらが優先されます
- 名前変更は ccdeck 上には即反映されますが、claude 側の名前は **次の再起動(「再起動」ボタン / `ccdeck restart`)** で反映されます
- `claude rc` セッションの復旧・再起動は、同じ名前で新しい Remote Control セッションを作り直します
  (rc の `--session-id` / `--continue` は会話 ID ではなく RC セッションの再接続用なので、ccdeck は付けません)

### 作業ディレクトリ(セッションごとに自動作成・削除)

- 新規作成時にディレクトリを指定しなければ、**作業ディレクトリのルート(既定 `~/claude`)直下に
  ランダムな名前のディレクトリ**を作り(例: `~/claude/d2evzq4b6k`、パーミッション 700)、そこがセッションの cwd になります
  - 名前は `abcdefghjkmnpqrstuvwxyz23456789`(0/o, 1/l/i など紛らわしい文字を除いた 31 文字)から **10 文字**(約 49 ビット)
- **セッションを削除するとこのディレクトリも中身ごと削除**されます(`ccdeck kill NAME --keep-dir` で残せます)
  - 削除するのは ccdeck が作ったディレクトリだけです。既存ディレクトリを指定したセッション、シンボリックリンク、
    ルート直下でないもの、ランダム名の形式でないもの、他のセッションも使っているものは削除しません
- ルートは Web UI の「⋯」メニュー、または `ccdeck workspace PATH` で変更できます(`<data>/settings.json` に保存)
- 既存のディレクトリで作業したい場合は、新規作成画面で「既存のディレクトリを使う」を選ぶか `ccdeck new NAME --dir PATH`

### 権限(セッションごと)

新規作成画面の「権限」、または各セッションの「設定」で選べます(変更は次の再起動で反映)。
Claude Code の設定ファイル (`--settings`) とフラグに変換して渡します。

| 項目 | 選択肢 | 対応する Claude Code の設定 |
|---|---|---|
| モード | 都度確認 / 編集は自動許可 / 計画のみ / 許可済み以外は自動拒否 / すべて許可(危険) | `--permission-mode` (`default` `acceptEdits` `plan` `dontAsk` `bypassPermissions`) |
| 読み取り範囲 | 作業ディレクトリのみ(外は都度確認) / ホーム全体 / どこでも | `permissions.allow`: `Read(~/**)` / `Read(//**)` |
| 追加ディレクトリ | 読み書きを許可するディレクトリ | `permissions.additionalDirectories` |
| 禁止パス | 読み書き禁止(既定: `~/.ssh` `~/.aws` `~/.gnupg` `~/.config/gh` `~/.netrc` `~/.docker/config.json`) | `permissions.deny`: `Read(...)` `Edit(...)`(+ sandbox の denyRead/denyWrite) |
| Bash | 都度確認 / サンドボックス内で自動実行 / 禁止 / 常に許可 | `sandbox.enabled` + `autoAllowBashIfSandboxed` / `deny: Bash` / `allow: Bash` |
| Web | WebFetch / WebSearch の許可 | `permissions.deny: WebFetch, WebSearch` |

- 書き込みは作業ディレクトリと追加ディレクトリに限られます(Claude Code の作業ディレクトリの仕組み)。deny は allow より優先されます
- `~/.config/ccdeck`(Web UI のトークン)は常に禁止です。「すべて許可」以外では bypassPermissions への切替も無効化します
- **サンドボックス**は OS レベルで Bash コマンドのファイル書き込み・ネットワークを制限します。`bubblewrap` と `socat` が必要です
  (install.sh が入れます)。Ubuntu 24.04 以降で AppArmor が非特権ユーザー名前空間を制限している場合は動かないことがあります
- `claude rc` の場合、モードは rc が起動するセッションに `--permission-mode` で適用され、詳細設定は ccdeck が作成した
  作業ディレクトリの `.claude/settings.local.json` に書き込まれます(既存ディレクトリではモードのみ)
- 既定の権限は config.toml の `[defaults.permissions]` で変更できます(例: `mode = "acceptEdits"`)
- `--cmd` に `--permission-mode` / `--dangerously-skip-permissions` / `--settings` を自分で書いた場合はそちらが優先されます
- CLI: `ccdeck new NAME --mode acceptEdits --read-scope home --add-dir ~/src/lib --deny-path ~/secret --bash sandbox --no-web`

### 既定のコマンドは `claude rc`

- 新規セッションは既定で `claude rc`(Remote Control)として起動し、ccdeck の Web UI に加えて claude.ai/code や
  Claude アプリからも操作できます。対話型の claude にしたい場合は新規作成画面のコマンドを `claude` に変えるか、
  config.toml の `[defaults] cmd` を変更してください
- 以前のインストールで生成された config.toml の `cmd = "claude"` は、一度だけ自動で `claude rc` に更新されます
  (その後 `claude` に戻した場合はそのまま尊重されます)

### Web UI

- 左: セッション一覧。カードをタップするとターミナル(ttyd)が開きます。複数開くとグリッド表示(列数は右上で切替)。
  開いているターミナルは画面の高さに収まるよう自動で分割され、**最大 4 つ**まで表示します。5 つ目を開くと最初に開いたものが閉じます
  (閉じるのは表示だけで、セッションは動き続けます)
- 各ターミナルの下に **Enter / Esc / Ctrl+C / continue / y / n / 1 / 2 / ↑ / ↓ / Tab / ⇧Tab** ボタンとテキスト送信欄
  (スマホのソフトキーボードで打ちにくいキーを `tmux send-keys` 経由で送ります)
- 復旧 / 停止 / 再起動 / ログ / 設定(自動 continue) / 名前変更 / 削除
- WebSocket が切れたら指数バックオフ(1, 2, 4 … 最大 30 秒)で自動再接続。オンライン復帰・タブ復帰時は即再接続

### リソース表示(セッションごとの CPU / メモリ / ディスク)

各セッションのカードとターミナルの見出しに `CPU 12% · メモリ 350MB · ディスク 1.2GB` を表示します
(`ccdeck serve` の watchdog が計測。外部コマンド・追加パッケージ不要)。

| 項目 | 計測方法 | 計測するタイミング |
|---|---|---|
| CPU | tmux ペインのプロセスツリー全体(claude と rc が起動したセッションを含む)の CPU 時間の増分。**1 コア使い切りで 100%** | **running のセッションだけ** 5 秒ごと。idle / 入力待ち / 制限中 / 停止中は計測も表示もしない |
| メモリ | 同じプロセスツリーの PSS 合計(共有メモリを按分するので二重計上しない。読めない場合は RSS) | running は 5 秒ごと、それ以外は `mem_idle_interval`(既定 60 秒)ごと |
| ディスク | セッションのルートディレクトリの使用量(`du -sx` 相当: ハードリンクは 1 回、シンボリックリンクは辿らない、別ファイルシステムは数えない) | `disk_interval`(既定 60 秒)ごと。新規セッションは作成直後に 1 回 |

- 計測は既存の watchdog の周期(5 秒)の中で行い、計測のためだけに起きるタイマーは増やしていません
  (ディスク計測スレッドは 60 秒周期と新規作成時の通知だけで起きます)。動いていないセッションの CPU を
  計測しないのも、無駄な処理で CPU を起こさない(省電力状態を妨げない)ためです
- 大きなディレクトリは 1 回あたり `disk_max_seconds`(既定 20 秒)で打ち切り、`≥4.2GB` のように下限として表示します
- CPU が 80% 以上のときはオレンジで表示します。項目にカーソルを合わせると内訳(プロセス数、PSS/RSS、計測時刻)が出ます
- 注意: `claude rc` は、リモートからのセッションが動いていてもターミナルの表示がほとんど変わらないため idle と判定されやすく、
  その間 CPU は表示されません

### 状態の意味

| 状態 | 意味 |
|---|---|
| running | 出力が続いている / `esc to interrupt` が表示中 |
| idle | `idle_seconds`(既定 20 秒)以上出力なし |
| waiting-input | 許可確認などのプロンプト (`Do you want to proceed?`, `❯ 1. Yes`, `(y/n)` …) を表示中 |
| rate-limited | 使用量制限メッセージを検出。解除時刻を解析して表示 |
| dead | claude が終了した / tmux セッションが無い(停止したものは `dead (stopped)`) |

無出力が `stall_seconds`(既定 900 秒)を超えると ⚠ 警告を表示します。

### 自動 continue(セッションごとにオプトイン)

- **rate limit**: `Claude usage limit reached … resets 3pm (Asia/Tokyo)` のような文言からリセット時刻を解析し、
  時刻 + `rate_limit_margin`(60 秒)後に `continue` + Enter を 1 回送信します。時刻が読めない場合は
  `rate_limit_unknown_retry`(30 分)ごとに送信します
- **stall**: 無出力が `stall_seconds` を超えたら 1 回だけ送信(出力が再開するまで再送しません)

いずれも既定では無効です。UI の「設定」または `ccdeck new --auto-continue` で有効にします。

### サーバーの起動・停止 (`start` / `stop` / `status`)

- systemd の user unit がインストール済みで使える場合は `systemctl --user start|stop ccdeck` を実行します
  (ログ: `journalctl --user -u ccdeck`)
- systemd --user が使えない環境では `ccdeck serve` をバックグラウンドで起動します
  (pid: `~/.local/share/ccdeck/run/serve.pid`、ログ: `~/.local/share/ccdeck/serve.log`)。
  この場合、マシン再起動後の自動起動はされないので `ccdeck start` を実行してください
- `stop` は Web UI だけを止めます。claude は tmux 上で動き続け、次の `start` 時にそのまま表示されます
- `status` の終了コードは起動中 0 / 停止中 3 です

## 復旧の仕組み

- tmux は `remain-on-exit on` で動かしているため、claude が終了してもペインが残り `dead` と判定されます
- 復旧コマンドは次の順で決まります
  1. 記録している claude セッション ID の transcript (`~/.claude/projects/<cwd を英数字以外 - に置換>/<id>.jsonl`) があれば `claude --resume <id>`
  2. 無ければ同じ cwd の transcript のうち他の ccdeck セッションが使っていない最新のもの
  3. それも無ければ `claude --continue`、transcript が一つも無ければ新規起動
- 新規作成時は `claude --session-id <uuid>` で ID を確定させます。`/clear` などで ID が変わった場合も
  watchdog が 30 秒ごとに transcript から推定し直します
- `ccdeck serve` の起動時に `sessions.json` と tmux の実態を突き合わせ、`auto_restore = true` のセッションを復旧します
  (マシン再起動で tmux サーバーごと消えた場合も全部再作成)。稼働中にクラッシュしたものも自動復旧したい場合は
  `[watchdog] auto_restore_dead = true`(1 時間に 5 回までの再起動制限付き)
- tmux サーバーは `systemd-run --user --scope` で ccdeck.service とは別の cgroup で起動するため、
  `systemctl --user restart ccdeck` でもセッションは止まりません

## 設定 (`~/.config/ccdeck/config.toml`)

主な項目(全項目と既定値は生成されたファイルを参照):

```toml
[server]
bind = "0.0.0.0"        # 既定は全インターフェース (LAN / Tailscale から直接アクセス可)
port = 8787
allow_external = true   # 127.0.0.1 以外に bind するには true が必要 (安全スイッチ)
token = "..."           # Bearer / Cookie 用トークン
cookie_secure = false   # HTTPS 経由 (tailscale serve 等) で使うなら true

[defaults]
cmd = "claude rc"       # 既定。対話の claude にするなら "claude"(例: "claude --model opus")
dir = "~"

[watchdog]
idle_seconds = 20
stall_seconds = 900
auto_restore_dead = false
```

変更後は `systemctl --user restart ccdeck`(tmux セッションは影響を受けません)。

## セキュリティ

ccdeck の Web UI は **あなたのユーザー権限で任意のコマンドを実行できる端末そのもの** です。取り扱いに注意してください。

- Web UI は既定で **`0.0.0.0:8787`(全インターフェース)** で待ち受けます。同じネットワーク上の誰でも
  ログイン画面には到達できるため、トークンの管理に注意し、必要に応じてファイアウォールで制限してください
  (例: `sudo ufw allow from 192.168.1.0/24 to any port 8787` / `sudo ufw allow in on tailscale0 to any port 8787`)
- ローカルのみにしたい場合は `bind = "127.0.0.1"` に変更してください。127.0.0.1 以外に bind する場合は
  `allow_external = true` が必要です(既定で true。false にすると外部 bind を拒否します)
- 通信は平文 HTTP です。信頼できないネットワーク(公衆 Wi-Fi・インターネット直結)ではトークンが盗聴され得るため、
  Tailscale(WireGuard で暗号化)や SSH ポートフォワード経由で使うか、`tailscale serve` 等で HTTPS 化してください
- すべての API・端末アクセスはトークン(`Authorization: Bearer` または HttpOnly / SameSite=Strict Cookie)で保護され、
  Cookie 認証の変更系リクエストには `X-CCDeck: 1` ヘッダを必須にして CSRF を防いでいます
- ttyd は `127.0.0.1` でのみ待ち受け、ccdeck のフロントプロキシがトークンを検証してから WebSocket を含めて中継します
  (1 接続 1 リクエストに制限し、すべてのリクエストで認証を確認)
  - さらに ttyd 自体にも起動ごとにランダムな Basic 認証 (`-c`) を設定し、プロキシが付与します。
    ただしこの値は `ps` から同一マシンの他ユーザーに見えるため、**マルチユーザー環境での主防御にはなりません**。
    共有マシンでは信頼できないユーザーがいないことを前提にしてください
- セッション名は正規表現で制限し、subprocess は常に配列渡し(シェルを経由しません)。
  セッションのコマンド (`--cmd`) だけは意図的にあなたのログインシェル (`$SHELL -lc`) で実行されます
- `config.toml` (トークン) は 600、設定・データディレクトリは 700 で作成されます
- 外部への通信・テレメトリは一切ありません

### リモート(スマホ等)からアクセスする場合

同じ LAN / tailnet 内なら `http://<マシンのIP>:8787/` で直接アクセスできます(`ccdeck url` が候補を表示)。
ポートをインターネットに直接公開(ルーターのポート開放など)はしないでください。推奨:

- **Tailscale**: tailnet 内から `http://<tailscale IP>:8787/`。HTTPS にしたい場合は `bind = "127.0.0.1"` にして
  `tailscale serve --bg 8787`(この場合 `cookie_secure = true` を推奨)
- **SSH ポートフォワード**: `ssh -N -L 8787:127.0.0.1:8787 user@host` → 手元のブラウザで `http://127.0.0.1:8787/`

## トラブルシューティング

- `ccdeck doctor` で tmux / ttyd / claude / linger / ポート / 権限を確認できます
- 他の PC から開けない: `ccdeck doctor` の `bind address` を確認してください。`127.0.0.1` なら
  `./install.sh --bind 0.0.0.0`(または `ccdeck setup --bind 0.0.0.0 && systemctl --user restart ccdeck`)。
  `0.0.0.0` なのに繋がらない場合はファイアウォールを確認(例: `sudo ufw allow 8787/tcp`)
  - v0.1.0 の初回インストールで生成された未編集の config.toml は、再インストール時に自動で `0.0.0.0` に移行します
- サービスのログ: `journalctl --user -u ccdeck -f`
- claude が見つからない: セッションは `$SHELL -lc '<cmd>'` で起動されます。ログインシェルの PATH に claude を入れるか、
  `--cmd /full/path/to/claude` を指定してください。サービスの環境は `~/.config/ccdeck/env`(`ccdeck setup --force` で再生成)
- tmux を直接触る: `tmux -L ccdeck ls` / `tmux -L ccdeck attach -t <name>`
- `claude rc` で `Error: This folder is already served by a terminal claude remote-control on this device`:
  同じフォルダで別の `claude rc` が動いています(以前の実行の取り残しを含む)。`ccdeck doctor` の
  `remote-control pid ...` 行に、ccdeck 管理外の rc プロセスと `kill <pid>` のヒントが表示されます。
  ccdeck は同じフォルダで rc が動いているときは新規作成・再起動を止めてエラーを返します
- `claude rc` で毎回 `Error: CCR v2 worker registration failed for session cse_... 404` が出る
  (同じ `cse_...` ID が繰り返し出る): 以前の Remote Control セッションが正常終了せず、その ID が
  `~/.claude/projects/<cwd の英数字以外を - に置換>/bridge-pointer.json` に残っている可能性があります。
  claude rc を止めてからこのファイルを退避(`mv bridge-pointer.json bridge-pointer.json.bak`)して起動し直してください。
  同じ ID が繰り返し出る場合は、上記の「別の rc が動いている」状態も疑ってください。
  ccdeck は再起動・停止・削除のとき、まず Ctrl+C を送って claude の終了を最大 `[claude] exit_timeout` 秒(既定 10)待つので、
  これ以降は起きにくくなっています(それでも残ったプロセスは、ペインのプロセスツリーごと終了させます)
- 状態判定は Claude Code の画面文言に依存するヒューリスティックです。Claude Code の表示が変わると
  waiting-input / rate-limited の判定がずれることがあります(`ccdeck/detect.py` のパターンで調整できます)

## 開発

```bash
python3 -m pytest -q            # 単体テスト + tmux 統合テスト (tmux が必要)
python3 -m ccdeck serve --port 8788   # ソースから直接起動
```

- `tests/test_detect.py`: 状態判定と rate limit 文言解析
- `tests/test_restore.py`: セッション ID 推定・復旧コマンド決定・起動時突合・watchdog(自動 continue 等)
- `tests/test_web.py`: 認証 / CSRF / API / プロキシ補助関数 / 設定
- `tests/test_integration_tmux.py`: 実際の tmux + ダミーの長時間プロセス(偽 claude)で
  クラッシュ → dead → `--resume` 復旧、tmux サーバー消失 → 全復旧 を検証

## アンインストール

```bash
./install.sh --uninstall   # バイナリと systemd unit を削除 (設定・sessions.json・tmux セッションは残す)
```
