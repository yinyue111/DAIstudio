# 造梦 Studio / DAIstudio

造梦 Studio 是一个面向内容团队的 AI 图片与视频素材生成平台。用户可以粘贴小红书、抖音或普通网页链接，也可以上传图片/视频作为参考素材；平台会提取参考素材、反推结构化提示词，并通过已配置的大模型网关生成图片或视频。系统内置积分、支付充值、管理员配置、素材审核和生产部署所需的安全防护。

## 核心功能

- 参考素材获取：支持小红书、抖音、普通网页链接解析，支持图片/视频上传。
- 提示词反推：基于参考图、参考视频关键帧生成可编辑的结构化提示词。
- 图片生成：支持文生图、参考图生成/编辑、批量生成、比例/分辨率/张数配置。
- 视频生成：支持文生视频、图生视频、视频参考/编辑，预览阶段和完整视频阶段分离。
- 提示词库：内置提示词参考库，可在创作页面快速选用和改写。
- 预览与解锁：生成低清/水印预览，用户确认后使用积分解锁高清素材。
- 积分体系：提交任务冻结积分，成功结算，失败退款，完整流水可追踪。
- 支付充值：支持支付宝、微信支付配置，管理员可自定义充值套餐。
- 管理后台：用户白名单、管理员、额度发放、模型配置、支付/短信开关、用量报表、审计日志、素材举报处理。
- 生产加固：HttpOnly Cookie 登录、CSRF Origin/Referer 校验、SSRF 防护、上传/下载大小限制、任务幂等、发布包污染检查。

## 技术栈

| 模块 | 技术 |
| --- | --- |
| 前端 | Next.js 16 App Router、React 18、Tailwind CSS |
| 后端 | FastAPI、SQLAlchemy、Alembic |
| 异步任务 | Celery、Redis |
| 数据库 | PostgreSQL |
| 链接抓取 | Playwright、httpx、BeautifulSoup |
| 图片/视频分析 | ffmpeg、Tesseract、可选自托管检测/分割、视频语义和 Faster-Whisper ASR |
| 存储 | 本地文件系统或 S3/MinIO，通过统一存储服务和签名访问 |
| 部署 | Docker Compose、Nginx 反向代理 |

## 业务流程

1. 用户使用手机号和密码登录；注册需要手机号在白名单内。
2. 用户粘贴参考链接或上传参考图片/视频。
3. 平台抓取或保存素材，并生成可预览的参考资源。
4. 用户选择参考素材，可执行提示词反推，也可以直接输入创作要求。
5. 用户选择图片/视频模型、比例、质量、时长、数量等参数后提交任务。
6. 系统冻结预计积分并异步调用模型网关。
7. 生成完成后展示预览结果；失败时自动退回冻结积分。
8. 用户选中满意素材并解锁高清结果。
9. 管理员可在后台查看用量、审计、订单、任务和素材举报。

## 目录结构

```text
.
├── backend/                 # FastAPI 后端、Celery 任务、Alembic 迁移
│   ├── app/
│   │   ├── routers/         # API 路由
│   │   ├── services/        # 生成、抓取、支付、短信、SSRF、积分等业务服务
│   │   └── models.py        # SQLAlchemy 模型
│   ├── alembic/             # 数据库迁移
│   ├── tests/               # 后端测试
│   ├── models.yaml          # 首次初始化的模型和默认配置种子
│   └── .env.example         # 后端环境变量样例
├── frontend/                # Next.js 前端
│   ├── app/                 # 页面与组件
│   ├── components/          # 公共组件
│   └── lib/                 # API 客户端
├── evidence_provider/       # 可选图片检测/分割服务
├── video_semantic_provider/ # 可选视频运动区域和视觉转场服务
├── audio_provider/          # 可选 Faster-Whisper ASR 服务
├── deploy/nginx/            # dream.aiwuq.cn Nginx 模板
├── docs/security/           # 发布安全和密钥轮换 Runbook
├── scripts/                 # 初始化、启动、发布检查脚本
├── docker-compose.yml       # 生产/准生产一键编排
└── Makefile                 # 开发、测试、部署常用命令
```

## 环境要求

- Python 3.10+
- Node.js 20.9+
- PostgreSQL 13+
- Redis 6+
- ffmpeg：用于视频反推关键帧分析；Docker 镜像已内置
- Playwright Chromium：用于动态网页抓取；安装失败时会回退到 httpx/BeautifulSoup

## 本地开发

### 1. 准备数据库

```bash
createdb ai_studio
```

也可以使用已有 PostgreSQL，只需要在 `backend/.env` 中配置 `DATABASE_URL`。

