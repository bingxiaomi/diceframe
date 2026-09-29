# 自部署指南（含自定义规则二开）

> 面向：把这个 fork 部署到任意机器（Windows / Linux / NAS / 云主机），并与朋友联机跑团。
> 文档里的 `<仓库>` 指本仓库根目录，命令按你实际路径替换。

---

## 0. 这个 fork 改了什么

| 改动 | 位置 | 说明 |
|---|---|---|
| 新增规则运行时 `custom:declarative` | `src/rulesets/custom/` | 自己的规则大脑：资源、检定、成功度分级全部由规则 JSON 声明，**不受内置 6 个硬编码枚举限制** |
| 注册运行时 | `src/rulesets/builtin.py` | 一行注册，让引擎能发现上面这个运行时 |
| 示例规则 | `templates/rules/custom_freeform.json` | WebUI 规则列表里多出「自定义规则（自由编写）」 |
| 构建辅助脚本 | `scripts/dev/build_frontend.py`、`scripts/dev/watch_build.py` | 可观察的前端构建：实时日志 + 心跳状态 + 国内镜像 + 跳过 Playwright 下载 |
| 本文件 | `docs/DEPLOY_CN.md` | 部署 / 联机 / 排错 |

上游原有功能（多人桌、世界书、记忆、QQ/NapCat、D&D 2024）**未改动**。
详细进度与后续计划见 `docs/PROGRESS_CN.md`。

---

## 1. 前置条件

| 依赖 | 要求 | 说明 |
|---|---|---|
| Python | **≥ 3.11** | ⚠️ 上游 README 写"3.10 或更高"**不准**：代码用了 `typing.NotRequired`（3.11 才加入）。3.10 会在导入时报错 |
| Node.js | `^20.19.0 \|\| >=22.12.0` | 只有构建前端需要；跑 Docker 镜像不需要 |
| npm | 随 Node | **Windows 上用 `npm.cmd`**，不要用 `npm`（PowerShell 会拒绝 `npm.ps1`） |
| Docker | 可选 | 走容器部署时才需要 |

---

## 2. 部署方式

### 0）获取代码

```bash
git clone https://github.com/bingxiaomi/diceframe.git
cd diceframe
```

> ⚠️ **国内网络注意**：直连 `github.com:443` 会超时或被重置（实测报
> `Failed to connect to github.com port 443 after 21027 ms`，以及大流量上传时
> `Recv failure: Connection was reset`）。如果本机有代理（例如 Clash 的 `127.0.0.1:7897`），
> 给 git 配上即可（**只对 github.com 生效，不影响国内镜像源**）：
>
> ```bash
> git config --global http.https://github.com.proxy http://127.0.0.1:7897
> ```
>
> 实测走代理后推送速度从 296 KiB/s（且失败）提升到 **5.24 MiB/s**。
> 若不想用代理，也可以把仓库镜像到 Gitee（`gitee.com` 可直连）再从那里 clone。

> 💡 仓库已配置 `upstream` 远端指向国内可直连的上游镜像
> `https://github.laiyagushi.com/diceframe/diceframe.git`，需要参考上游代码时可直接 `git fetch upstream`。

### 方式 A：源码部署（推荐，能改代码）

```bash
cd <仓库>

# 1) 虚拟环境（Python 必须 >= 3.11）
python -V                          # 先确认版本
python -m venv .venv               # Windows 上建议命名为 .venv314，见方式 B 说明
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate

# 2) 后端依赖（只有 5 个包）
python -m pip install -r requirements.txt

# 3) 前端构建（首次必须做；改过 frontend-v2 要重做）
python scripts/dev/build_frontend.py
#    等价手动版：
#    cd frontend-v2
#    npm ci --registry=https://registry.npmmirror.com --no-audit --no-fund
#    npm run build
#    cd ..

# 4) 启动
python web_server.py
```

启动成功后终端会打印监听地址，**默认 `http://localhost:18000`**（可用 `TRPG_WEB_PORT` 覆盖）：

```
DiceFrame WebUI: http://127.0.0.1:18000  (host=0.0.0.0)
Initial access password: xxxxxxxx        ← 也写入 data/access_token.txt
```

