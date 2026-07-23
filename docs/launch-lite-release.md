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
5. 数据库执行 `alembic upgrade head`，并确认当前 revision 为 `0070_video_model_capabilities`。

## 发布检查

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/ruff check app
.venv/bin/pytest -q

cd ../frontend
npm run typecheck
npm run test:unit
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
