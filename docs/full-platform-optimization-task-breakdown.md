# AI 图片/视频生成平台完整优化任务拆分

> 基线日期：2026-07-18
> 规格来源：`sora2-qunc-full-platform-evaluation.md`、`full-platform-optimization-implementation-plan.md`、`reverse-workflow-complete-implementation-plan.md`
> 范围：完整目标产品，不按一期或 MVP 缩减。本文中的“完成”必须同时具备代码、迁移、测试和运行证据。

## 1. 已确认假设

1. 产品继续采用 Next.js + FastAPI + PostgreSQL + Redis/Celery，不重写成熟生成、积分和支付底座。
2. Studio 是图片生成、视频生成、图片反推、视频反推和提示词优化的统一工作区。
3. 顶级产品实体固定分为模型、工作流/配方和专项工具，不建设一页一模型的硬编码架构。
4. 生产价格、冻结、结算和退款全部由服务端 quote 与价格版本决定。
5. 反推输出必须先审阅再选择性应用，任何后台完成事件都不得静默覆盖用户工作区。
6. 切换目标生成模型只产生新的 `model_compiled` 版本，不重新调用视觉反推。
7. 图片和视频证据没有真实分析器或分析失败时，必须返回 `unsupported`、`degraded` 或 `partial`。

## 2. 工程命令

```bash
# 后端
cd backend && .venv/bin/pytest
cd backend && .venv/bin/ruff check .
cd backend && .venv/bin/python -m compileall -q app tests scripts alembic
cd backend && .venv/bin/alembic upgrade head
cd backend && .venv/bin/alembic check

# 前端
cd frontend && npm run test:unit
cd frontend && npm run typecheck
cd frontend && npm run build

# 组合发布门禁
make release-check-worktree
```

## 3. 代码边界

- Always：复用现有服务和路由模式；新状态必须持久化；所有权在服务端校验；外部调用具备幂等键；新增能力有聚焦测试。
- Ask first：删除旧表/旧字段、替换供应商、改变积分规则、引入有运行成本的新外部服务。
- Never：提交秘钥；信任客户端金额；伪造 OCR/音频/检测证据；用浏览器临时历史代替服务端记录；为单一模型复制完整 Studio。

## 4. 当前验收快照

- 工程发布门禁已通过：唯一 Alembic head `0059`、`alembic check`、`1522 passed, 1 skipped`、Ruff、compileall、52/52 前端 unit 命令组、typecheck、Next.js 生产构建和 `git diff --check` 均成功。
- 反推 E2E 为 `8 passed`，覆盖图片结构修改/并发冲突、视频多片段/关键帧、取消退款、断线恢复、历史恢复和降级证据。测试使用 API fixture，不代表供应商真实生成。
- 真实本地后端和 MP4 已验证视频区间/关键帧提交前拒绝、扣费前置和刷新恢复；桌面端/移动端截图已留存。
- 当前服务运行在 `mock_mode=true`。图片 detector/segmenter、视频语义 provider、ASR/说话人 provider 和真实 S3 未配置，因此相关任务不得勾选为完成。
- 下方复选框代表严格 DoD，不是“没有代码”。只要真实 provider、对象存储、历史迁移或恢复演练未满足 Acceptance，就继续保持未勾选。

## 5. 功能任务清单

### WP01 AppShell 与能力发现

- [x] `WP01-01` 完成数据驱动的全局导航和权限过滤。
  - Acceptance：导航来自目录契约；隐藏、禁用、无权限状态一致；Studio 是默认入口。
  - Verify：目录接口测试、`nav-layout-stability.test.mjs`、桌面/移动端截图。
  - Files：`backend/app/routers/catalog.py`、`frontend/components/AppShell.jsx`、`frontend/components/Nav.jsx`。
  - Evidence：`test_catalog_versions.py` 与 `test_config_and_estimate.py` 通过；前端全量 unit、typecheck 和 production build 通过；普通用户、管理员、暂停/隐藏状态已在 1440x900 与 390x844 真浏览器验证，截图见 `output/playwright/wp01-*.png`；验收后 `navigation_states` 已恢复为 `{}`。