> 💡 **为什么要用 `scripts/dev/build_frontend.py` 而不是直接 `npm ci`**：
> 到 `registry.npmjs.org` 实测单个包可耗时 **158 秒**（`naive-ui`），整轮 install 卡 4 分钟以上仍无输出，
> 极易误判为死锁；换 `registry.npmmirror.com` 后**整轮 install + build 只需 56 秒**。
> 该脚本默认走镜像，并跳过构建不需要的 Playwright 浏览器下载。

### 方式 B：Windows 一键 `web_ui.bat`

仓库根目录的 `web_ui.bat` 会自动检查依赖与前端产物，缺什么补什么。它的解释器查找顺序是：

```
.venv314\Scripts\python.exe  →  .venv\Scripts\python.exe  →  系统 python
```

> ⚠️ **如果系统的 `python` 低于 3.11**，最后一档会失败（`cannot import name 'NotRequired'`）。
> 这种情况先在仓库内建好符合版本要求的虚拟环境，命名成 `.venv314` 或 `.venv`，再双击 `web_ui.bat`。

### 方式 C：Docker

```bash
docker compose up -d
# 打开 http://localhost:9876
```

宿主机端口由 `.env` 的 `DICEFRAME_HTTP_PORT` 控制（默认 9876），容器内固定 9876；运行数据挂载到 `./data`。

**Docker 有两种语义，别搞混：**

| 命令 | 镜像来源 | 是否含本 fork 的改动 |
|---|---|---|
| `docker compose up -d` | **从本地源码构建**（走 Dockerfile 最后的 `runtime` 阶段，自包含） | ✅ 含 |
| `docker compose pull` + `docker compose up -d` | 拉官方发布镜像 `ghcr.io/diceframe/diceframe:latest` | ❌ 不含 |

> ⚠️ 本地构建**不要指定 `--target managed-artifact`**（那是 CI/发布专用阶段，需要预先存在的
> `dist/docker-update.zip`，会报 `COPY ... not found`）。默认 target 就是给本地用的 `runtime`。

### 方式 D：Windows 便携版 / 官方镜像

见上游 README（下载 `DiceFrame-vX.Y.Z-windows-portable.zip` 或 `docker pull` 官方镜像）。
**这两条路都不含本 fork 的改动**，只适合体验原版。

---

## 3. 首次启动要做的三件事

### 3.1 登录口令（Owner 凭据）

| 项 | 说明 |
|---|---|
| 作用 | **房主/管理员凭据**，不是玩游戏的凭据。拿到它 = 后台全部权限（改模型配置、看日志、装插件、管理存档、预览任意玩家视角） |
| 存储 | `data/secrets.json`，**PBKDF2-SHA256（21 万次迭代）哈希**，服务端不存明文 |
| 平级凭据 | **设备令牌**（扫码配对，落盘只存 SHA-256，可单独吊销） |
| 首次启动 | 自动生成随机口令 → 打印到终端 + 写入 `data/access_token.txt` |
| 忘记后 | 新建 `data/reset_access_password.txt` 写入新口令 → 重启（自动哈希化并删除该文件） |

**改成易记口令的两种方式：**

```bash
# 方式一：环境变量（最高优先级，不落盘到 secrets.json）
# 在 <仓库>/.env 里写（.env 已被 .gitignore 忽略）：
TRPG_ACCESS_TOKEN=你的长口令

# 方式二：一次性重置文件
echo 你的新口令 > data/reset_access_password.txt   # Windows: 用编辑器新建该文件并写入
# 重启后生效，文件会被自动删除
```

> ⚠️ **口令不能"留空禁用"**：`src/webui/host_credentials.py` 检测到凭证无效时会**重新生成**。
> 这是有意的安全默认——只要 `access_password_configured = True`，所有 `/api/*` 都要求 owner 鉴权，
> 否则同机其它进程、浏览器扩展、局域网设备都能直接读你的存档与 API Key。
> 所以正解是**换成一个你记得住的强口令**，而不是去掉它。

### 3.2 配置模型（两步走）

> ⚠️ 旧的环境变量 `TRPG_LLM_*` / `TRPG_EMBEDDING_*` / `TRPG_TTS_*` / `TRPG_ASR_*` / `TRPG_IMAGEGEN_*`
> **已废弃且会被拒绝（HTTP 400）**。务必在 WebUI 里配置，不要照旧教程写 `.env`。

