# 发布安全与密钥轮换 Runbook

本文件用于上线前和发布打包前执行。代码可以证明“交付物不夹带本地敏感/运行时文件”，但不能证明真实密钥已经在供应商侧轮换，也不能替代 Git 历史清理后的远端强推与缓存失效。

## 1. 本地开发目录与 release 包隔离

以下目录/文件可以存在于开发机，但不得进入 release 包、Docker build context 外部交付物或 Git:

- `backend/.env`
- `frontend/.env.local`
- `backend/storage`
- `backend/.venv`
- `frontend/node_modules`
- `frontend/.next`
- `.git`
- `.codex`, `.agents`
- `*.db`, `*.sqlite*`, `*.log`, `*.pem`, `*.key`, `*.p12`, `*.pfx`

已落地的代码门禁:

- `.gitignore` 排除本地密钥、缓存、依赖、运行时数据。
- `.dockerignore` 排除本地密钥、缓存、依赖、运行时数据。
- `scripts/check_release_artifact.py` 会拒绝 `.env`、`.git`、`.codex/.agents`、`.venv`、`node_modules`、`.next`、`storage`、数据库、日志、证书私钥和超大文件进入交付物。
- CI 的 release artifact job 会对 `git archive` 产物执行污染检查。

发布前执行:

```bash
make release-check-worktree
```

构建正式源码包:

```bash
make release-source
python3 scripts/check_release_artifact.py dist/ai-studio-source.tar.gz
```

如果是手工打包或外部系统打包，必须先对候选包再跑一遍:

```bash
python3 scripts/check_release_artifact.py /path/to/release.tar.gz
python3 scripts/check_release_artifact.py /path/to/unpacked-release
```

## 2. 真实密钥轮换

一旦 `.env`、聊天记录、截图、日志或 Git 历史中出现真实密钥，必须先在供应商侧轮换。删除代码里的字符串不等于密钥失效。

建议轮换顺序:

1. 盘点泄露范围: `backend/.env`、`frontend/.env.local`、CI secrets、部署平台 secrets、历史日志、Git 历史、运维截图。
2. 在对应供应商控制台创建新密钥:
   - 模型网关: 图像、视觉、视频 provider API key。
   - 火山/Ark 或其他视频模型网关 API key。
   - 支付: 支付宝应用私钥/公钥配置、微信支付商户 APIv3 key、商户私钥、平台证书。
   - 短信: HTTP 短信网关 key/secret。
   - 应用密钥: `JWT_SECRET`、`METRICS_TOKEN`、`PAYMENT_CONFIG_SECRET`、`MODEL_CONFIG_SECRET`。
3. 更新部署环境变量或 Secret Manager，不要把新密钥写入仓库。
4. 重启 API、worker、beat、frontend，使新配置生效。
5. 在管理后台验证:
   - 模型配置 provider 测试通过，并能列出/选择模型。
   - 支付配置回调验签通过，测试订单能正确入账。
   - 短信配置测试发送成功。
6. 撤销旧密钥，确认旧密钥调用失败。
7. 记录轮换时间、执行人、影响范围、验证截图或日志摘要。

注意:

- 如果更换 `PAYMENT_CONFIG_SECRET` 或 `MODEL_CONFIG_SECRET`，数据库里已加密保存的支付/模型密钥可能无法解密，需要管理员在后台重新录入对应配置。
- `JWT_SECRET` 轮换会使旧登录态失效，这是预期行为。
- 支付渠道密钥轮换后，要同步更新支付平台回调域名和证书序列号记录。

## 3. Git 历史清理

Git 历史清理必须由仓库 owner 在允许强推的环境执行。本地代码改动无法证明远端历史、fork、缓存和 tag 都已清理。

执行前:

```bash
git clone --mirror <repo-url> repo-backup.git
gitleaks detect --source . --redact --log-opts="--all"
```

使用 `git filter-repo` 清理敏感路径示例:

```bash
git filter-repo \
  --path backend/.env \
  --path frontend/.env.local \
  --path backend/storage \
  --path backend/.venv \
  --path frontend/node_modules \
  --invert-paths
```

如果是清理历史中某个已泄露的字符串，优先使用供应商侧轮换；历史重写只作为降低二次传播风险的补救。需要字符串替换时，使用 `git filter-repo --replace-text` 或 BFG，并在替换规则中只写已失效的旧密钥。

清理后本地压缩:

```bash
git reflog expire --expire=now --all
git gc --prune=now --aggressive
gitleaks detect --source . --redact --log-opts="--all"
```

推送前确认:

```bash
make release-check-worktree
git log --all -- backend/.env frontend/.env.local backend/storage backend/.venv frontend/node_modules
```

远端同步:

```bash
git push --force-with-lease --all
git push --force-with-lease --tags
```

清理后还要处理:

- 通知协作者重新 clone 或按团队规范 rebase。
- 删除/失效旧 release 包、CI artifact、部署缓存。
- 检查 GitHub forks、PR refs、issue/comment 附件中是否还有泄露。
- 再次确认供应商侧旧密钥已撤销。

## 4. 上线前最小验收

```bash
make compile
make lint
make test
make test-frontend
make release-check-worktree
```

如需完整生产构建:

```bash
cd frontend && npm run build
cd ..
make release-source
```

验收通过的定义:

- 测试和 lint 通过。
- 候选 release 包通过 `scripts/check_release_artifact.py`。
- `gitleaks detect --source . --redact --log-opts="--all"` 无未处理真实密钥。
- 真实密钥已在供应商侧轮换，旧密钥已撤销。
- 生产环境变量只存在于 Secret Manager、部署平台或私有 `.env`，不进入仓库和 release 包。