### 2. 安装依赖

```bash
make install
make install-frontend
```

### 3. 配置环境变量

```bash
cp backend/.env.example backend/.env
```

至少检查以下配置：

```env
DEBUG=true
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/ai_studio
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=replace-with-a-long-random-secret
PUBLIC_BASE_URL=http://localhost:8000
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

如果要使用真实模型网关，继续配置：

```env
GATEWAY_BASE_URL=https://your-openai-compatible-gateway
GATEWAY_API_KEY=<provider-api-key>
ANTHROPIC_BASE_URL=https://your-anthropic-compatible-gateway
ANTHROPIC_AUTH_TOKEN=<prompt-optimizer-api-key>
VIDEO_GATEWAY_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
VIDEO_GATEWAY_API_KEY=<ark-api-key>
VIDEO_GATEWAY_FORMAT=ark
MODEL_CONFIG_SECRET=replace-with-a-strong-random-secret
```

### 4. 执行迁移并初始化管理员

```bash
make migrate
ADMIN_PASSWORD='<strong-admin-password>' \
  backend/.venv/bin/python -m scripts.init_db --admin-phone 13800000000 --credits 1000
```

生产环境请使用更强的管理员密码。未设置 `ADMIN_PASSWORD` 时，初始化脚本会生成一次性随机密码并打印。

### 5. 启动服务

四个终端分别执行：

```bash
make run-api
make run-worker
make run-beat
make run-frontend
```

默认地址：

- 前端：http://localhost:3000
- 后端：http://localhost:8000
- 存活检查：http://localhost:8000/api/live
- 就绪检查：http://localhost:8000/api/ready

## Docker 部署

Docker Compose 会启动 PostgreSQL、Redis、迁移任务、API、Celery Worker、Celery Beat 和前端。

### 1. 准备生产环境变量

```bash
cp backend/.env.example backend/.env.production
```

生产必须配置强随机密钥：

```env
DEBUG=false
JWT_SECRET=<strong-random-secret>
METRICS_TOKEN=<strong-random-token>
PAYMENT_CONFIG_SECRET=<strong-random-secret-at-least-32-chars>
MODEL_CONFIG_SECRET=<strong-random-secret-at-least-32-chars>
PUBLIC_BASE_URL=https://dream.aiwuq.cn
CORS_ORIGINS=https://dream.aiwuq.cn
PAYMENT_FRONTEND_BASE_URL=https://dream.aiwuq.cn
TRUSTED_PROXY_IPS=127.0.0.1
ANTHROPIC_BASE_URL=https://your-anthropic-compatible-gateway
ANTHROPIC_AUTH_TOKEN=<prompt-optimizer-api-key>
```

`TRUSTED_PROXY_IPS` 只能填写真正转发到 API 的反代来源 IP。按本文档的 nginx 配置，API 只监听宿主机 `127.0.0.1:8000`，所以生产默认只信任 `127.0.0.1`。不要直接信任整个 Docker 私网网段，否则同网络内其他容器可能伪造 `X-Forwarded-For`。

然后在 shell 或根目录 `.env` 中设置 Compose 需要插值的数据库和 Redis 密码：

```bash
export POSTGRES_PASSWORD='<strong-postgres-password>'
export REDIS_PASSWORD='<strong-redis-password>'
```

`DEBUG`、`PUBLIC_BASE_URL`、`CORS_ORIGINS`、`TRUSTED_PROXY_IPS`、短信、支付、模型网关和在线升级等后端应用配置都只写入 `backend/.env.production`。Compose 默认通过 `BACKEND_ENV_FILE=./backend/.env.production` 读取它们；宿主 shell/根目录 `.env` 只用于 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、端口和前端构建参数，避免覆盖后端应用配置。若你使用其他文件名，启动时显式设置 `BACKEND_ENV_FILE=/path/to/backend.env`。

### 2. 启动全栈

```bash
docker compose up -d --build
```

首次启动后创建管理员：

```bash
ADMIN_PASSWORD='<strong-admin-password>' docker compose run --rm -e ADMIN_PASSWORD api \
  python -m scripts.init_db --admin-phone 13800000000 --credits 1000
