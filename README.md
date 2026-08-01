# Web Terminal（浏览器访问本机终端）

通过 **ttyd + tmux + 会话管理页 + Cloudflare Tunnel + LaunchAgent**，用浏览器打开本机 shell。

- 管理页：https://term.lucadesign.uk/
- 终端：https://term.lucadesign.uk/term/?arg=main
- 本机管理：http://127.0.0.1:7690/
- 本机终端：http://127.0.0.1:7681/term/
- 项目目录：`~/web-terminal`

## 能力

- **断线重连**：终端页自动退避重连；网络恢复 / 标签页重新可见时立即重试
- **会话保留**：每个连接挂到独立 `tmux` 会话（`wt-*`），浏览器断开不会杀进程
- **会话管理**：首页可查看当前会话、历史（已停止）、新建 / 打开 / 停止
- **进入 PIN**：打开/新建终端需输入 `.env` 的 `SESSION_PIN`（与 Basic Auth 分离；界面不提示规则）
- **工作目录**：新建时可指定路径，且必须在 `SESSION_PATH_ROOT` 下；管理页支持按名称/路径关键字检索
- **标签页图标**：终端页（绿）/ 管理页（蓝）各有高对比 favicon —— ttyd 自带图标是黑底黑字，深色标签栏里等于没图标
- **粘贴图片**：按客户端系统分流键位 —— macOS 客户端 `⌘V`/`Ctrl+V` 贴图（沿用本机剪贴板）；Windows/Linux 客户端 `Ctrl+V` 贴文本、`Alt+V` 贴图（剪贴板 API 不可用时弹引导浮层）、`Ctrl+Alt+V` 发原始 `^V` 给 vim 等
- **回看页数**：管理页「全局回看」设全局默认（存 `run/scrollback-pages`，即时生效）；每个会话可单独配置，留空/点「跟随全局」即不配置、自动跟随全局默认

## 依赖

- `ttyd` / `cloudflared` / `tmux` / `python3`（Homebrew）

## 快速开始

```bash
cd ~/web-terminal
cp .env.example .env   # 仓库只带模板；密钥只写进本地 .env（已 gitignore）
# 编辑 .env：TTYD_PASSWORD、SESSION_PIN、PUBLIC_HOST 等
./bin/start.sh
./bin/status.sh
```

浏览器打开你的域名，用 `.env` 里的账号密码登录管理页，进入终端时再输 `SESSION_PIN`。

```bash
./bin/stop.sh           # 停止服务（保留 tmux 会话）
./bin/safe-restart.sh   # 滚动重启（不杀会话）
```

`start.sh` 会安装 **LaunchDaemon**（`/Library/LaunchDaemons`）：开机/通电后**无需手动登录**也会启动（以本机用户运行）。若开了 FileVault，仍需完成一次磁盘解锁。

## 结构

```
web-terminal/
├── .env.example         # 配置模板（可提交）
├── .env                 # 本地密钥（勿提交，gitignore）
├── bin/
│   ├── start.sh / stop.sh / status.sh / safe-restart.sh
│   ├── run-ttyd.sh / run-manage.sh / run-cloudflared.sh
│   ├── attach-session.sh / session-ctl.sh / manage-server.py
│   ├── ensure-ttyd-index.sh / patch-ttyd-index.py / healthcheck.sh
│   └── common.sh
├── config/
│   ├── cloudflared.yml
│   ├── tmux.web.conf
│   └── uk.lucadesign.web-terminal.*.plist
├── web/                 # ttyd 自定义页（启动时生成）+ wt-*.js / wt-icon-*（favicon 源）
├── logs/
└── run/
```

## Cloudflare

| 项 | 值 |
|----|-----|
| Tunnel | `web-terminal` |
| ID | `7969fb73-7802-4be4-8205-899320051f34` |
| 域名 | `term.lucadesign.uk` |
| `/` | 管理页 → `127.0.0.1:7690` |
| `/term` | ttyd → `127.0.0.1:7681` |
| 凭据 | `~/.cloudflared/7969fb73-7802-4be4-8205-899320051f34.json` |

## 安全

1. **`.env` 永不上传**：已在 `.gitignore`；别人克隆后 `cp .env.example .env` 自己填密码/PIN。
2. 在 Cloudflare Zero Trust → Access 给域名加登录策略（推荐）。
3. 仅监听 `127.0.0.1`，不暴露公网端口。
4. 不要提交 tunnel 凭据（`~/.cloudflared/*.json`）。

## 说明

- 默认会话名 `main`；URL 形如 `/term/?arg=work` 或新建 `/term/?arg=work&arg=create`
- 图片粘贴链路：浏览器取图 → 管理 API 落盘并写入 **本机（Mac）剪贴板** → 向 PTY 发 `^V`，让 Claude Code 像本地一样读图；非 mac 客户端的图只存在于对端剪贴板，必须由服务端写入 Mac 剪贴板
- Claude Code / TUI 在 tmux 下一般可用（已关 status、aggressive-resize、低 escape-time）；若遇异常可在管理页新建干净会话
- 停止会话会 `tmux kill-session`，其中进程结束；仅浏览器断开不会停止会话
