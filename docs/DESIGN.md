# ccdeck 設計

## 1. 全体構成

```
 ブラウザ (PC / スマホ)
   │  HTTP + WebSocket   (Cookie / Bearer トークン)
   ▼
┌──────────────────────── ccdeck serve (systemd --user) ─────────────────────────┐
│ front proxy (stdlib socketserver, bind=0.0.0.0:8787)                           │
│   ├─ /tty/*  … トークン検証 → ttyd へ生 TCP 中継 (WebSocket 含む)               │
│   │            + ttyd の Basic 認証ヘッダを付与、index.html に再接続フックを注入 │
│   └─ その他  … 内部 Flask (127.0.0.1:ランダムポート) へ中継                     │
│ Flask API  … /api/*  (セッション操作・状態取得)                                │
│ watchdog   … 5 秒周期: capture-pane → ハッシュ → 状態判定 → 自動 continue 等   │
│ ttyd 監視  … ttyd -i 127.0.0.1 -p 7682 -b /tty -a -P 30 -- ccdeck attach       │
└────────────────────────────────────────────────────────────────────────────────┘
   │ tmux -L ccdeck (専用ソケット, ~/.config/ccdeck/tmux.conf)
   ▼
 tmux server (systemd-run --user --scope で ccdeck.service とは別 cgroup)
   ├─ session "api"   : $SHELL -lc 'claude --session-id <uuid>'
   ├─ session "front" : $SHELL -lc 'claude --resume <uuid>'
   └─ ...
 状態: ~/.local/share/ccdeck/sessions.json (flock + atomic rename + .bak)
```

- tmux サーバーは serve プロセスとは独立しているので、`ccdeck serve` の再起動や
  ブラウザ/SSH 切断でセッションは死なない。
- `remain-on-exit on` により claude が終了してもペインは残り(`pane_dead=1`)、
  最後の出力を確認したうえで `respawn-pane` で同じ cwd に復旧できる。

## 2. ディレクトリ構成

```
ccdeck/                 Python パッケージ (stdlib + Flask のみ)
  cli.py                argparse による CLI (new/ls/attach/kill/restart/resume/serve/doctor/...)
  config.py             パス解決, config.toml 読込 (tomllib → 最小パーサにフォールバック), 初期生成
  store.py              sessions.json の排他・アトミック更新
  tmux.py               tmux ラッパー (常に配列渡し)
  claude.py             ~/.claude/projects/<エンコード cwd>/*.jsonl からの ID 推定, 起動コマンド組立
  detect.py             状態判定・rate limit 文言解析 (純粋関数, テスト対象)
  manager.py            高水準操作 (作成/停止/再起動/復旧/改名/キー送信/ログ/起動時突合)
  watchdog.py           周期監視 + 自動 continue / 自動復旧
  ttyd.py               ttyd プロセスの起動・監視
  proxy.py              フロントプロキシ (認証 + HTTP/WebSocket 中継)
  web.py                Flask アプリ (REST API)
  workspace.py          セッションごとの作業ディレクトリ (<root>/<ランダム10文字>) の作成・安全な削除
  permissions.py        権限プロファイル → Claude Code settings (--settings / --permission-mode)
  procs.py              /proc からのプロセスツリー・remote-control プロセス検出
  doctor.py             環境診断
  static/index.html     Web UI (単一 HTML + バニラ JS)
tests/                  pytest (単体 + tmux 統合テスト)
install.sh              apt install + zipapp 化して ~/.local/bin/ccdeck へ配置 + systemd 登録
```

`~/.local/bin/ccdeck` はパッケージを `python3 -m zipapp` で 1 ファイルにまとめた実行ファイル。

## 3. 状態遷移

```
             new / resume / restart
   (none) ─────────────────────────────► running ◄──────┐
                                          │  ▲          │ 出力変化
                   idle_seconds 無出力     ▼  │ 出力変化  │
                                          idle ─────────┤
                                          │             │
          確認プロンプト検出 (y/n, 1. Yes …)  ▼             │
                                     waiting-input ─────┤ 入力で解消
                                                        │
          "usage limit reached … resets 3pm" 検出        │
                                     rate-limited ──────┘ (リセット時刻 + margin 後、
                                          │                オプトインなら "continue" 自動送信)
     claude 終了 / クラッシュ / tmux 消失   ▼ (どの状態からでも)
                                         dead ──── resume ───► running
                                          │      (claude --resume <id> / --continue)
                              stop        ▼
                                  dead (stopped) … auto_restore の対象外
```

判定の優先順位: `dead` > `rate-limited` > `waiting-input` > `running` > `idle`