- [ ] `WP01-02` 完成模型、工具和工作流目录的搜索、筛选、比较与深链回填。
  - Acceptance：深链只改变 Studio 预设，不产生第二套工作台；无效版本有明确降级。
  - Verify：目录契约测试、`catalog-model-comparison.test.mjs`、`workflow-preset.test.mjs`。
  - Files：`backend/app/services/catalog.py`、`frontend/app/catalog/page.jsx`、`frontend/app/studio/workflowPreset.ts`。

### WP02 模型能力、价格与运行路由

- [ ] `WP02-01` 完成 Capability/Price 版本发布、回滚和历史引用。
  - Acceptance：历史任务继续引用原版本；后台可草拟、发布、停用和回滚。
  - Verify：迁移、`test_catalog_versions.py`、后台版本前端测试。
  - Files：`backend/app/services/model_versions.py`、`backend/app/routers/admin_catalog.py`、`frontend/app/admin/components/catalog-versions.jsx`。
- [ ] `WP02-02` 完成服务端报价、防篡改和过期处理。
  - Acceptance：生成只接受有效 quote；参数或模型变化使旧 quote 失效；重复提交不重复冻结。
  - Verify：`test_generation_quotes.py`、账务守恒测试、前端报价测试。
  - Files：`backend/app/services/generation_quotes.py`、`backend/app/routers/generate.py`、`frontend/app/studio/generationQuote.ts`。
- [ ] `WP02-03` 完成 ModelRoute 健康探测、熔断、降级和后台故障演练。
  - Acceptance：路由选择有审计记录；失败不会静默切换到不兼容模型；后台可回滚。
  - Verify：`test_model_routes.py`、`test_generation_model_runtime.py`、路由故障演练。
  - Files：`backend/app/services/model_routes.py`、`backend/app/services/generation_model_runtime.py`、`backend/app/routers/admin_models.py`。

### WP03 工具工作流引擎

- [ ] `WP03-01` 注册生产 `parse/reverse/compile/generate/compose/export` 节点处理器。
  - Acceptance：API 与 worker 启动均幂等注册；节点复用真实服务；未支持能力明确失败。
  - Verify：生产注册测试、真实工具运行测试、worker import smoke。
  - Files：`backend/app/services/workflow_node_adapters.py`、`backend/app/main.py`、`backend/app/celery_app.py`、`backend/app/tasks.py`。
- [ ] `WP03-02` 完成外部任务等待、恢复、取消、重试和补偿。
  - Acceptance：取消生成可退回未结算冻结；重放不重复创建外部任务；worker 丢失可恢复。
  - Verify：`test_tool_workflows.py`、dispatch outbox 测试、Celery eager/非 eager smoke。
  - Files：`backend/app/services/tool_workflows.py`、`backend/app/services/workflow_dispatch.py`、`backend/tests/test_tool_workflows.py`。

### WP04 统一反推任务

- [ ] `WP04-01` 验证单项、批量、幂等、进度、取消、断线恢复和计费全链路。
  - Acceptance：每项可独立重试；恢复不覆盖新素材；失败按实际阶段结算或退款。
  - Verify：反推/批量后端测试、前端批量测试、断线恢复 E2E。
  - Files：`backend/app/services/reverse_operations.py`、`frontend/hooks/useReverseBatches.js`、`frontend/hooks/useRecentReverseOperations.js`。
- [ ] `WP04-02` 验证多参考角色和逐项视频覆盖配置。
  - Acceptance：主素材与商品/人物/风格/构图角色可追溯；视频每项可覆盖片段、关键帧和音频策略。
  - Verify：`test_reverse_batches.py`、`reverse-batches.test.mjs`。
  - Files：`backend/app/schemas.py`、`frontend/app/studio/StudioReverseSourcesEditor.jsx`、`frontend/app/studio/StudioReverseBatchPanel.jsx`。