```

### 3. 可选证据分析服务

需要证据化图片/视频反推时，先在 `backend/.env.production` 配置三个私网
provider 的 URL 和同名 Bearer key，再启用可选 `evidence` profile：

```env
IMAGE_EVIDENCE_DETECTOR_URL=http://evidence_provider:8090/v1/region/analyze
IMAGE_EVIDENCE_DETECTOR_HEALTH_URL=http://evidence_provider:8090/health/detector
IMAGE_EVIDENCE_DETECTOR_API_KEY=<image-provider-key>
IMAGE_EVIDENCE_SEGMENTER_URL=http://evidence_provider:8090/v1/region/analyze
IMAGE_EVIDENCE_SEGMENTER_HEALTH_URL=http://evidence_provider:8090/health/segmenter
IMAGE_EVIDENCE_SEGMENTER_API_KEY=<image-provider-key>
VIDEO_EVIDENCE_SEMANTIC_URL=http://video_semantic_provider:8091/v1/video-semantic/analyze
VIDEO_EVIDENCE_SEMANTIC_HEALTH_URL=http://video_semantic_provider:8091/health
VIDEO_EVIDENCE_SEMANTIC_API_KEY=<video-provider-key>
AUDIO_GATEWAY_ENABLED=true
AUDIO_GATEWAY_BASE_URL=http://audio_provider:8092
AUDIO_GATEWAY_HEALTH_URL=http://audio_provider:8092/health
AUDIO_GATEWAY_API_KEY=<audio-provider-key>
AUDIO_TRANSCRIPTION_MODEL=small
TRUSTED_ANALYZER_HOSTS=evidence_provider,video_semantic_provider,audio_provider
```

```bash
EVIDENCE_PROVIDER_API_KEY='<image-provider-key>' \
VIDEO_SEMANTIC_PROVIDER_API_KEY='<video-provider-key>' \
ASR_PROVIDER_API_KEY='<audio-provider-key>' \
docker compose --profile evidence up -d --build
```

三个 provider 只绑定宿主机 `127.0.0.1`，通过 `backend_internal` 接受 API/
worker 调用，并使用独立 `analyzer_edge` 提供本机运维探针，不与前端网络共享。
当前自托管视频语义服务只提供运动区域和视觉突变，说话人、姿态和动作能力
会明确返回 `unsupported`，不会由普通文案伪装成分析证据。

### 4. Worker 队列

Docker Compose 默认按工作负载拆分 Worker，避免长任务把支付、清理和视频轮询饿死：

```text
worker        default,payment,video_poll,cleanup
worker_image  image
worker_video  video_submit
worker_video_download  video_download
worker_parse  parse
worker_reverse  reverse
worker_workflow  workflow
```

`make run-worker` 本地默认仍可消费全部队列，方便开发；生产裸机部署可以用 `WORKER_ROLE` 直接拆：

```bash
WORKER_ROLE=critical ./scripts/run_worker.sh
WORKER_ROLE=image WORKER_CONCURRENCY=6 ./scripts/run_worker.sh
WORKER_ROLE=video-submit ./scripts/run_worker.sh
WORKER_ROLE=video-download ./scripts/run_worker.sh
WORKER_ROLE=parse ./scripts/run_worker.sh
WORKER_ROLE=reverse ./scripts/run_worker.sh
WORKER_ROLE=workflow ./scripts/run_worker.sh
```

`WORKER_ROLE=video` 仍作为本地兼容别名存在，但会同时消费 `video_submit,video_download`，生产不建议使用。特殊场景仍可直接覆盖 `WORKER_QUEUES`。需要兼容 macOS fork 调试时，可显式设置 `WORKER_POOL=solo WORKER_CONCURRENCY=1`，但这会让对应 worker 串行执行。`beat` 服务只能保留一个实例，负责支付对账、卡死任务回收、视频轮询恢复和清理任务。

## 域名与反向代理

项目按 `dream.aiwuq.cn` 生产域名准备了 Nginx 模板：

```text
deploy/nginx/dream.aiwuq.cn.conf
```

反向代理规则：

- `/` 和 `/_next/static/` 转发到前端 `127.0.0.1:3000`
- `/api/`、`/media/`、`/ws/` 转发到后端 `127.0.0.1:8000`
- 默认请求体限制 `2m`；`/api/uploads/image` 为 `32m`；`/api/uploads/video` 为 `528m`
- `proxy_read_timeout 900s`
- 强制 HTTPS，并开启 HSTS、nosniff、Referrer-Policy、X-Frame-Options

部署时需要把证书路径替换为真实证书路径，或使用 certbot 托管证书。

## 模型配置

系统支持两种模型配置来源：

1. 环境变量兜底：`GATEWAY_BASE_URL`、`GATEWAY_API_KEY`、`ANTHROPIC_*`、`VIDEO_GATEWAY_*`
2. 管理后台动态配置：模型用途、提供商、Base URL、API Key、模型 ID、积分价格、额外参数

管理后台的模型用途：

| 用途 | 说明 |
| --- | --- |
| `vision` | 图片/视频参考素材反推提示词 |
| `image` | 图片生成与图片编辑 |
| `video` | 视频生成、预览和完整渲染 |
| `prompt` | 直接输入提示词优化，默认走独立 Anthropic 兼容网关 |

后台保存的 API Key 会使用 `MODEL_CONFIG_SECRET` 加密；如果未单独配置，则回退使用 `PAYMENT_CONFIG_SECRET`。生产环境不要把真实 API Key 提交到仓库。

后台支持测试提供商并读取模型列表，管理员可以先填入 Base URL 和 API Key，再探测支持的模型 ID 并选择保存。

### 常见模型网关

- OpenAI 兼容网关：用于 `vision` 和 `image`，路径通常是 `/v1/chat/completions`、`/v1/images/generations`、`/v1/images/edits`
- 火山 Ark / Doubao Seedance：用于 `video`，默认 Base URL 可使用 `https://ark.cn-beijing.volces.com/api/v3`

