# WeiLink 微信桥接器

通过 WeiLink 接收主人一对一微信消息，并交给本机 Codex App Server 处理。支持独立微信会话、桌面任务查看与消息转交、持久化发送队列、图片、视频、引用上下文、人设表情和语音回复。

## 本机准备

- Windows、PowerShell 7、Python 3.12 和可用的 Codex CLI 登录环境。
- 使用 WeiLink 完成 Bot 登录，保留本机授权数据；`data_path` 必须指向该数据目录。
- 同一份 WeiLink 授权数据只运行一个接收端，避免与其他轮询进程争抢消息。
- 视频处理需要 PATH 中可用的 FFmpeg 和 FFprobe。

在仓库根目录创建环境并安装锁定依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
Copy-Item bridge-config.example.json bridge-config.json
```

编辑 `bridge-config.json`，填写已授权的 `owner_user_id`、`bot_id`、数据目录和微信会话工作目录。`codex_command` 是启动 App Server 的命令数组；如果 Codex 不在 PATH 中，改为实际可执行文件路径。`model` 和 `reasoning_effort` 留空时沿用本机设置。

当前语音处理依赖本机 edge-voice 技能及独立 TTS 环境。`bridge/voice.py` 的 `RUNTIME`、`SCRIPT` 和 `bridge/wechat_bridge.py` 的语音技能提示路径包含原机器路径，迁移机器时需要调整。当前语音回复采用 MP3 文件附件，并保留文字；微信原生语音气泡送达尚未确认。

本机已验证的识别组合为 `faster-whisper==1.2.1` 与 `av==16.0.1`。前者调用 `av.open` 时传入 `metadata_errors`，本机原有的 `av==19.0.2` 不接受该参数；独立 TTS 环境的依赖锁定文件位于仓库外，迁移或重建时需保留兼容版本。

直接发送的视频支持 3 分钟、100 MB 以内；桥接器下载后按时间抽取最多 12 张画面，并尝试在本机转写音轨。模型接收带时间点的实际画面和可用转写，无法据此确认每一帧连续动作。没有音轨或识别失败时仍可分析画面，并明确音频读取状态。视频原件、画面及转写缓存在微信工作目录下的 `.wechat-videos/`，不进入版本控制。

桌面任务功能读取本机 Codex 数据目录（默认 `~/.codex`，可通过 `CODEX_HOME` 指定），依赖其私有 SQLite 和会话记录格式。桌面任务忙碌时，转交消息会等待其空闲。

## 运行

```powershell
pwsh -File .\bridge\start.ps1
pwsh -File .\bridge\status.ps1
pwsh -File .\bridge\stop.ps1
```

停止请求会等待当前长轮询结束，最长约 45 秒。微信发送 `/help` 可查看可用命令；人设和表情说明见 [personas/README.md](personas/README.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s bridge/tests
```

现有自动化测试覆盖桥接状态、发送队列、桌面转交、图片与引用、视频抽帧及转写、人设和语音等逻辑。视频解码测试需要 FFmpeg 和 FFprobe，缺少时会跳过。实际微信送达、语音服务及跨机器运行仍需对应环境验证。

## 仓库内容

`bridge/` 保存桥接代码、管理脚本和测试，`requirements-lock.txt` 保存 Python 依赖快照。`personas/` 保存人设目录、审核名单、已解压的提示词和表情素材；上游仓库及固定提交记录见 `personas/catalog.json`，素材权利归原作者。

真实配置、登录凭据、聊天数据库、日志、生成媒体、虚拟环境、历史备份和重复 ZIP 下载包通过 `.gitignore` 排除。克隆仓库不会恢复原机器登录态或聊天记录。