1. **加服务商** → 「管理 → 设置 → 模型接口 → AI 服务商」：填名称 / API 格式（`openai` 或 `anthropic`）/ Base URL / API Key / 可用模型列表
2. **分配用途** → 「管理 → 设置 → 模型配置」：指派主模型（**必需**）、备用模型、向量记忆、TTS、ASR、生图（其余可留空）

#### Base URL 必须写到 `/v1`（最易错）

代码逻辑是 `url = base_url.rstrip("/")` 然后补 `/chat/completions`：

| 服务商 | API 格式 | Base URL | 模型名示例 |
|---|---|---|---|
| OpenAI | `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `openai` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 硅基流动 | `openai` | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` |
| 本地 Ollama | `openai` | `http://localhost:11434/v1` | `qwen2.5:14b` |
| 本地 LM Studio | `openai` | `http://localhost:1234/v1` | 你加载的模型名 |
| Anthropic Claude | `anthropic` | `https://api.anthropic.com` | `claude-sonnet-4-5` |

- **本地服务商允许 API Key 留空**（Ollama / LM Studio 不需要 key）。
- 走代理填 `TRPG_PROXY_URL`（例：`http://127.0.0.1:7890`）。
- 客户端已内置：`429/502/503/504/408` 退避重试、备用模型降级、输出被 `max_tokens` 截断时自动放大预算重试。

### 3.3 验证二开是否生效

在仓库根目录执行（**必须先 `import web_server`**，原因见排错表 T5）：

```bash
python -c "import web_server; from src.rulesets.builtin import build_default_ruleset_registry as b; print(b().runtime_ids())"
```

预期输出：

```
('core:dnd2024', 'core:legacy', 'custom:declarative')
```

看到 `custom:declarative` 即说明自定义规则运行时已被引擎注册。

---

## 4. 跑第一局

1. 打开 WebUI → 「**总览 → 创建新冒险**」
2. **规则选「自定义规则（自由编写）」**（即 `custom_freeform`）
   - 关键：只有这条规则声明了 `"runtime": {"id": "custom:declarative"}`，引擎才会用你的运行时
3. 世界选模板或让 AI 生成 → 创建角色
4. 进「游玩」，用自然语言描述行动，例如：
   > 我盯着墙上那幅画，想确认自己是否还能稳住心神
5. 需要检定时系统自动判断并**只掷一次骰**，然后 GM 继续叙事

**怎么确认规则逻辑在起作用**：示例规则声明了资源「意志值」（初始 50 / 上限 99）与「意志检定」。
当前处于 **Stage A（叙事模式）**，它体现为：

- GM 的上下文多出一段 `ruleset_authority`（各席位资源、声明的检定与成功度、策略声明
  `Narrate resolved results only; never invent or mutate custom mechanics.`）；
- `authoritative_fields` 里列出的键（`resources` / `attributes` / `skills`）会被从 LLM 的状态提案中**直接剔除**
  —— 模型没有能力改写它们。

即：**模型负责叙事，数值归你的运行时。**

---

## 5. 公网联机（内网穿透）安全清单

### 5.1 三层凭据的关系

```
Owner 口令  ──→  后台管理 + GM 席位        （玩家拿不到，也不需要）
邀请链接    ──→  玩家身份（?user=<uid>&share=1，服务端 player_access 白名单）
                    ↓
房间密码    ──→  verify-room-password → room_token → 玩家 API（endpoint 白名单）
```

- 玩家用**邀请链接 + 房间密码**进入，只拿到玩家视图与受限 endpoint。
- ⚠️ **GM 席位必须 owner 登录**（中间件里有 `GM_SEAT_REQUIRES_OWNER`）：分享链接**不能**冒充本局 GM 席位。
  所以：你自己当 GM 没影响；**若让朋友当 GM**，要么给他 owner 口令（= 全部权限），要么他不走分享链接、直接访问站点登录。

### 5.2 必做清单

