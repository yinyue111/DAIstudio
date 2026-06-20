# AI 素材生成平台(内部工具)

员工粘贴一个网页链接 → 平台抓取页面图片/视频 → 理解风格 → 调用大模型生成同风格新素材 →
按内部额度扣费后下载。把「找参考 → 描述风格 → 写提示词 → 调模型」自动化。

本仓库是**可跑通的 MVP**:手机号登录 + 白名单、链接抓取(小红书/抖音/普通网页)、图像/视频反推、
文生图/图生图、视频两段式生成、预览 + 选择解锁、内部额度记账、支付套餐/渠道配置、管理后台、用量报表和审计日志。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Next.js 16 (App Router) + React + Tailwind,H5 友好 |
| 后端 | Python FastAPI(异步) |
| 队列 | Celery + Redis(生成异步) |
| 抓取 | Playwright(装了就用)/ 自动回退 httpx + BeautifulSoup |
| 数据库 | PostgreSQL(额度/任务需事务一致) |
| 存储 | 本地文件系统(MVP),抽象层可换 MinIO/OSS |
| 认证 | 手机号 + 密码(PBKDF2)+ JWT,白名单准入,登录防爆破,短信验证码支持 mock/HTTP 网关 |

模型能力全部收敛为对**你已有网关**的标准 HTTP 调用(同 `base_url + api_key`,只换 `model_id`):
反推 `POST /v1/chat/completions`、文生图 `POST /v1/images/generations`、文生视频(异步 submit/poll)。

---

## 先决条件

- Python 3.10+
- Node.js 20.9+
- PostgreSQL 13+(本地裸跑)
- Redis 6+
- **ffmpeg**(可选,用于视频反推抽关键帧;缺失时自动回退到封面图。Docker 镜像已内置)

创建数据库:

```bash
createdb ai_studio        # 或: psql -c "CREATE DATABASE ai_studio;"
```

如需自定义连接串,改 `backend/.env` 的 `DATABASE_URL` / `REDIS_URL`。

---

## 开发与质量门禁

根目录 `Makefile` 收敛了常用命令(`make help` 查看全部):

```bash
make install          # 建 venv + 装后端运行/开发依赖(含 ruff)
make install-frontend # 装前端依赖
make lint             # ruff 静态检查(CI 同款)
make fmt              # ruff 自动修复 + 格式化
make test             # pytest(sqlite + fakeredis + eager Celery + mock 网关,无需外部服务)
make migrate          # alembic upgrade head
make run-api / run-worker / run-beat / run-frontend
make docker-up        # 一键起全栈
```

CI(`.github/workflows/ci.yml`)在每次 push / PR 上跑:**后端 `ruff check` + `pytest`,前端 `next build`**。
后端测试自带隔离(SQLite + fakeredis + eager Celery + mock 网关),不依赖真实 Postgres/Redis/网关,本地与 CI 一致可复现。

---

## 一键安装

```bash
chmod +x scripts/*.sh
ADMIN_PHONE=13800000000 ./scripts/setup.sh
```

`setup.sh` 会:建 venv 并装后端依赖 → (可选)装 Playwright Chromium → 显式执行本地开发建表 + 写入种子模型配置 +
创建管理员(并发放 1000 额度)→ 前端 `npm install`。生产初始化不要走开发建表，必须先跑 Alembic 迁移。

> 网关密钥已写入 `backend/.env`(`GATEWAY_BASE_URL` / `GATEWAY_API_KEY`)。该文件已被 `.gitignore`,
> 不要提交。模型 `model_id` 请按你网关 `/v1/models` 的真实值在「管理后台 → 模型配置」里改。

---

## 启动(四个终端)

```bash
./scripts/run_backend.sh     # http://localhost:8000  (API + /media 静态)
./scripts/run_worker.sh      # Celery 生成 worker
./scripts/run_beat.sh        # Celery Beat:卡死任务回收 / 视频轮询恢复 / 清理任务
./scripts/run_frontend.sh    # http://localhost:3000  (Web)
```