图片和视频生成时间较长，相关超时在 `backend/.env.example` 中已拆分：

```env
REVERSE_GATEWAY_TIMEOUT_SECONDS=240
REVERSE_GATEWAY_MAX_RETRIES=0
REVERSE_GATEWAY_MAX_TOKENS=4096
REVERSE_GATEWAY_REASONING_EFFORT=low
IMAGE_GATEWAY_TIMEOUT_SECONDS=600
IMAGE_DOWNLOAD_TIMEOUT_SECONDS=600
VIDEO_SUBMIT_TIMEOUT_SECONDS=300
VIDEO_POLL_MAX_SECONDS=600
VIDEO_DOWNLOAD_TIMEOUT_SECONDS=3600
```

## 支付配置

支付功能默认关闭，管理员可在后台开启并配置套餐。

### 支付宝

需要准备：

- 应用 ID
- 商户/卖家 ID
- 应用私钥
- 支付宝公钥
- 回调地址，例如 `https://dream.aiwuq.cn/api/payments/alipay/notify`

### 微信支付

需要准备：

- AppID
- 商户号
- 商户证书序列号
- 商户私钥
- API v3 Key
- 平台证书序列号
- 平台证书 PEM
- 回调地址，例如 `https://dream.aiwuq.cn/api/payments/wechat/notify`

生产环境必须保持：

```env
PAYMENT_MOCK_ENABLED=false
PAYMENT_CONFIG_SECRET=<strong-random-secret>
PAYMENT_FRONTEND_BASE_URL=https://dream.aiwuq.cn
```

支付回调失败时，Celery Beat 会按配置执行订单对账，降低漏通知导致不到账的风险。

## 在线版本升级

管理后台提供“版本升级”页，可以从服务端配置的 Git remote/branch 拉取新代码并执行固定生效命令。该功能默认关闭，默认升级源是私有 GitHub 仓库 `https://github.com/yinyue111/DAIstudio.git`。生产启用前需要确认 API 进程运行在真实 Git checkout 中，并为私有仓库配置 `ONLINE_UPDATE_GITHUB_TOKEN`。Docker 镜像默认不包含 `.git`，所以普通 Compose 镜像内 `/app` 不能直接在线升级；只有挂载真实仓库并配置固定生效命令后，才建议显式设置 `ONLINE_UPDATE_ENABLED=true`。

安全边界：

- 默认 `ONLINE_UPDATE_ENABLED=false`，需要真实 Git checkout 和固定生效命令后显式开启
- 只更新服务端配置的 `ONLINE_UPDATE_REMOTE` / `ONLINE_UPDATE_BRANCH`
- 私有库使用 GitHub fine-grained PAT，通过 `ONLINE_UPDATE_GITHUB_TOKEN` 注入，不要把 GitHub 密码或 token 写进 URL
- `ONLINE_UPDATE_REMOTE` 可以是 HTTPS/SSH Git URL 或 remote 名称；remote 名称解析后的真实 URL 也会校验，默认拒绝 `file://`、绝对路径、相对路径等本地仓库来源
- 只允许 `git merge --ff-only`；若生效命令失败且工作区在升级前是干净的，会 `git reset --hard` 回滚到升级前版本，避免半升级代码继续运行
- 工作区有未提交改动时默认拒绝升级
- 前端不能传入任意命令；生效命令只能由服务端环境变量固定配置

示例配置：