| # | 事项 | 原因 |
|---|---|---|
| 1 | **设强口令**（`TRPG_ACCESS_TOKEN`，≥ 24 字符随机） | 端口一旦公网可达，owner 口令是唯一防线。登录有 `abuse_guard` 限流，但不足以抗暴力破解 |
| 2 | **走 HTTPS** | HTTP 下口令与 `room_token` 明文传输。优先用自带 HTTPS 的穿透（Cloudflare Tunnel / ngrok），或启用 `TRPG_TLS_MODE`（`self_signed` / `lets_encrypt`） |
| 3 | **删掉 `data/access_token.txt`** | 那是明文口令的"提醒文件"；代码在校验通过后**会保留**它（只在口令不一致时才删）。删除不影响登录 |
| 4 | **每局设房间密码** | 玩家入口的第二道门 |
| 5 | **`TRPG_WEB_CORS_ORIGINS` 保持为空** | 同源部署根本不需要；只有独立部署前端（如 Cloudflare Pages）才需填写具体域名。**不要写 `*`** |
| 6 | 人齐后**关闭玩家入口** | 服务端 `player_access_is_closed` → 后到者直接 403 |
| 7 | **`data/` 不可整包外发** | 内含 `secrets.json`（API Key / Bot Token）与 `saves/`（私人剧情） |
| 8 | 先确定公开地址再发链接 | 邀请链接里的域名就是穿透域名，换域名后旧链接失效 |

### 5.3 推荐配置片段

```bash
# <仓库>/.env  —— 应用启动时会自动加载（load_project_env），已存在环境变量优先
TRPG_ACCESS_TOKEN=<你的强口令>
TZ=Asia/Shanghai
# 同源部署不需要 CORS
# TRPG_WEB_CORS_ORIGINS=
```

---

## 6. 可观察的构建与排错

### 6.1 两个辅助脚本

```bash
# 可观察的构建驱动：逐行实时日志 + 后台心跳刷状态 + 跳过 Playwright 下载 + 默认国内镜像
python scripts/dev/build_frontend.py
python scripts/dev/build_frontend.py --registry https://registry.npmjs.org   # 想用官方源
python scripts/dev/build_frontend.py --ci-only / --build-only                # 分步执行

# 观察器：显示 步骤 / 真实已用时长 / 静默时长 / node 进程数，并直接给出判断结论
python scripts/dev/watch_build.py --seconds 200
```

产物（均在 `logs/`，已被 `.gitignore` 忽略）：

| 文件 | 用途 |
|---|---|
| `logs/build_frontend.log` | 完整逐行日志（人看） |
| `logs/build_frontend_status.json` | 机器可读状态，**每 5 秒心跳刷新** |

### 6.2 判断"构建卡住"的三个信号

`npm ci` 在非 TTY 下会缓冲输出，**"没有新输出"不等于卡死**（实测下载 `naive-ui` 时静默 158 秒）。看这三条：

| 信号 | 正常 | 异常 |
|---|---|---|
| `node.exe` 进程是否存活（内存缓慢增长） | 有，占百 MB | 没有 |
| 日志文件 mtime / 大小是否在变 | 在变（即使间隔久） | 长时间完全不变 |
| 静默时长 | < 4 分钟（大包下载） | > 4 分钟 |

`watch_build.py` 会把这三条综合成一句结论（活跃输出中 / 正常静默 / 怀疑卡住 / 已死）。

---

## 7. 排错表

