# 多台电脑分发与 NAS 使用（内网加速版）

2026-11-15 更新：本版自动同步 NAS 已上传进度，完成当前默认 10 次目标后可提高次数；采集结束保存后自动上传并校验。所有换机采集电脑应使用同一更新版，旧版不会遵守 NAS 被试占用保护。详情与升级注意事项见 [跨电脑续采](cross-pc-progress.md)。

内网加速版分发仓库：`https://github.com/DiDicpR/cfimage-brainco-acquisition-lan`。使用独立目录 `D:\Paradigm-update\cfimage-brainco-acquisition-lan`，保留首版目录。新目录不会自动继承旧目录中的被试数据；已有采集需要续采时，应先迁移完整数据和本机配置，再检查剩余进度。

## 操作顺序

1. 分发工具 `NAS-Dispatch-Client-20261020-v2/Start-Dispatch.bat` 输入本项目 GitHub 仓库 URL。该工具使用预先授权的 SSH 网关；不要将工具包里的 private、client.ppk 放进 GitHub。
2. 在实验电脑双击 **启动GUI.bat / RUN_GUI.bat**。脚本从局域网 NAS 下载经过 SHA-256 固定的运行包，包含 Python 3.12、uv 和必需 wheels，然后由 uv 离线安装到项目 `.venv`。无需预装 Python/uv，不从公网逐个下载依赖；后续复用本地缓存。见 [环境加速与旧版升级](OFFLINE_RUNTIME.md)。
3. 自动从 NAS 的 S3 图片库匿名读取，不需要输入 SMB 账号密码，也不依赖本机 AWS profile。
4. 自动下载并校验 2580 张全场景图片，最多 4 路并行，逐个检查大小和 SHA-256；中断后重试会跳过已核验文件，坏文件重新下载。完成后自动关闭下载步骤并打开采集工作台，失败则保留错误说明。所有图片必须在正式采集前下载到本地。
5. 打开采集工作台，为每台机器填写本次真实被试编号，然后连通性测试、采集。发布版不预填 s001，避免 10 台机器误用相同身份。硬件、网络、显示器和实际标记时序需逐机验证。
6. 本轮停止并保存后，选择已有被试，点击“上传当前被试全部数据”；也可用独立的 `上传被试数据.bat`。带授权分发版本自动导入 Windows 加密凭据并连接 NAS，启动器可“检查公共目录上传”。上传期间不要启动采集；程序会占用与采集相同的进程锁。详见 `public-upload.md`。

## NAS 路径与身份

本项目原范式身份 `BCSCN-20261113` 保留不变。用户明确指定存储目录 `CFIMAGE-20261113`，所以传输层使用这个名称；它不是重新编排试次或改写历史记录。原模板 DVC remote 为另一可选流程，当前一键下载使用 download-index.json，勿将未发布的旧 DVC 指针当作已上传素材。

默认图片读取 S3，数据写入 SMB；SMB 图片库仍保留作为可选后端：

- 图片：`\\172.16.4.2\aim\shared\CFIMAGE-20261113\assets\stimuli\...`
- 图片索引：同一 assets 目录下 download-index.json；GitHub 也保存一份已固定哈希的索引。
- 数据：`\\172.16.4.2\aim\shared\CFIMAGE-20261113\eeg-data\sub-<编号>\<机器UUID>\...`

S3 目标同样已配置，但 SMB 文件不会自动变成 S3 对象。管理员提供 S3 写入权限后，在 **config/transfer.local.toml** 中写入覆盖配置（被 Git 忽略）：

```toml
backend = "s3"
profile = "cfimage-upload"
```

本机 AWS profile 保存 AK/SK，程序不把密钥写入项目。对应目标为 `s3://oi-task-assets/CFIMAGE-20261113/` 和 `s3://oi-data/CFIMAGE-20261113/`；素材下载使用匿名读取，数据上传/读回必须经过授权。切换到 S3 后需要先执行素材发布命令，不能假设 SMB 上传结果已在那里。

## 数据上传语义

上传范围为当前被试的全部 native 会话目录、其 handoff 副本（若存在），以及只含此被试记录的新建 progress.sqlite；不会把其他被试的行或 SQLite 已删除页混入备份。SQLite 用 backup API 保持一致性，临时快照需要额外磁盘空间。running 状态会阻止上传，须先完成保存或处理异常会话。

每台机器自动生成独立 UUID，保存在 .local/machine-id，禁止复制这个文件到其他实验机。数据按内容哈希存放在 objects/，最终 snapshots/<哈希>.json 描述路径、大小、SHA-256。没有最终清单表示上传未完成。每次写入会读回核验，不使用 multipart ETag 当 SHA-256。重复上传复用相同内容，更新后的本地文件生成新的内容对象和快照，不覆盖旧备份、不删除本地数据。

NAS 目录中的对象需配合清单恢复，不能仅凭文件名判断 EEG 格式。恢复命令：

```powershell
uv run --no-sync python -m src.nas_transfer restore --snapshot "sub-s001/<机器UUID>/snapshots/<清单哈希>.json" --output "D:\Recovered\s001"
```

目标必须为空；恢复也逐文件验证。上传成功仅表示备份完整，不等于脑电质量合格或 EEGQC 审核通过。中断/无效试次会原样保留，由清洗人员根据状态筛选。

## 命令行与发布

```powershell
uv run --no-sync python -m src.nas_transfer publish
uv run --no-sync python -m src.nas_transfer download
uv run --no-sync python -m src.nas_transfer upload --participant s001
uv run --no-sync python main.py --config config/brainco.toml --check-assets
```

publish 仅给维护者使用：先校验本地素材，再上传并读回验证，最后发布索引。公开仓库使用 scripts/prepare_public.py 的白名单生成，不包含图片、NAS 密码、SSH 密钥、真实采集数据、诊断报告、wheel 或虚拟环境。

依赖管理按 [uv 项目文档](https://docs.astral.sh/uv/guides/projects/)；S3 凭据采用 [Boto3 标准本机凭据链](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)。