```env
ONLINE_UPDATE_ENABLED=true
ONLINE_UPDATE_REPO_DIR=/srv/ai-media-studio
ONLINE_UPDATE_REMOTE=https://github.com/yinyue111/DAIstudio.git
ONLINE_UPDATE_BRANCH=main
ONLINE_UPDATE_GITHUB_TOKEN=github_pat_xxx
ONLINE_UPDATE_APPLY_COMMAND=/usr/local/bin/ai-studio-apply-update
ONLINE_UPDATE_TIMEOUT_SECONDS=600
ONLINE_UPDATE_ALLOW_DIRTY=false
ONLINE_UPDATE_ALLOW_LOCAL_REMOTE=false
ONLINE_UPDATE_REQUIRE_SIGNED_COMMITS=true
```

私有 GitHub 仓库需要创建一个只读 token：

1. 打开 GitHub `Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens`
2. 选择仓库 `yinyue111/DAIstudio`
3. `Repository permissions` 里只给 `Contents: Read-only`
4. 生成 token 后写入后端环境变量 `ONLINE_UPDATE_GITHUB_TOKEN`
5. 重启后端，让后台“版本升级”页读取新配置

不要写成 `https://token@github.com/...`。后端会拒绝带用户名/密码的 remote，并且会把 token 通过临时 Git 环境配置传给 `ls-remote/fetch`，接口输出和命令日志会做脱敏。

Docker Compose 部署时，在线升级配置同样写在 `backend/.env.production`。Compose 不会用空默认值覆盖该文件里的 `ONLINE_UPDATE_*`，但镜像内 `/app` 默认不是 Git checkout。要在容器内直接点“从 GitHub 更新并生效”，需要把宿主机真实 checkout 挂载为 `ONLINE_UPDATE_REPO_DIR`；否则请把 `ONLINE_UPDATE_ENABLED=false`，避免后台显示可升级但实际目录没有 `.git`。生产环境启用在线升级时必须设置 `ONLINE_UPDATE_REQUIRE_SIGNED_COMMITS=true`，并确保发布分支提交能通过 `git verify-commit`。

生效命令不要在 API 进程里直接执行 `docker compose up/restart/down/rm`，尤其不能重启 `api` 服务。否则升级脚本会先停掉正在执行命令的 API 容器，命令中断后可能留下“新容器 Created、8000 端口无人监听”的半升级状态，Caddy 转发 `/api/*` 就会返回 502。后端会拒绝这种高风险命令和脚本内容。

后台“版本升级”页会显示部署模式、升级策略和下一步建议：

- `安全生效命令 / 外部执行器`：可以从页面触发升级，后端快进代码后执行固定生效命令。
- `需外部执行器`：已配置命令但命令会重启 API 或存在高风险，页面会禁用升级按钮，必须改成宿主机 systemd oneshot、外部运维队列或 supervisor。
- `需手动生效`：只允许检查远端版本，不能直接合并代码。需要先配置安全 `ONLINE_UPDATE_APPLY_COMMAND`，或由运维在宿主机手动拉取、迁移、构建和重启。

类似 sub2api 的“页面更新后重启生效”依赖的是单二进制原子替换，然后让 systemd/Docker restart policy 拉起新进程。本项目是 FastAPI + Next.js + Celery + 迁移的多服务 Compose 架构，不能只替换 API 进程或让 API 自己执行 Compose；必须由 API 外部的执行器完成整组服务生效。

推荐的 Docker 在线升级结构是“API 只拉代码和写状态，宿主机/外部进程完成重启”：

1. `ONLINE_UPDATE_REPO_DIR` 指向宿主机真实 checkout 的挂载目录。
2. `ONLINE_UPDATE_APPLY_COMMAND` 指向一个不会直接重启 API 的固定命令，例如写入一个队列文件、调用宿主机 systemd oneshot、或通知外部 supervisor。
3. 宿主机 oneshot/supervisor 再执行 `docker compose up -d --build migrate api worker worker_image worker_video worker_video_download worker_parse worker_reverse worker_workflow beat frontend`。
4. 重启完成后先用 `https://dream.aiwuq.cn/api/live` 确认进程存活，再用 `https://dream.aiwuq.cn/api/ready` 确认 DB/Redis 就绪，最后检查容器状态和后台版本页。

提示词反推异步化版本必须按以下顺序发布，不能把 API 或前端先于数据库迁移上线：

1. 先运行 `migrate`，确认 Alembic 已到 `0035_async_reverse_operations`，旧同步接口仍可读写已有记录。
2. 再发布 `api`，检查 `/api/live`、`/api/ready` 和 `/api/health` 均正常；此时旧前端仍可使用兼容接口。
3. 然后发布独立的 `worker_reverse` 和 `beat`，确认 `reverse` 队列可消费、每分钟清理任务已注册，且 worker 使用线程池并发 2。
4. 最后发布 `frontend`，完成图片反推、视频反推、封面确认、取消和刷新恢复的 mock 冒烟检查。

