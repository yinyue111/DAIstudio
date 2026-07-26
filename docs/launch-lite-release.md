# 精简版上线清单

## 上线范围

- 统一 Studio：文生图、图片编辑、文生视频、图生视频。
- 单图和单视频提示词反推。
- Studio 内提示词优化，不拆独立入口。
- 提示词库：系统内置案例、我的提示词、收藏与一键套用。
- 任务中心、历史记录、资产预览与下载、积分账户。
- 精简管理后台，用于用户、模型、任务、积分和审计管理。

`PRODUCT_EDITION=launch_lite` 会同时隐藏前端入口并将对应 API 置为 404：批量反推、视频组合、复刻评估/修复、创作配方（含公开发现）、项目、工具工作流。提示词库及 `/api/prompts/history` 保留可用。在精简版中在线支付强制关闭，但保留积分余额和账单查询。

## 发布前配置

1. 设置 `PRODUCT_EDITION=launch_lite`。
2. 在管理后台为 `vision` / `image` / `video` / `prompt` 各启用至少一个已验证模型。
3. 后台新增模型默认启用，并会自动进入对应的 Studio 模型下拉框；停用后自动移除。上线前只启用已完成真实供应商冒烟测试的模型。
4. 使用强随机 `JWT_SECRET`、`MODEL_CONFIG_SECRET`、`PAYMENT_CONFIG_SECRET` 和 `METRICS_TOKEN`，并配置正确的 `CORS_ORIGINS` / `PUBLIC_BASE_URL`。
5. 数据库执行 `alembic upgrade head`，并用 `alembic heads` 与 `alembic current` 对照，确认数据库 revision 与代码里的迁移链头一致（不要依赖文档写死的 revision 号，以 `backend/alembic/versions/` 实际链头为准）。

## 发布检查

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/ruff check app
.venv/bin/pytest -q

cd ../frontend
npm run typecheck
npm run test:unit
npm run test:e2e   # Playwright 端到端（接口走路由拦截 mock，无需后端）
npm run build

cd ..
make release-check-worktree
make release-source-worktree
```

当前工作区发布包默认输出为：

- `dist/ai-studio-launch-lite-20260721-source.tar.gz`
- `dist/ai-studio-launch-lite-20260721-source.tar.gz.sha256`

部署前在仓库根目录执行 `cd dist && shasum -a 256 -c ai-studio-launch-lite-20260721-source.tar.gz.sha256`，校验通过后再解压。正式环境只复制并填写 `backend/.env.example`，发布包不会包含本机 `backend/.env`。

使用正式域名在桌面端和移动端逐项验证：登录，四种创作模式，图片/视频反推，提示词优化，系统提示词浏览/套用，我的提示词保存/收藏/套用，任务进度，历史记录，资产预览/下载，积分扣减/退回，管理员查询。每个真实模型至少成功一次，并核对超时、失败、取消时的积分一致性。

## 暂不放行

- 任一模型仅在 Mock 模式成功。
- 任一启用模型未通过真实供应商冒烟测试。
- 对象存储、备份恢复或定时清理未验证。
- 积分并发扣减/退回、任务重试或容器重启后恢复未验证。
- 桌面端和移动端核心流程未完成真实浏览器验收。

## 解禁到 full 的前置清单

把 `PRODUCT_EDITION` 从 `launch_lite` 切到 `full`（或用 `FEATURE_*_ENABLED` 单项覆盖）之前，逐条确认：

1. **证据分析 provider 已配置并可达。** 三个分析器（`evidence_provider` 图片检测/分割、`video_semantic_provider` 视频语义、`audio_provider` 音频）在 Compose 里挂在 `profiles: [evidence]` 下，**默认不启动**，需要 `docker compose --profile evidence up -d` 显式拉起。仅启动 provider 不够：后端只从 `BACKEND_ENV_FILE`（默认 `backend/.env.production`）读取配置，必须在该文件里补上各 provider 的 URL / API key / health URL——`IMAGE_EVIDENCE_DETECTOR_URL`、`IMAGE_EVIDENCE_SEGMENTER_URL`、`VIDEO_EVIDENCE_SEMANTIC_URL`（正确值为 `http://video_semantic_provider:8091/v1/video-semantic/analyze`）等，逐项取值见 `backend/.env.example` 中对应变量的注释。留空时后端会如实把相应证据能力标记为 unsupported，反推证据门会降级而不是报错，所以配置遗漏不会在启动时暴露，必须在解禁前用 `GET /api/prompt/reverse-analyzers/status`（登录态）逐个核对 image / video / audio 三项健康状态。注意：不要试图在 docker-compose.yml 各服务的 `environment:` 段做 shell 透传注入，Compose v5 的 environment 条目会覆盖 env_file 取值（详见 docker-compose.yml 中 `x-backend-env-file` 处注释）。
2. **reproduction 队列有 worker 消费。** 复刻评估任务路由到 `reproduction` 队列，由 `worker_reverse` 消费（`WORKER_REVERSE_QUEUES` 默认 `reverse,reproduction`）。若自定义过该变量，确认包含 `reproduction`，否则复刻评估会永远停在 queued。
3. **在线支付是独立的 DB 开关，不随版本切换自动打开。** `payment_enabled` 存在系统设置表里（默认 `false`），精简版强制关闭；切到 full 后仍需管理员在后台设置里显式开启，并先完成支付渠道配置与对账验证。
4. **种子数据就绪。** 解禁功能（创作配方公开发现、项目模板、工具工作流等）依赖的系统内置数据需提前用 `scripts/init_db` / 后台导入补齐，避免用户看到空列表。
5. **首日值班安排。** 解禁当天安排后端值班：盯 `reproduction` / `reverse` 队列积压、evidence provider 健康与内存（模型常驻，注意 `*_MEM_LIMIT`）、支付对账任务，准备好按第 1-3 条逐项回退（关 profile / 关 DB 开关 / 改回 `PRODUCT_EDITION`）。