| 编号 | 现象 | 原因与处理 |
|---|---|---|
| T1 | `ImportError: cannot import name 'NotRequired' from 'typing'` | Python < 3.11。换 3.11+（推荐 3.12/3.13/3.14） |
| T2 | `npm` 报「禁止运行脚本」 | PowerShell 执行策略。改用 `npm.cmd`；或用 `cmd.exe`；或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| T3 | 首页白屏 / 404 | 前端未构建。跑 `python scripts/dev/build_frontend.py` |
| T4 | 找不到 `npm` | 未装 Node.js 或不在 PATH。需要 `^20.19.0 \|\| >=22.12.0` |
| T5 | 单独跑某个 pytest 文件报 `cannot import name 'fresh' from partially initialized module ...lorebook_runtime` | **仓库既有的导入顺序脆弱性，与二开无关**。进程若最先导入 `src.rulesets` 或 `src.engine.modules` 就会撞上导入环。解法：先 `import web_server`；跑测试用 `python -m pytest -p web_server <路径>` |
| T6 | 模型报 401 / 404 | 九成是 Base URL 写错。检查是否**写到 `/v1`**、有没有多写 `/chat/completions`、API 格式选对没 |
| T7 | 配了 `.env` 的 `TRPG_LLM_*` 不生效甚至 400 | 已废弃。必须在「管理 → 设置 → 模型接口」里加服务商 |
| T8 | 端口被占用 | 用 `TRPG_WEB_PORT=18001` 换端口 |
| T9 | 规则列表看不到「自定义规则（自由编写）」 | 确认 `templates/rules/custom_freeform.json` 存在且服务已重启 |
| T10 | 选了自定义规则但行为像普通规则 | 正常。Stage A 只影响 GM 上下文与状态守卫，回合流水线仍走原有叙事路径 |
| T11 | Docker 里没有「自定义规则（自由编写）」 | 用了官方发布镜像。改为不带 `pull` 的 `docker compose up -d`（本地源码构建） |
| T12 | `docker compose build` 报 `COPY dist/docker-update.zip: not found` | 指定了 `managed-artifact` target（CI 专用）。本地构建不要指定 target |
| T13 | 双击 `web_ui.bat` 报 `NotRequired` | 它回退到了系统 `python`（< 3.11）。先在仓库内建 `.venv314` / `.venv` |
| T14 | 首页 404 但日志显示已启动 | 前端未构建，见 T3 |
| T15 | `npm ci` 第一步长时间无进展 | 官方源太慢（实测单包 158s）。改用 `scripts/dev/build_frontend.py`（默认镜像） |
| T16 | 想知道是在下载还是真卡死 | 跑 `scripts/dev/watch_build.py`，看**静默时长 + node 进程存活** |
| T17 | 想停掉后台服务 | Windows：`taskkill /PID <PID> /T /F`；Linux：`kill <PID>`。PID 见启动日志 |

---

## 8. 改代码后的日常循环

| 你改了什么 | 要做什么 |
|---|---|
| Python（`src/**`、规则 JSON、模板） | 重启 `web_server.py` |
| 前端（`frontend-v2/**`） | `python scripts/dev/build_frontend.py --build-only`（或双击 `web_ui.bat`，它会检测源码更新时间自动重建） |
| 只改规则 JSON | 重启后端；或在 WebUI 规则页编辑保存（用户规则存在 `data/templates/rules/`） |

---

## 9. 路径速查

| 用途 | 路径 |
|---|---|
| 运行数据根目录 | `data/`（可用 `TRPG_DATA_DIR` 覆盖） |
| 登录口令（首次生成的明文提醒文件） | `data/access_token.txt` |
| 口令重置入口 | `data/reset_access_password.txt` |
| 普通配置 / 敏感配置 | `data/config.json` / `data/secrets.json` |
| 用户自定义规则 | `data/templates/rules/`（升级不覆盖） |
| 内置规则（含本示例） | `templates/rules/` |
| 游戏存档 | `data/saves/` |
| 前端源码 / 构建产物 | `frontend-v2/` / `static-v2/` |
| 构建日志与状态 | `logs/build_frontend.log` / `logs/build_frontend_status.json` |
| 自定义规则运行时源码 | `src/rulesets/custom/` |
| 构建辅助脚本 | `scripts/dev/build_frontend.py`、`scripts/dev/watch_build.py` |

---

## 10. 可选：接入 QQ（NapCat）

与本 fork 的改动无关，需要时再看：

1. 部署 [NapCat](https://github.com/NapNeko/NapCat)，在「网络设置 → WebSocket 服务器」启用服务，记下端口与 access_token；
2. DiceFrame「管理 → 插件」打开 `QQ / NapCat`，填 WebSocket 地址 / 端口 / token 并启用；
3. 游戏页复制 Bot 绑定命令，发到目标群；
4. 玩家发 `@bot 加入 角色名` 后即可用自然语言行动。

也可只填 `.env` 的 `NAPCAT_HOST` / `NAPCAT_PORT` / `NAPCAT_TOKEN`（Docker 下同机 NapCat 可用 `host.docker.internal`）。

> **Bot 卡片渲染需要中文字体**：Docker 镜像已内置 `fonts-noto-cjk`；Linux 直接跑 `python web_server.py`
> 需自行安装 CJK 字体（如 Debian/Ubuntu 的 `fonts-noto-cjk`），否则卡片会降级为纯文本。
