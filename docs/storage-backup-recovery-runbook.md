# 存储备份与恢复 Runbook

## 1. 保护目标

完整备份包同时包含 PostgreSQL 快照和媒体快照。媒体快照保留稳定对象 key，
支持 `local` 与 S3/MinIO，并为每个对象记录容量和 SHA-256。数据库和媒体快照是顺序生成的，
因此必须在 API、Worker 和 Beat 停止写入的维护窗口执行。

备份完成的判定标准：

- `database.sql.gz` 可通过 gzip 校验。
- `media.tar.gz` 的成员、容量和逐对象 SHA-256 与 `manifest.json` 一致。
- 两个文件的整包 SHA-256 记录在 `SHA256SUMS`。
- 备份目录在所有校验通过后原子发布，失败的临时目录不对外暴露。

`SHA256SUMS` 用于发现意外损坏，不是防篡改签名。生产备份还必须写入独立账号、
开启不可变保留或版本控制，并在平台外保存签名或审计记录。

## 2. 生成备份

```bash
cd backend

# 必须在已停止写入的维护窗口执行
BACKUP_DIR=/var/backups/ai-studio \
BACKUP_RETENTION_DAYS=14 \
./scripts/backup_db.sh
```

`STORAGE_BACKEND=local` 时读取 `STORAGE_DIR`，可以用 `MEDIA_DIR` 显式覆盖。
`STORAGE_BACKEND=s3` 时直接遍历配置 bucket，不使用 `.object-cache` 或本地镜像充当完整备份。

可单独生成或校验媒体快照：

```bash
.venv/bin/python -m scripts.media_snapshot create --output /tmp/media.tar.gz
.venv/bin/python -m scripts.media_snapshot verify --archive /tmp/media.tar.gz
```

## 3. 非生产恢复演练

至少每月一次，使用隔离的 PostgreSQL 管理库连接执行：

```bash
cd backend
export TARGET_DATABASE_URL='postgresql://restore_user@127.0.0.1:5432/postgres'
export PGPASSWORD='<temporary-restore-password>'
export RESTORE_VERIFY_NON_PRODUCTION=1
./scripts/verify_restore.sh --bundle /var/backups/ai-studio/ai_studio_YYYYMMDD_HHMMSS
```

脚本会校验整包摘要、逐对象摘要和媒体包成员安全性，然后创建随机临时数据库、
恢复 SQL 并执行关键表/字段探测，最后自动删除临时数据库。它会拒绝 URL 中含密码、
主机名明确含 `prod/production` 或缺少 `RESTORE_VERIFY_NON_PRODUCTION=1` 的运行。

## 4. 媒体恢复

首先只演练，不写入当前存储：

```bash
cd backend
.venv/bin/python -m scripts.media_snapshot restore \
  --archive /path/to/media.tar.gz
```

确认当前环境的 `STORAGE_BACKEND`、`STORAGE_DIR` 或 S3/MinIO bucket 是恢复目标，
且 API、Worker 和 Beat 已停止后，才执行：

```bash
.venv/bin/python -m scripts.media_snapshot restore \
  --archive /path/to/media.tar.gz \
  --execute
```

已存在且 SHA-256 相同的对象会跳过。已存在但内容不同的对象会使恢复失败；
只有确认备份是权威副本时，才允许加 `--overwrite`。恢复完成后工具会再计算目标对象 SHA-256。

## 5. 切换、回滚与验收

1. 停止平台写入，记录备份包、数据库版本和目标 bucket/目录。
2. 完成数据库恢复和媒体恢复，运行 `media_snapshot verify`。
3. 启动 API/Worker/Beat，使用两个隔离账号验证历史上传、预览、原图下载和视频播放。
4. 确认跨账号访问返回 `404`，过期播放凭证返回 `401`，签名地址超时后不可用。
5. 对恢复对象数、总容量和抽样 SHA-256 做记录，保留演练日期、执行人和结果。
6. 任一核心探测失败时停止写入，切回恢复前的数据库和存储目标，不得混用两套数据。