打开 http://localhost:3000,用管理员账号登录:**手机号 `13800000000`**。管理员密码不再有固定默认值——
`setup.sh` 会调用 `init_db`,**未设 `ADMIN_PASSWORD` 时它会生成一次性随机密码并打印**(仅显示一次,请保存并尽快改密);
也可在运行前 `export ADMIN_PHONE=... ADMIN_PASSWORD=...` 自定义。其他员工在登录页切到「注册」,
用白名单内的手机号 + 自设密码注册即可。短信验证码注册默认关闭，可在管理后台完成短信网关配置后再开启。

---

## Docker 一键部署(推荐生产)

无需本地装 Postgres/Redis/Node:

```bash
export POSTGRES_PASSWORD='<强随机数据库密码>'
export METRICS_TOKEN='<强随机监控访问令牌>'
export PAYMENT_CONFIG_SECRET='<强随机密钥,至少32位,用于加密后台保存的支付/模型密钥>'
docker compose up -d --build
# 首次:等待 migrate 服务完成 Alembic 后,建管理员 + 种子配置
# (设强密码,或省略 ADMIN_PASSWORD 让 init_db 打印一次性随机密码)
ADMIN_PASSWORD='<强随机密码>' docker compose run --rm -e ADMIN_PASSWORD api \
  python -m scripts.init_db --admin-phone 13800000000 --credits 1000
```

起 Postgres + Redis + 迁移任务 + API + Worker + Beat + 前端；生产迁移由独立 `migrate` 服务执行
`alembic upgrade head`，API 容器只在迁移成功后启动。`init_db` 在生产只做 seed/admin，不会隐式建表。
网关密钥仍从 `backend/.env` 读;`DATABASE_URL/REDIS_URL` 由 compose 注入。生产默认同域部署:
`https://dream.aiwuq.cn` 访问前端,`/api`、`/media`、`/ws` 反代到后端。Nginx 模板见
`deploy/nginx/dream.aiwuq.cn.conf`。
Compose 默认只把 API/前端绑定到 `127.0.0.1:${API_PORT:-8000}` 和
`127.0.0.1:${FRONTEND_PORT:-3000}`,不要直接裸露到公网;公网流量走 nginx/HTTPS。生产还需要在
`backend/.env` 或宿主环境中确认强 `JWT_SECRET`、`METRICS_TOKEN` 和 `PAYMENT_CONFIG_SECRET`。短信验证码注册和支付充值默认关闭；
开启短信前配置真实 HTTP 短信网关(`SMS_PROVIDER=http` + `SMS_HTTP_URL`)；开启真实支付前设置支付密钥加密用的
`PAYMENT_CONFIG_SECRET`，再在管理后台配置商户资料和套餐。后台模型 API Key 默认也使用该密钥加密
(如需分离可另设 `MODEL_CONFIG_SECRET`)。Compose 会在缺少 `PAYMENT_CONFIG_SECRET` 时直接拒绝启动，并默认覆盖为
`DEBUG=false`、`PAYMENT_MOCK_ENABLED=false`;本地调试才显式
`DEBUG=true PAYMENT_MOCK_ENABLED=true docker compose ...`。

## 数据库迁移(Alembic)

生产用迁移而非自动建表:

```bash
cd backend
alembic upgrade head                              # 应用迁移
alembic revision --autogenerate -m "add xxx"      # 改了 models 后生成新迁移
```

(本地裸跑时应用启动会 `create_all` 兜底建表,方便快速试。)

---

## 端到端流程(与产品一致)

1. 手机号 + 密码 注册 / 登录(注册需手机号在白名单内;短信验证码可由管理员配置后开启)。
2. 粘贴链接(小红书/抖音/普通网页) → 抓取页面 → 返回图片/视频资源列表。
3. 勾选要参考的一张图 / 一段视频。
4. (可选)反推:调视觉大模型输出结构化提示词,可编辑;关闭反推则走「图 + 指令 → 图」。
5. 选用途与参数 → 提交 → **冻结预估额度** → 异步生成。
6. 产出带水印/低清预览候选。
7. 选中满意的 → **扣额度解锁** → 下载高清(内部无水印)。
8. 视频:先出低成本预览,选定方向后再扣大额渲染完整视频。

---

## 离线 / 演示模式