若任一阶段失败，停止后续阶段；不要用强杀 worker 的方式回滚。已冻结但未完成的反推任务由补投和超时清理流程继续处理或退款。

如果暂时没有宿主机执行器，建议把 `ONLINE_UPDATE_APPLY_COMMAND` 留空：后台只允许查看远端版本和升级预检；检测到新版本时会拒绝直接合并代码，之后由运维在宿主机手动执行拉取、迁移和 Compose 生效。

在服务器上用同一个用户验证：

```bash
export ONLINE_UPDATE_GITHUB_TOKEN=github_pat_xxx
git -c http.https://github.com/.extraheader="Authorization: Basic $(printf 'x-access-token:%s' "$ONLINE_UPDATE_GITHUB_TOKEN" | base64)" \
  ls-remote --heads https://github.com/yinyue111/DAIstudio.git main
```

如果你仍想使用 SSH，也可以把 `ONLINE_UPDATE_REMOTE` 改成 `git@github.com:yinyue111/DAIstudio.git`，并在服务器上配置 SSH 读取权限：

```bash
ssh-keygen -t ed25519 -C "ai-studio-online-update" -f ~/.ssh/ai_studio_update
cat ~/.ssh/ai_studio_update.pub
```

把公钥添加到 GitHub 仓库 `yinyue111/DAIstudio` 的 `Settings -> Deploy keys`，勾选只读即可。然后给运行后端的用户配置 `~/.ssh/config`：

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/ai_studio_update
  IdentitiesOnly yes