### WP05 图片证据反推

- [ ] `WP05-01` 完成独立中文 OCR、检测、分割及冲突契约。
  - Acceptance：OCR 真实使用配置语言；语言包缺失时明确降级；VLM 与独立分析冲突可见。
  - Verify：OCR fixture、`test_evidence_analysis_engine.py`、运行环境语言探测。
  - Files：`backend/app/services/image_evidence_analysis.py`、`backend/app/config.py`、`backend/Dockerfile`。
- [ ] `WP05-02` 完成证据区域编辑和版本差异。
  - Acceptance：新增/删除/文字/坐标/审阅/保护模式变化均进入 revision diff。
  - Verify：图片证据前端测试、revision 契约测试。
  - Files：`frontend/app/studio/StudioImageEvidence.jsx`、`frontend/app/studio/reverseResultRevisionDiff.ts`、`frontend/scripts/image-evidence.test.mjs`。
- [ ] `WP05-03` 完成保护/编辑蒙版生成与提交前预检。
  - Acceptance：服务端校验最终蒙版；多参考单编辑源限制在报价前提示；非法区域不能生成。
  - Verify：`test_generation_image_evidence.py`、蒙版像素测试、真实编辑生成。
  - Files：`backend/app/services/generation_image_evidence.py`、`frontend/app/studio/imageEvidence.ts`、`frontend/app/studio/generationPayload.ts`。

### WP06 视频证据反推与合成

- [ ] `WP06-01` 完成多片段镜头切分、自适应抽帧和绝对时间证据。
  - Acceptance：覆盖率只按所选片段计算；重叠区间正确合并；每镜头引用稳定证据帧。
  - Verify：`test_reverse_video_segments.py`、真实多片段 fixture。
  - Files：`backend/app/services/video_frames.py`、`backend/app/services/video_evidence_analysis.py`。
- [ ] `WP06-02` 完成逐帧 OCR track、主体/姿态/动作跟踪、运镜和语义转场。
  - Acceptance：各分析器独立报告状态；VLM 描述不冒充检测证据；单镜头可重新分析。
  - Verify：OCR track、motion/subject fixture、镜头重分析测试。
  - Files：`backend/app/services/video_evidence_analysis.py`、`backend/tests/test_reverse_video_segments.py`、`backend/tests/test_reverse_shot_operations.py`。
- [ ] `WP06-03` 完成 ASR、说话人、节拍、音乐和音效证据。
  - Acceptance：每类特征独立状态；绝对时间准确；无音轨和 provider 失败可解释。
  - Verify：`test_video_audio.py`、真实带音频 MP4 fixture。
  - Files：`backend/app/services/video_audio.py`、`backend/tests/test_video_audio.py`。
- [ ] `WP06-04` 完成逐镜生成、素材替换、拼接、字幕烧录、音频混合和导出。
  - Acceptance：每个镜头绑定生成任务/资产；只使用 owner 可访问素材；导出 MP4 和工程清单可恢复。
  - Verify：ffmpeg fixture、所有权测试、工作流 compose/export E2E、浏览器导出 E2E。
  - Files：`backend/app/services/video_composition.py`、`backend/app/routers/video_compositions.py`、`frontend/app/studio/StudioVideoComposition.jsx`。

### WP07 结果应用与正式血缘

- [ ] `WP07-01` 完成字段级应用、撤销、真实 diff 和并发保护。
  - Acceptance：用户编辑期间完成的后台结果只进入待审阅状态；撤销精确恢复应用前快照。
  - Verify：revision/应用后端测试、`reverse-result-application.test.mjs`。
  - Files：`backend/app/services/reverse_lineage.py`、`frontend/app/studio/reverseResultApplication.ts`、`frontend/app/studio/reverseSnapshot.ts`。
- [ ] `WP07-02` 完成 `model_compiled -> generation` 原子关联和恢复。
  - Acceptance：生成任务引用已验证 revision；模型切换不重跑反推；重试链保留来源。
  - Verify：`test_generation_lineage.py`、`test_reverse_revision_lineage.py`、恢复 E2E。
  - Files：`backend/app/services/reverse_lineage.py`、`backend/app/services/generation_submit.py`、`backend/app/models.py`。