把 `backend/.env` 的 `MOCK_MODE=true`(或留空 `GATEWAY_API_KEY`),所有网关调用返回本地生成的占位图/视频,
整条链路无需真实网关即可跑通,方便演示与联调。

---

## 额度记账(一致性)

所有变动写 `credit_transactions` 流水,余额用 `SELECT ... FOR UPDATE` 行锁更新:

- 提交任务 → `freeze`(冻结预估)
- 成功 → `settle`(按实际消耗结算,差额退回)
- 失败 → `refund`(自动全额退回)
- 选中解锁 → `unlock`(直接扣)

管理员按人/部门 `grant` 发放;「用量报表」按人/按部门给出消耗,供内部分摊。

---

## 安全要点

- **SSRF(红线)**:`services/ssrf.py` 仅放行 http/https,解析后任一 IP 命中
  内网/回环/链路本地(含 `169.254.169.254` 元数据)/保留/多播/CGNAT 一律拒绝;
  额外归一化 IPv4-mapped IPv6(`::ffff:127.0.0.1`)。抓取**禁用自动重定向**,
  每一跳的 Location 重新做 SSRF 校验;Playwright 用 `page.route` 拦截内网请求。
- **输入校验**:生成参数 `n`(≤8)、`size`(WxH 且单边 ≤4096px)、`duration`(≤`MAX_VIDEO_SECONDS`, 默认 900s/15min)
  在入口 clamp/拒绝,挡住资源耗尽。
- 密码:PBKDF2-SHA256 加盐哈希存储;登录失败防爆破**按手机号 + 按 IP** 双维度限流。
- **JWT**:`DEBUG=false` 且仍为默认密钥时拒绝启动(防伪造 token)。
- 密钥:网关 key 放 `.env`,不入库、不入日志;`.env` 已 gitignore;后台只读视图掩码展示。
- 高清不外泄:未解锁时接口不返回 `hd_url`,下载接口校验登录 + 解锁状态。
- 幂等:已到终态的生成任务再次执行会被跳过,避免重复扣费。

---

## 本轮 spec 对齐 / 优化(新增)

- **反推精度升级**:图片/视频反推模板大幅强化,要求像素级、可量化(色值/百分比坐标/焦段/角度),
  最大化复刻参考;`temperature` 降到 0.2 提升稳定性。
- **视频关键帧理解**:反推视频时用 `ffmpeg` 抽取首帧 + 等间隔关键帧(`services/video_frames.py`),
  以 base64 多帧喂给视觉模型推断运动,省带宽;无 ffmpeg / 抽取失败时自动回退到封面图。
  > 部署需在镜像内装 `ffmpeg`(后端 Dockerfile 已加);可用 `REVERSE_VIDEO_FRAMES` 调抽帧数。
- **图+指令→图 默认开启**:关闭反推时,参考图 + 指令默认走图生图 edit 端点(`IMAGE_EDIT_PATH`,
  默认 `/v1/images/edits`)。
  > **若你的网关没有该端点**,把图像模型 `extra.edit_path` 设为 `""` 或 `IMAGE_EDIT_PATH=` 关闭,
  > 否则关闭反推的生成会因 404 失败(失败会自动退额度)。
- **按张计费**:文生图冻结/结算额度 = `cost_credits × n`(出几张扣几份)。
- **真实成本记账**:每次网关调用写 `gateway_calls` 流水(类型/模型/时延/token 用量);
  反推捕获视觉模型返回的 `usage`(真实 token);用量报表新增「真实 token 消耗」列与 CSV 字段。
  迁移:`alembic upgrade head`(`0004_gateway_calls`)。
- **反推开关服务端生效**:管理员关闭后 `POST /api/prompt/reverse` 直接 403,不再只是前端隐藏。
- **网关稳定性**:统一重试覆盖图片/视频全部调用(原仅 `_post`),只重试连接错误/超时/429/5xx,
  4xx 立即失败;视频 submit 不重试(防重复任务);下载失败归一化为 `GatewayError`。
- **退款幂等(P0)**:终态转换走原子 `claim_terminal()` CAS,只有抢到「非终态→终态」的一方执行
  settle/refund;worker、reaper、重复投递并发也**恰好结算/退款一次**。
