# WeiLink · 微信里的 Codex

离开电脑，也能通过微信继续本机的 Codex 工作。

向 Bot 发消息、看截图、问任务进度，或把新要求发回正在进行的桌面会话。WeiLink 桥接器运行在你的电脑上，只响应已绑定主人的消息；使用时电脑和桥接进程需要保持运行。

## 能做什么

- **在微信里工作**：用自然语言提出需求，接收 Codex 的最终答复；支持创建和切换多个微信会话。
- **继续桌面任务**：查找本机 Codex 任务、查看进展，并向原桌面会话补充要求。
- **发送图片和视频**：结合截图、引用上下文或视频抽样画面提问，也能接收 Codex 生成的图片。
- **使用语音交流**：配置语音环境后，可识别语音消息，并以 MP3 附件回复。
- **切换聊天人设**：内置大肥鱼和爱弥斯，支持按语境发送对应表情，也可关闭人设。

当前管理脚本面向 **Windows**，主要用于主人的一对一 Bot 会话。

## 快速开始

### 1. 安装

准备 Python 3.12、PowerShell 7、Git，以及已完成登录和模型配置、能正常使用的 Codex CLI。先确认 `python --version` 显示为 `3.12.x`；安装了多个版本时，也可用 `py -3.12` 替代下方的 `python`。

