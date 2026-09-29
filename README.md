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
git clone <this repo> ccdeck && cd ccdeck
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
ccdeck url          # トークン付きのログイン URL を表示 (http://<このマシンのIP>:8787/?token=...)
ccdeck doctor       # 環境診断
```

URL を一度開くと Cookie が設定され、以後はトークン無しでアクセスできます。
トークン入力フォームからログインすることもできます。

## 使い方

### CLI

```bash
ccdeck new api --dir ~/src/api                  # claude を起動 (--session-id を付与して ID を記録)
ccdeck new web --dir ~/src/web --cmd "claude --model opus" --auto-continue
ccdeck ls                                       # 一覧 (状態 / 無出力時間 / claude セッションID / cwd)
ccdeck attach api                               # ターミナルから接続 (Ctrl-b d で離脱)
ccdeck send api "continue" --enter              # テキスト送信
ccdeck send api --key Escape                    # キー送信 (Enter, Escape, C-c, Up, Down, Tab, BTab ...)
ccdeck log api -n 500                           # 画面履歴 (tmux capture-pane)
ccdeck restart api [--fresh]                    # 再起動 (既定は会話を --resume で引き継ぐ)
ccdeck resume api | ccdeck resume --all         # dead を復旧
ccdeck kill api [--keep]                        # 停止して記録削除 (--keep は記録を残す)
ccdeck rename api backend
ccdeck serve                                    # Web UI + watchdog (通常は systemd から起動)
```

セッション名は `^[a-zA-Z0-9_-]{1,32}$` に制限されます。

### Web UI

- 左: セッション一覧。カードをタップするとターミナル(ttyd)が開きます。複数開くとグリッド表示(列数は右上で切替)
- 各ターミナルの下に **Enter / Esc / Ctrl+C / continue / y / n / 1 / 2 / ↑ / ↓ / Tab / ⇧Tab** ボタンとテキスト送信欄
  (スマホのソフトキーボードで打ちにくいキーを `tmux send-keys` 経由で送ります)
- 復旧 / 停止 / 再起動 / ログ / 設定(自動 continue) / 名前変更 / 削除
- WebSocket が切れたら指数バックオフ(1, 2, 4 … 最大 30 秒)で自動再接続。オンライン復帰・タブ復帰時は即再接続

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
cmd = "claude"          # 例: "claude --dangerously-skip-permissions"
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
- サービスのログ: `journalctl --user -u ccdeck -f`
- claude が見つからない: セッションは `$SHELL -lc '<cmd>'` で起動されます。ログインシェルの PATH に claude を入れるか、
  `--cmd /full/path/to/claude` を指定してください。サービスの環境は `~/.config/ccdeck/env`(`ccdeck setup --force` で再生成)
- tmux を直接触る: `tmux -L ccdeck ls` / `tmux -L ccdeck attach -t <name>`
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