- [ ] `WP07-03` 完成图片/视频源素材与生成结果的复刻度评估和修正版本链。
  - Acceptance：双方资产经过 owner gate 且媒体一致；图片 findings 可定位 bbox，视频 findings 可定位时间段/镜头；不可用维度明确降级；选中 findings 只创建新修正 revision，不覆盖旧结果。
  - Verify：`test_reproduction_assessments.py`、图片热力图像素测试、视频差异时间线测试、修正后局部/逐镜重生成 E2E。
  - Files：`backend/app/services/reproduction_assessment.py`、`backend/app/routers/reproduction_assessments.py`、`frontend/app/studio/StudioReproductionAssessment.jsx`、`frontend/app/studio/reproductionAssessment.ts`。

### WP08 提示词优化与模型编译

- [ ] `WP08-01` 完成忠实、精简、扩写、商业、电影化、约束、翻译和模型适配方向。
  - Acceptance：优化以建议形式呈现；支持字段接受/拒绝/撤销；上下文过期时禁止误应用。
  - Verify：`test_studio_prompt_optimization.py`、优化 reducer/context 前端测试。
  - Files：`backend/app/services/prompt_optimization.py`、`frontend/app/studio/promptOptimization.ts`、`frontend/app/studio/StudioPromptWorkspace.jsx`。
- [ ] `WP08-02` 完成图片/视频模型编译预览和兼容警告。
  - Acceptance：编译结果与分析稿分离；字幕/旁白/音效不进入纯视觉提示词；不兼容参数可定位。
  - Verify：模型切换只编译测试、视频编译器测试、UI 警告测试。
  - Files：`backend/app/services/gateway_prompting.py`、`backend/app/services/video_prompt_compiler.py`、`frontend/app/studio/StudioPromptWorkspace.jsx`。

### WP09 Recipe 与灵感内容

- [ ] `WP09-01` 完成 Recipe 精确回填、版本、克隆、收藏和派生归因。
  - Acceptance：回填保留素材角色、模型/能力版本、参数、证据和血缘；缺失私有素材有明确提示。
  - Verify：`test_recipes.py`、`creation-recipe-transfer.test.mjs`。
  - Files：`backend/app/routers/recipes.py`、`frontend/lib/creationRecipeTransfer.ts`、`frontend/app/prompts/CreationRecipeBrowser.jsx`。
- [ ] `WP09-02` 完成公开审核、分享 URL 和私有素材脱敏。
  - Acceptance：未审核内容不能公开；撤销分享立即失效；外部响应不泄漏私有素材引用。
  - Verify：分享/审核后端测试、分享页面 E2E。
  - Files：`backend/app/routers/recipes.py`、`backend/app/routers/admin_recipes.py`、`frontend/app/recipes/shared/[slug]/page.jsx`。

### WP10 项目与统一素材

- [ ] `WP10-01` 完成统一素材视图、元数据、标签、去重和相似素材。
  - Acceptance：上传与生成素材使用统一 `asset_ref`；所有操作保留来源审计。
  - Verify：素材所有权/相似测试、`unified-assets.test.mjs`。
  - Files：`backend/app/services/user_assets.py`、`frontend/components/AssetPickerDialog.jsx`。
- [ ] `WP10-02` 完成项目/文件夹、自动归档、云草稿、项目成本和导出包。
  - Acceptance：解除关联不删除真实素材；导出清单和媒体一致；项目成本来自服务端账务。
  - Verify：`test_projects.py`、项目治理前端测试、项目导出 E2E。
  - Files：`backend/app/services/project_collection.py`、`backend/app/routers/projects.py`、`frontend/app/projects/page.jsx`。

### WP11 任务、历史与账务