```powershell
git clone https://github.com/xxxifan/weilink.git
cd weilink
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

以下命令均在仓库根目录执行。

### 2. 绑定微信

首次使用时，运行下面的命令，按提示扫码登录。记录登录结果中的 **Bot ID**。

```powershell
$weiData = Join-Path (Get-Location).Path 'data'
.\.venv\Scripts\weilink.exe login --base-path $weiData
```

接着运行接收命令，并在等待期间用本人的微信给 Bot 发一条便于辨认的文字消息：

```powershell
.\.venv\Scripts\weilink.exe recv --timeout 30 --base-path $weiData --json
```

在本人发出的那条消息中找到 `from_user`，用作后续配置的 **`owner_user_id`**。它是发送者标识，不是微信昵称。超时未收到消息时，可重新运行接收命令。

已有 WeiLink 授权数据时，可复用原目录；登录命令中的 `--base-path` 和下一步的 `data_path` 必须指向同一目录。同一份授权数据同时只运行一个接收端，启动桥接器前先结束上述接收命令。

### 3. 配置

创建本机配置文件；已有配置时保留原文件：

```powershell
if (-not (Test-Path .\bridge-config.json)) {
    Copy-Item .\bridge-config.example.json .\bridge-config.json
}
```

编辑 `bridge-config.json`，至少填写以下五项：

| 配置项 | 填写内容 |
| --- | --- |
| `owner_user_id` | 上一步本人消息中的 `from_user`，用于限定谁能操作 Bot |
| `bot_id` | 扫码登录结果中的 Bot ID |
| `data_path` | WeiLink 授权数据目录的绝对路径，例如 `C:/path/to/weilink/data` |
| `workspace` | 已存在的本机工作目录，供微信会话执行任务和保存文件 |
| `codex_command` | 启动命令数组，默认 `["codex", "app-server"]`；找不到 `codex` 时改用可执行文件的完整路径 |

路径推荐使用 JSON 中的正斜杠。其余选项可按需调整：

| 配置项 | 默认值与用途 |
| --- | --- |
| `model`、`reasoning_effort` | 留空时沿用本机 Codex 设置 |
| `turn_timeout_seconds` | `3600`，单轮请求等待时限，单位为秒 |
| `default_persona` | `deepseek`（大肥鱼）；可选 `aemeath`（爱弥斯）或 `off` |
| `animated_meme_mode` | `file`，将动态表情作为原始文件附件发送 |

完整示例见 [bridge-config.example.json](bridge-config.example.json)。

### 4. 启动并发起对话

```powershell
pwsh -File .\bridge\start.ps1
pwsh -File .\bridge\status.ps1
```

状态中的 `Running` 和 `CodexConnected` 均为 `True` 后，向 Bot 发送一条新消息即可开始。用于确认身份的那条旧消息不会作为新任务执行。

在微信里发送下面的命令可查看帮助：

```text
/help
```

停止桥接器：

```powershell
pwsh -File .\bridge\stop.ps1
```

停止请求会等待当前长轮询结束，最长约 45 秒。

## 微信端用法

日常使用直接发送需求即可；不同话题可以用独立会话保存上下文。查看桌面任务后，使用列表给出的任务编号向原会话补充要求。

| 命令 | 用途 |
| --- | --- |
| `/new [名称]` | 开启新的微信会话 |
| `/sessions`、`/use 编号` | 列出或切换微信会话 |
| `/status` | 查看连接状态和当前上下文用量 |
| `/tasks [关键词]` | 列出或搜索桌面任务 |
| `/task t编号` | 查看指定桌面任务的进展 |
| `/reply t编号 内容` | 向原桌面会话发送新的要求 |
| `/replies` | 查看桌面消息的投递状态 |
| `/stop` | 请求停止当前微信任务 |
| `/persona 大肥鱼`、`/persona 爱弥斯`、`/persona off` | 切换或关闭人设 |
| `/meme [分类]` | 发送当前人设的表情，默认分类为 `happy` |
| `/help` | 查看完整命令，包括操作审批和问题回答 |

方括号表示可选参数。收到操作确认或待回答问题时，按 Bot 给出的编号和指令回复。更多人设与表情用法见 [personas/README.md](personas/README.md)。

### 连续发送消息

同一微信会话连续发送的消息，以本批第一条回复开始发送为边界合并处理，不使用固定的几秒时间窗口。排队消息一起交给模型；处理中收到的文字、图片、语音或视频会追加到当前任务，媒体下载或语音转写完成前暂缓旧答复。

例如先发“这是”、再发图片，只要图片消息在回复开始发送前到达，Bot 就会结合两条消息回复。模型恰好已完成时，会在原会话中继续处理补充消息，并替换尚未发送的文字和媒体草稿。开始发送后的新消息进入下一批；不同会话分别处理，斜杠命令和明确的问题回答仍按原规则执行。

## 可选功能

### 视频

在 PATH 中安装可用的 **FFmpeg 和 FFprobe** 后，可直接给 Bot 发送 **3 分钟、100 MB 以内**的视频。桥接器抽取最多 12 张带时间点的画面；语音环境可用时，还会尝试转写音轨。

视频分析依据抽样画面和可用转写，不能覆盖每一帧的连续动作。没有音轨或转写失败时，仍可分析画面。

### 语音

语音识别、语音回复和视频音轨转写需要额外准备 **edge-voice 技能与独立 TTS 环境**，安装本仓库的 Python 依赖并不会自动配置这些功能。

语音环境默认使用仓库同级 `tts/` 下的 `.venv/Scripts/python.exe`；识别脚本默认位于当前用户的 `~/.codex/skills/edge-voice/scripts/speech.py`，也会遵循 `CODEX_HOME`。使用其他安装位置时，可在启动桥接器前设置环境变量，无需修改源码：

```powershell
$env:WEILINK_VOICE_RUNTIME = 'C:/path/to/tts'
$env:WEILINK_VOICE_SCRIPT = 'C:/path/to/edge-voice/scripts/speech.py'
```

语音回复以 MP3 文件附件发送；生成失败时回退文字。包含代码或链接的回复还会保留可复制的文字内容。

### 桌面任务

查看桌面任务使用本机 Codex 数据目录，默认是 `~/.codex`，可通过 `CODEX_HOME` 指定。向原会话发送消息还需要 **Codex 桌面应用正在运行**，且其 `codex-app-tools` MCP 连接可用。

目标会话忙碌时，消息会等待其空闲。`/replies` 中的“已投递”表示消息已交给桌面会话，任务处理完成后才会回传最终答复。此功能依赖 Codex 的本机存储和桌面工具接口，版本变化可能需要适配。

## 常见问题

- **显示已启动，但 Bot 没有回复？** 先运行状态脚本确认 `Running` 和 `CodexConnected`，再检查主人 ID、授权数据目录，以及是否有其他接收端占用。启动失败时查看根目录的 `bridge.log` 和 `bridge-stderr.log`。
- **如何恢复过期的发送会话？** 先由主人向 Bot 发送一条新消息。网络结果不明的发送不会自动重发，可查看状态后再决定是否重试。
- **为什么动图显示为文件？** 默认保留原始动画文件，通过附件发送；不保证在聊天气泡中自动播放。

## 数据保存

仓库的 `.gitignore` 已排除默认的 `data/` 授权与聊天数据目录、本机配置、状态数据库、日志和媒体缓存。使用自定义 `data_path` 时，请在其所属 Git 仓库中排除该目录。克隆仓库后需要自行完成绑定与配置。

接收的图片、语音和视频分别保存在 `workspace` 下的 `.wechat-images/`、`.wechat-voices/` 和 `.wechat-videos/`。如果该工作目录属于另一个 Git 仓库，请在对应仓库中排除这些目录。

## 开发

运行现有测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s bridge/tests
```

视频解码测试需要 FFmpeg 和 FFprobe，缺少时会跳过。

## 致谢

本项目基于 WeiLink 接入微信 Bot。人设提示词和表情素材的上游仓库、固定版本记录见 [personas/catalog.json](personas/catalog.json)，素材权利归原作者。