- **账务/SSRF/路径加固**:反推按视觉模型成本同步扣费(`consume`/失败 `refund_consumed`);
  `/generate`、`/reverse` 的用户素材 URL 统一过 SSRF(放行本站 `/media`);`key_from_url` 防 `..` 穿越;
  生产 `DEBUG=false` 且 JWT 仍为默认值则拒绝启动;默认管理员密码改为一次性随机/必填,不再打印固定明文。
- **可恢复视频生命周期**:视频改为**非阻塞状态机** —— `generate.video` 只负责 submit(持久化
  `external_task_id`/`external_submitted_at`/`phase`),再由自我续期的 `poll.video` 任务轮询到完成,
  worker 不再被长渲染占用;`cleanup.resume_videos` beat(每 2 分钟)通过 Redis 存活键发现「轮询链已死」
  (worker 崩溃)的在途任务并用 `external_task_id` **续查恢复**,避免外部任务丢结果。
  迁移:`alembic upgrade head`(`0005_video_lifecycle`)。

---

## 目录结构

```
backend/
  app/
    main.py            FastAPI 入口(建表+种子+/media 静态)
    config.py          env 配置 + models.yaml 种子
    models.py          ORM(见建表 SQL)
    schemas.py         Pydantic
    deps.py            鉴权依赖 / admin 守卫
    security.py        JWT
    celery_app.py      Celery
    tasks.py           Celery 任务(generate.image / generate.video)
    services/
      sms.py           验证码(mock / 阿里云占位)
      credits.py       冻结/结算/退款/解锁(行锁)
      ssrf.py          SSRF 防护(红线)
      fetcher.py       抓取 + 媒体提取(Playwright/bs4)
      gateway.py       网关封装(反推/文生图/视频 + mock)
      watermark.py     水印/低清预览(Pillow)
      generation.py    生成编排(两段式 + 结算 + 退款)
      config_store.py  模型/默认值(DB,可后台改)
      progress.py      进度(Redis)
      audit.py         审计日志
    routers/           auth / me / parse / prompt / generate / tasks / assets / admin / ws
  models.yaml          模型用途 + 成本积分种子
  scripts/init_db.py   种子 + 建管理员(仅 --create-tables-dev-only 时开发建表)
  .env(.example)       配置(密钥)
frontend/
  app/                 login / 工作台(/) / 个人主页(/profile) / 历史记录(/history) / 管理后台(/admin)
  components/Nav.jsx   顶部导航
  app/globals.css      设计系统(Claude/Anthropic 风格令牌 + 组件类)
  tailwind.config.js   品牌色板 + 字体(Poppins/Lora)
  lib/api.js           API 客户端
scripts/               setup / run_backend / run_worker / run_frontend
```

---

## 接口清单

认证:`POST /api/auth/register`、`POST /api/auth/login`(手机号+密码)、`GET /api/me`、`GET /api/config`
健康:`GET /api/health`(含 DB / Redis 探活)
个人主页:`GET /api/profile`、`GET /api/profile/assets?type=image|video|all`(30 天保留,过期自动过滤/清理)
解析/反推:`POST /api/parse`、`GET /api/parse/{id}`、`POST /api/prompt/reverse`
生成/下载:`POST /api/generate`、`GET /api/tasks`、`GET /api/tasks/{id}`、
`WS /ws/tasks/{id}`、`POST /api/assets/{id}/unlock`、`GET /api/assets/{id}/download`
管理:`GET/POST/DELETE /api/admin/whitelist`、`GET /api/admin/users`、
`PATCH /api/admin/users/{id}/status`、`POST /api/admin/quota/grant`、
`GET /api/admin/usage/report`(`?start=&end=&format=csv`)、`GET/PUT /api/admin/models`、
`GET/PUT /api/admin/settings`、`GET /api/admin/audit`、`GET /api/admin/gateway`

交互式文档:启动后访问 http://localhost:8000/docs 。

---

## 里程碑对照