- [x] `WP11-01` 完成统一任务中心、断线恢复、失败建议和兼容模型重试。
  - Acceptance：生成/反推/解析/工作流任务统一呈现；重试生成新的链路节点而不篡改原任务。
  - Verify：`test_task_center.py`、`test_generation_retry.py`、统一任务前端测试。
  - Files：`backend/app/services/task_center.py`、`frontend/hooks/useUnifiedTaskCenter.js`、`frontend/components/GlobalTaskCenter.jsx`。
  - Evidence：`test_task_center.py + test_generation_retry.py + test_model_routes.py` 共 73 个测试通过，覆盖脏旧参数、结构化失败建议、能力/路由/报价候选筛选、同模型和切换模型的新任务/新报价/新 revision、不变原任务及幂等冲突；聚焦 Ruff、`unified-task-center.test.mjs`、TypeScript 和 `git diff --check` 通过。真实本地 `mock_mode=false` 后端与 Next.js 页面已验证任务实时连接、失败建议、兼容模型选择、预估积分、原任务不变说明、服务端价格复核和任务详情；`1440x900`、`390x844` 截图见 `output/playwright/wp11-compatible-retry-*.png` 与 `wp11-task-detail-mobile.png`，临时验收数据已清理。
- [x] `WP11-02` 完成报价、冻结、结算、退款、订单和争议明细。
  - Acceptance：账务守恒；每笔流水可追溯业务实体；异常任务可人工处理并审计。
  - Verify：`test_billing.py`、`test_generation_quotes.py`、账户账单前端测试。
  - Files：`backend/app/services/billing.py`、`frontend/app/recharge/CreditLedger.jsx`、`frontend/app/recharge/PaymentOrdersPanel.jsx`。
  - Evidence：`test_billing.py + test_generation_quotes.py` 共 45 个测试通过，覆盖业务聚合、游标/筛选、真实优化提案与 quote 关联、结算差额/冻结/同步扣费退回、余额与冻结守恒、`needs_review` 以及人工退款/结算审计备注；聚焦 Ruff、`account-billing.test.mjs`、TypeScript 和 `git diff --check` 通过。真实本地 `mock_mode=false` 服务已在 `1440x900` 和 `390x844` 验证业务实体、quote/价格版本、退款拆分、守恒提示、人工处理备注和任务深链，控制台无新增错误；截图见 `output/playwright/wp11-billing-desktop-1440x900.png`、`wp11-billing-mobile-390x844.png` 与 `wp11-billing-task-detail-mobile-390x844.png`，临时验收账号、任务、quote、提案、流水和审计记录已清理。

### WP12 管理与质量运营

- [x] `WP12-01` 完成目录、工具、路由和 Recipe 的权限化 CRUD、发布与回滚。
  - Acceptance：普通用户不可调用管理接口；所有变更有版本和审计记录。
  - Verify：管理权限测试、版本回滚测试、后台浏览器 E2E。
  - Files：`backend/app/routers/admin_catalog.py`、`backend/app/routers/admin_models.py`、`backend/app/routers/admin_recipes.py`。
  - Evidence：管理权限、目录版本、工具工作流、模型路由、Recipe 和迁移聚焦套件共 `189` 项测试通过；Ruff、compileall、前端目录契约 `7/7`、TypeScript 和 `git diff --check` 通过。真实本地 `mock_mode=false` 服务已验证模型/工具元数据历史、回滚确认、移动端布局，以及普通用户直达管理 URL 被送回 Studio 且导航不暴露管理入口；控制台无新错误，截图见 `output/playwright/wp12-catalog-*-history-*.png`与 `wp12-admin-permission-denied-mobile.png`，临时验收数据已清理。