```

在服务器上用同一个用户验证：

```bash
which ssh
ssh -T git@github.com
git ls-remote --heads git@github.com:yinyue111/DAIstudio.git main
```

如果后台提示 `cannot run ssh: No such file or directory` 或 `缺少 ssh 客户端`，说明运行后端的环境里没有 SSH 客户端。Docker 部署需要重新构建包含 `openssh-client` 的后端镜像；裸机部署可安装 `openssh-client` 或把 `ONLINE_UPDATE_REMOTE` 改为可访问的 HTTPS Git URL。

`ONLINE_UPDATE_APPLY_COMMAND` 建议指向一个固定脚本。脚本里可以按你的部署方式执行迁移、构建和重启，例如裸机部署可执行 `make migrate`、前端构建、重启 systemd 服务；Docker 部署的 Compose 重建/重启必须由宿主机或受控运维环境完成，不要把宿主机 Docker 权限随意挂进业务容器，也不要让 API 容器自己重建自己。

如果代码已经快进但生效命令失败，接口会返回 `partial_failure=true`，后台会保留升级前后版本和错误输出。此时不要重复盲点升级，应先查看输出，修复脚本或依赖后重新执行生效命令。

## 短信配置

短信验证码默认关闭。管理员可在后台开启注册短信验证。

当前实现支持：

- `SMS_PROVIDER=mock`：本地开发使用
- `SMS_PROVIDER=http`：对接自有或第三方 HTTP 短信网关

HTTP 短信网关会收到 JSON：

```json
{
  "phone": "手机号",
  "code": "验证码",
  "sign_name": "短信签名",
  "template_code": "模板编号"
}
```

需要配置：

```env
SMS_PROVIDER=http
SMS_HTTP_URL=https://your-sms-gateway/send
SMS_HTTP_API_KEY=<optional-api-key>
SMS_SIGN_NAME=<your-sign-name>
SMS_TEMPLATE_CODE=<your-template-code>
```

## 关键环境变量

| 变量 | 说明 |
| --- | --- |
| `DEBUG` | 本地可设 `true`，生产必须 `false` |
| `DATABASE_URL` | PostgreSQL 连接串 |
| `REDIS_URL` | Redis 连接串 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | PostgreSQL 连接池容量和溢出连接数 |
| `DB_POOL_TIMEOUT_SECONDS` / `DB_POOL_RECYCLE_SECONDS` | PostgreSQL 获取连接超时和连接回收时间 |
| `REDIS_MAX_CONNECTIONS` | API/Worker/Celery Redis 最大连接数 |
| `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` / `REDIS_SOCKET_TIMEOUT_SECONDS` | Redis 连接和读写超时 |
| `JWT_SECRET` | 登录态签名密钥，生产必须强随机 |
| `PUBLIC_BASE_URL` | 后端公开访问地址，用于媒体 URL |
| `STORAGE_BACKEND` | 媒体存储后端：`local` 或 S3/MinIO 兼容的 `s3` |
| `STORAGE_MIRROR_LOCAL` | S3 灰度期同时保留新对象的本地镜像，便于回滚 |
| `STORAGE_S3_ENDPOINT_URL` / `STORAGE_S3_BUCKET` | S3/MinIO endpoint 与 bucket；AWS S3 可留空 endpoint |
| `STORAGE_S3_PUBLIC_BASE_URL` | 对象存储或 CDN 的媒体访问前缀；生产必须 HTTPS |
| `CORS_ORIGINS` | 前端允许来源 |
| `TRUSTED_PROXY_IPS` | 可传递 `X-Forwarded-For` 的反代 IP |
| `GATEWAY_BASE_URL` / `GATEWAY_API_KEY` | OpenAI 兼容模型网关兜底配置 |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | 提示词优化专用 Anthropic 兼容网关兜底配置 |
| `VIDEO_GATEWAY_BASE_URL` / `VIDEO_GATEWAY_API_KEY` | 视频模型网关兜底配置 |
| `MODEL_CONFIG_SECRET` | 加密后台保存的模型 API Key |
| `PAYMENT_CONFIG_SECRET` | 加密后台保存的支付密钥 |
| `PAYMENT_MOCK_ENABLED` | 支付 mock 开关，生产必须 `false` |
| `METRICS_TOKEN` | `/metrics` 访问令牌 |
| `ONLINE_UPDATE_ENABLED` | 管理后台版本升级开关，默认关闭；仅真实 Git checkout 且配置固定生效命令后设为 `true` |
| `ONLINE_UPDATE_REPO_DIR` | 在线升级使用的 Git checkout 绝对路径 |
| `ONLINE_UPDATE_REMOTE` | 在线升级 Git 源，默认 `https://github.com/yinyue111/DAIstudio.git` |
| `ONLINE_UPDATE_BRANCH` | 在线升级分支，默认 `main` |
| `ONLINE_UPDATE_GITHUB_TOKEN` | 私有 GitHub 仓库只读 token，不要写进 remote URL |
| `ONLINE_UPDATE_APPLY_COMMAND` | 代码更新后执行的固定生效命令 |
| `ONLINE_UPDATE_ALLOW_LOCAL_REMOTE` | 是否允许在线升级使用本地 Git remote，默认 `false`，仅建议测试环境开启 |
| `MAX_IMAGE_N` | 单次图片生成最大张数，默认 8 |
| `MAX_IMAGE_DIM` | 图片最大边长，默认 2048；当前前端隐藏 4K，2K 会按请求提交并展示网关实际返回图 |
| `MAX_VIDEO_SECONDS` | 上传/参考视频源最大时长，默认 900 秒 |
| `MAX_VIDEO_GENERATION_SECONDS` | 单条生成视频最大时长，默认 15 秒，应用层硬上限 15 秒 |
| `REVERSE_VIDEO_FRAMES` | 视频反推抽帧数量 |
| `WS_TICKET_RATE_PER_MINUTE` | 单用户 WebSocket ticket 申请频率，默认 60/分钟 |
| `WS_CONNECT_RATE_PER_MINUTE` | 单用户 WebSocket 连接频率，默认 60/分钟 |
| `WORKER_CRITICAL_CONCURRENCY` | 短任务 worker 并发，默认 2；只消费 `default,payment,video_poll,cleanup`，避免支付/轮询被长任务饿死 |
| `WORKER_IMAGE_CONCURRENCY` | 图片生成 worker 并发，默认 4；只消费 `image` |
| `WORKER_VIDEO_SUBMIT_CONCURRENCY` | 视频提交 worker 并发，默认 1；只消费 `video_submit` |
| `WORKER_VIDEO_DOWNLOAD_CONCURRENCY` | 视频下载落盘 worker 并发，默认 1；只消费 `video_download` |
| `WORKER_PARSE_CONCURRENCY` | 链接解析/爬虫 worker 并发，默认 1；只消费 `parse`，用于隔离 Playwright/反爬波动 |
| `NEXT_PUBLIC_API_BASE` | 浏览器访问 API 的地址；反代部署保持为空，浏览器走同源 `/api`；仅 Compose 直连且没有反代时设为 `http://localhost:8000` |
| `API_INTERNAL_BASE` | 前端服务端/中间件访问 API 的内部地址；Compose 默认 `http://api:8000`，兼容旧变量 `INTERNAL_API_BASE` |

完整配置见 `backend/.env.example`。