- **MVP(本仓库)**:手机号登录 + 白名单、链接抓取(小红书/抖音/普通网页)、文生图(直调网关)、预览 + 选择解锁、额度记账。✅
- **V1**:反推开关 ✅(平台默认可后台配置)、视频两段式 ✅(final 校验并继承预览提示词)、
  用量成本报表 ✅(日期筛选 + CSV 导出)、管理后台 ✅、审计日志 ✅(可视化查询)。
- **V2**:历史管理 ✅(/history)、个人主页 ✅(/profile 素材画廊 + 30 天保留)、批量生成、限流与分摊完善、视频异步稳定性打磨。

### 本轮 P1/P2(新增)

- **账户安全**:改密码(自助)、管理员重置密码、服务端登出 —— 均通过 `token_version` 失效全部旧 JWT(`POST /api/me/password`、`/api/me/logout`、`/api/admin/users/{id}/reset_password`)。
- **素材管理**:收藏 / 删除 / 收藏筛选,个人主页与历史均支持「加载更多」分页(`POST /api/assets/{id}/favorite`、`DELETE /api/assets/{id}`)。
- **可靠性**:失败任务一键重试(`POST /api/tasks/{id}/retry`);卡死任务自动回收 —— 超时仍在 queued/running 的任务每 10 分钟标记失败并退款(`cleanup.reap_stuck` beat)。
- **更多生成参数**:文生图 seed、负向提示词;文生视频时长 / 分辨率(透传到网关 payload)。
- **报表**:用量报表新增「每日消耗趋势」柱状图。
- **监控/备份**:`GET /metrics`(Prometheus,请求量/时延 + 进程指标)、`scripts/backup_db.sh`(pg_dump 每日备份,保留 14 天)。

### 上一轮生产化

- **认证改为手机号 + 密码**:注册(白名单内)/ 登录,PBKDF2 加盐哈希,登录失败防爆破;短信验证码支持后台开关。
- **实时进度 WebSocket**:工作台经 `WS /ws/tasks/{id}` 推进度,断连自动回退轮询。
- **Alembic 迁移**:`alembic/` 初始迁移就绪,`alembic upgrade head` 可建全表;生产用迁移代替自动建表。
- **Docker 一键部署**:`docker-compose.yml` + 前后端 Dockerfile(API 自动迁移、Worker 含每日清理 beat)。
- **可观测性**:请求 ID 贯穿日志、访问日志中间件、`/api/health` 公开存活探针、`/api/health/detail` 受保护依赖探活、可选 Sentry(`SENTRY_DSN`)。
- **全表留存清理**:素材(30 天)+ 任务 + 解析记录(7 天)+ 审计(90 天,可配),每日 beat / cron 清理。
- **集成测试**:TestClient + SQLite + fakeredis + eager Celery,覆盖注册登录、生成→解锁→画廊、额度不足、鉴权、健康检查、支付与抓取安全。

### 个人主页与素材保留(本轮新增)

- `/profile`:个人信息 + 额度 + 统计,按「全部 / 图片 / 视频」筛选的素材画廊,视频内联播放,
  点击放大预览、解锁、下载,并显示「N 天后过期」。
- 默认保留 **30 天**(可在「管理后台 → 平台设置 → 素材保留天数」修改):过期素材在画廊加载时被过滤并尽力清理。
- 彻底清理任务:`backend/scripts/cleanup_expired.py`(建议 cron 每天跑一次,见文件内注释),
  或 Celery 任务 `cleanup.expired_assets`(可配 celery beat 定时)。

### 前端设计(本轮重构)