- [x] `WP12-02` 完成质量、成本和转化报表及 CSV。
  - Acceptance：可按媒体/模型/目标查看成功率、证据覆盖、采用率、编辑幅度、生成转化、成本和毛利。
  - Verify：报表口径测试、CSV 测试、管理端前端测试。
  - Files：`backend/app/routers/admin_usage.py`、`frontend/app/admin/components/reports-audit.jsx`。
  - Evidence：`backend/tests/test_usage.py` 共 39 项通过，覆盖图片/视频证据覆盖率、采用/编辑/生成/Recipe 转化、供应商成本 `complete/partial/unavailable` 语义、毛利和 CSV；Ruff 通过。管理报表前端测试共 9 项通过，覆盖乱序请求、完整筛选快照、CSV 查询与成本空值；TypeScript、11 路由生产构建和 `git diff --check` 通过。真实本地 `mock_mode=false` 管理页面验证未筛选 45 项、`media_type=image` 38 项、叠加 `model=gpt-5.6-sol` 2 项、再叠加 `focus=poster_layout` 1 项，CSV 只包含该筛选任务；成本不可用时毛利/毛利率保持空值。桌面无页面级横向溢出；`390x844` 视口文档宽度与客户端均为 380px，质量表格在 306px 容器内可横向滚动到 1504px 内容末端，控制台无 error/warning；截图见 `output/playwright/wp12-reverse-report-filtered-*.png`。

### WP13 存储、安全与生命周期

- [ ] `WP13-01` 完成对象存储迁移、签名访问、本地物化缓存和对象校验。
  - Acceptance：私有媒体不暴露永久 URL；迁移可重入；缓存不绕过 owner gate。
  - Verify：`test_object_storage.py`、迁移脚本 dry-run/execute/verify。
  - Files：`backend/app/services/storage.py`、`backend/scripts/migrate_media_storage.py`、`backend/scripts/media_snapshot.py`。
- [ ] `WP13-02` 完成 SSRF、内容审核、保留清理、备份和恢复演练。
  - Acceptance：外部 URL 受网络边界限制；过期结果明确；恢复后关键关系和对象可用。
  - Verify：安全/保留/备份测试、恢复脚本演练。
  - Files：`backend/app/services/retention.py`、`backend/scripts/backup_db.sh`、`backend/scripts/verify_restore.sh`。

### WP14 全量发布验收

- [ ] `WP14-01` 完成数据库迁移升降级与模型漂移检查。
  - Acceptance：空库和历史库均可升级到 head；关键迁移可降级；`alembic check` 无漂移。
  - Verify：`test_migrations.py`、PostgreSQL upgrade/downgrade、`alembic check`。
- [x] `WP14-02` 完成全量后端、前端、类型和生产构建门禁。
  - Acceptance：pytest、ruff、compileall、unit、typecheck、build 全部通过。
  - Verify：`make release-check-worktree` 中对应步骤。
- [ ] `WP14-03` 完成真实浏览器与真实媒体验收。
  - Acceptance：桌面/移动端无重叠；图片证据画框与蒙版像素正确；中文 OCR、视频音频、逐镜生成和导出可运行。
  - Verify：Playwright 截图、canvas 像素检查、真实图片/MP4/音频冒烟记录。

## 6. 实施依赖

1. 先完成 WP07 的正式血缘和原子关联，其他生成入口不得绕过。
2. 并行完成 WP03 生产工作流适配器与 WP05 图片证据加固。
3. 在证据契约稳定后完成 WP06 视频语义、逐镜生成和合成导出。
4. 以真实工作流输出闭环验证 WP01、WP09、WP10、WP11 和 WP12，而不是只验证静态页面。
5. 最后执行 WP13 恢复演练和 WP14 全量发布验收，并逐项回填直接证据。

## 7. 完成定义

只有同时满足下列条件，工作包才可勾选完成：

1. 用户可见入口和后端真实能力一致，没有占位按钮或伪分析结果。
2. 数据库迁移覆盖新增持久状态，升级、降级和历史兼容经过验证。
3. 聚焦测试覆盖成功、失败、取消、重试、越权和幂等分支。
4. 全量后端/前端测试、类型检查和生产构建通过。
5. 对图片、视频或浏览器交互有要求的功能完成真实媒体/E2E 验收。
6. 文档状态由直接证据更新，不能以代码搜索结果或测试名称代替运行证明。