### 本地媒体迁移到 S3/MinIO

迁移脚本保持原有对象 key，默认只演练且永不删除本地文件。推荐先配置
`STORAGE_BACKEND=s3`、bucket 和访问凭据，并在灰度期启用
`STORAGE_MIRROR_LOCAL=true`：

```bash
cd backend

# 1. 只列出待迁移对象，不写入远端
.venv/bin/python -m scripts.migrate_media_storage

# 2. 上传并逐对象下载计算 SHA-256 校验
.venv/bin/python -m scripts.migrate_media_storage --execute --verify sha256

# 3. 服务切流前再次只校验，不重复上传
.venv/bin/python -m scripts.migrate_media_storage --verify-only --verify sha256

# 4. 可选：只安装终止残留分片的非破坏性生命周期规则
.venv/bin/python -m scripts.migrate_media_storage \
  --verify-only --verify sha256 --configure-lifecycle --execute
```

如果远端已有同 key 但大小不同，脚本会失败而不是覆盖；确认以本地副本为准时才使用
`--overwrite`。灰度期不要清理 `STORAGE_DIR`。回滚时停止写入后将
`STORAGE_BACKEND` 改回 `local` 并重启 API/Worker，原 URL 和对象 key 无需修改。

完整备份、逐对象校验、非生产恢复演练和 S3/MinIO 恢复命令见
`docs/storage-backup-recovery-runbook.md`。备份中的媒体包不再依赖本地镜像：
`STORAGE_BACKEND=s3` 时会直接遍历 bucket 中的稳定对象 key，生成带 SHA-256 清单的快照。

## 测试与发布检查

常用质量门禁：

```bash
make compile
make lint
make test
make test-frontend
make build-frontend
make alembic-check
make compose-check
make release-check-worktree
make release-source-worktree
```

发布源码包：

```bash
make release-source
```

发布检查会拒绝以下内容进入交付包：

- `.env`、密钥、证书私钥
- `.git`、`.codex`、`.agents`
- Python 虚拟环境
- `node_modules`
- `.next`
- 本地 storage、数据库、日志、coverage/report

上线前请按 `docs/security/release-security-runbook.md` 执行密钥轮换、Git 历史清理和 release 包验收。

## 安全与合规注意事项

- 平台内置 SSRF 防护：抓取和网关请求会拒绝内网、回环、链路本地、元数据地址和保留地址。
- 登录使用 HttpOnly Cookie，生产环境会拒绝默认 `JWT_SECRET`。
- 生成、上传、解析、支付回调均有大小限制和速率限制。
- 用户上传视频会先通过 ffmpeg 重编码为去元数据的标准 MP4，再进入存储、网关和下载链路；原始上传字节不会作为平台素材长期保存。
- 高清素材未解锁前不会返回下载地址。
- 积分流水采用冻结、结算、退款、解锁的分阶段记账方式，避免重复扣费。
- 模型调用失败、超时、上游状态未知时，任务可能进入 `needs_review`，由管理员人工对账处理。
- 小红书、抖音等平台可能触发安全校验或反爬限制，抓取失败不代表平台后端异常；当前抓取运行在独立 `parse` worker 中，生产上可进一步替换为内部 fetcher 微服务。失败时应引导用户改用手动上传参考素材。
- 真正泄露过的 API Key、支付密钥、短信密钥必须在供应商控制台轮换；删除本地文件或提交记录不能让旧密钥失效。

## 常见命令

```bash
make help                    # 查看命令
make migrate                 # 数据库迁移
make run-api                 # 启动后端 API
make run-worker              # 启动 Celery Worker
make run-beat                # 启动 Celery Beat
make run-frontend            # 启动前端
make docker-up               # Docker Compose 启动全栈
make docker-down             # Docker Compose 停止全栈
make clean                   # 清理缓存和构建产物
```

## 生产上线最小清单

1. DNS 指向服务器，HTTPS 证书可用。
2. Nginx 按 `deploy/nginx/dream.aiwuq.cn.conf` 配置并 reload。
3. `DEBUG=false`，所有密钥为强随机值。
4. PostgreSQL、Redis、API、Worker、Beat、Frontend 全部健康。
5. `make release-check-worktree` 或 CI 全部通过。
6. 管理员能登录后台。
7. 模型配置探测通过，图片/视频各完成一次真实生成测试。
8. 支付宝/微信支付回调验签通过，测试订单能正确入账。
9. 短信开关按需开启，真实短信发送成功。
10. 发布包不包含 `.env`、`storage`、`.venv`、`node_modules`、`.next` 等本地文件。