- 全站采用 Claude/Anthropic 视觉风格:米白底(#faf9f5)、Claude 橙强调色(#d97757)、暖中性色,
  Poppins 标题 + Lora 正文(通过 Google Fonts 在浏览器加载,首次需联网)。
- 统一设计系统:`tailwind.config.js` 定义品牌色板与字体,`globals.css` 提供 `.card/.btn-*/.input/.chip/.badge`
  等组件类;新增顶部导航 `components/Nav.jsx`(工作台 / 历史记录 / 管理后台 + 额度)。
- 工作台改为分步卡片式布局,结果区支持点击放大预览;新增「历史记录」页。
- 工作台当前为聚焦式单列控制台 + 下方作品墙:「提示词/参考素材/参数/生成结果」集中在主流程内,移动端和桌面端保持一致。

### 本轮 V1 优化(对照需求新增)

- 审计日志可视化:`GET /api/admin/audit`(按 action/user 过滤)+ 后台「审计日志」页。
- 平台设置:`GET/PUT /api/admin/settings`(默认反推开关、出图数量、尺寸)+ 后台「平台设置」页;
  前端通过 `GET /api/config` 读取默认值与各模型积分成本。
- 网关状态只读视图:`GET /api/admin/gateway`(base_url + 掩码 key + 是否 mock)。
- 用量报表:支持 `?start=&end=` 日期范围与 `?format=csv` 导出。
- 成本/并发安全:生成任务加 Redis 幂等锁,避免重复投递导致重复扣费;新增按用户抓取限流。
- 视频两段式:`final` 阶段强制校验所属预览任务并继承其提示词。

### 识别与生成体验优化

- 图片反推维度大幅扩充(主体/细节特征/场景/风格/构图/视角镜头/光线/色调配色/材质纹理/氛围/后期质感/负向),
  并要求模型给出可量化描述,使复刻图更接近参考图。
- 新增视频专用反推模板:基于封面/关键帧推断主体动作、镜头运动、运动节奏、时序分镜、时长与转场,
  生成可连续播放的视频提示词(`POST /api/prompt/reverse` 增加 `target=image|video`)。
- 反推结构化字段在前端动态渲染,后端字段调整无需改前端。
- 预览后再导出:候选图/结果图可点击放大预览(带水印),在预览弹窗内解锁、解锁后再下载高清。
- 出图尺寸扩充为 1:1 / 竖屏 / 横屏 / 16:9 / 9:16 多档(由 `GET /api/config` 下发,后台可设默认)。

---

## 备注 / 待办

- 视频网关已接入 **火山方舟 Volcengine Ark / 豆包 Seedance**(异步任务接口):
  - 凭据在 `backend/.env`:`VIDEO_GATEWAY_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`、
    `VIDEO_GATEWAY_API_KEY=ark-...`、`VIDEO_GATEWAY_FORMAT=ark`(视频网关独立于图片/视觉网关)。
  - 适配器:`submit` → `POST /contents/generations/tasks`(content 数组,参数以 `--resolution/--duration/--ratio` 等
    flag 附在文本上;选了图片素材时作为首帧做图生视频),`poll` → `GET /contents/generations/tasks/{id}`,取 `content.video_url`。
  - 两段式:预览=480p 短片(免费观看),定稿=1080p 完整视频(解锁后下载);生成后**自动下载并本地落盘**(Ark 链接约 24h 过期)。
  - **已有数据库需在「管理后台 → 模型配置」把 video 的 model_id 设为 `doubao-seedance-1-5-pro-251215` 并启用**
    (`models.yaml` 仅对全新初始化生效;种子不会覆盖已存在的行)。
  - 其它视频网关:把 `VIDEO_GATEWAY_FORMAT=openai` 走通用适配(路径见 `models.yaml` 的 `video.extra`)。
- 短信:`services/sms.py` 内置本地 mock 和通用 HTTP 短信网关适配器。短信验证码注册默认关闭；生产开启前设置
  `SMS_PROVIDER=http`、`SMS_HTTP_URL=<短信服务 HTTPS 地址>`、可选 `SMS_HTTP_API_KEY`，
  并在管理后台打开“短信验证码注册”。该网关会收到 `{phone, code, sign_name, template_code}` JSON。
  直连阿里云/腾讯云 SDK 还未启用，需要新增 provider 实现、签名/模板报备和测试后再开放生产配置。
- 存储:`services/storage.py` 为本地实现,换 MinIO/OSS 只需替换 `save_bytes/public_url`。
- 生产部署:Compose 是单机/MVP 支持路径(API 用单进程 uvicorn,前置 nginx/HTTPS);高并发场景再切到
  PostgreSQL/Redis 独立实例、前端 `next build && next start`、后端 gunicorn+uvicorn 多 worker,并同步健康检查和超时参数。