| 状態 | 判定条件 |
|---|---|
| dead | tmux セッション無し / `pane_dead=1` / 起動から 15 秒以上経過後にペインのフォアグラウンドがシェル |
| rate-limited | 画面末尾 12 行に limit 文言。リセット時刻を解析(過去 15 分以上前なら無視) |
| waiting-input | 画面末尾に確認プロンプト (`Do you want to proceed?`, `❯ 1. Yes`, `(y/n)` …) |
| running | `esc to interrupt` 表示中、または最終出力から `idle_seconds` 未満 |
| idle | 上記以外 |

加えて `stalled` フラグ: dead 以外で無出力が `stall_seconds` を超えたら UI で警告。
`auto_continue.stall` を有効にしたセッションのみ、無出力 1 エピソードにつき 1 回 `continue` を送信。

## 4. 復旧ロジック

1. 記録済み `claude_session_id` の jsonl が存在 → `claude --resume <id>`
2. 無ければ、同じ cwd の jsonl のうち他セッションが使っていない最新のものを推定 → `--resume <推定id>`
3. jsonl はあるが推定不能 → `claude --continue`
4. jsonl が一つも無い → 新規起動 (`--session-id` 付き)

元コマンドの `--resume/-r/--continue/-c/--session-id` は除去してから付け直す。
claude 以外のコマンド(`--cmd "htop"` 等)は元コマンドをそのまま再実行。

`ccdeck serve` 起動時: sessions.json と `tmux list-panes -a` を突き合わせ、
`auto_restore=true` かつ `stopped=false` の dead セッションを復旧(tmux サーバー自体が
消えていても sessions.json から全再作成)。

## 5. API 一覧

すべて `Authorization: Bearer <token>` または Cookie `ccdeck_token` が必要
(`/api/health` と `POST /api/login` を除く)。Cookie 認証での変更系リクエストは
`X-CCDeck: 1` ヘッダ必須 (CSRF 対策)。

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/` | Web UI (`?token=` 付きなら Cookie を設定してリダイレクト) |
| GET | `/api/health` | 死活 |
| POST | `/api/login` / `/api/logout` | `{token}` → Cookie 設定 / 削除 |
| GET | `/api/config` | 既定値 (cmd, dir) など |
| GET | `/api/sessions` | 一覧 (状態, 経過秒, cwd, rate limit 解除時刻 …) |
| POST | `/api/sessions` | 作成 `{name, dir, cmd, auto_restore, auto_continue, permissions}`(dir 省略時は `<root>/<ランダム>` を作成) |
| GET | `/api/sessions/<name>` | 詳細 |
| PATCH | `/api/sessions/<name>` | 設定変更 `{auto_restore, auto_continue:{rate_limit, stall, text}, permissions}` |
| DELETE | `/api/sessions/<name>?keep_dir=1` | 停止して記録削除(ccdeck が作った作業ディレクトリも削除) |
| GET/PUT | `/api/settings` | `{workspace_root}` 作業ディレクトリのルート |
| POST | `/api/sessions/<name>/stop` | 停止 (記録は残す) |
| POST | `/api/sessions/<name>/restart` | 再起動 `{fresh:false}` (既定は会話を resume) |
| POST | `/api/sessions/<name>/resume` | 復旧 (dead のときのみ) |
| POST | `/api/sessions/<name>/rename` | 改名 `{new_name}` |
| POST | `/api/sessions/<name>/keys` | `{key:"Enter"|"Escape"|"C-c"|…}` or `{text, enter}` |
| GET | `/api/sessions/<name>/log?lines=N` | `tmux capture-pane` の履歴 |
| POST | `/api/restore-all` | dead をすべて復旧 |
| * | `/tty/?arg=<name>` | ttyd (ブラウザ端末) へのプロキシ |

## 6. CLI

```
ccdeck new <name> [--dir PATH] [--cmd "claude ..."] [--no-auto-restore] [--auto-continue]
ccdeck ls [--json]
ccdeck attach <name>
ccdeck kill <name> [--keep]
ccdeck restart <name> [--fresh]
ccdeck resume <name> | --all
ccdeck rename <old> <new>
ccdeck send <name> (--key KEY | TEXT [--enter])
ccdeck log <name> [-n LINES]
ccdeck start | stop [--all] | status [--json]   # Web UI サーバー制御 (systemd or バックグラウンド)
ccdeck serve [--bind ADDR] [--port N]           # フォアグラウンド実行
ccdeck doctor
ccdeck setup      # 設定・tmux.conf・systemd unit 生成 (install.sh が呼ぶ)
ccdeck url        # トークン付きログイン URL を表示
```
