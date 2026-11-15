# 小米6 工具箱 · Web UI

Material 3 风格的网页界面，跑在本地，用 Edge 应用模式开窗（无地址栏）。

**自带 Python 3.11**（`python-embed/`）：整个文件夹拷到另一台 Windows 电脑上，双击 `启动.bat` 就能用，不用装 Python。

## 启动

双击 `启动.bat`，它会：

1. 优先用 `python-embed\pythonw.exe`（内置 Python，无控制台窗口）；没有才退回系统里的 pythonw / python
2. 在 `127.0.0.1:8765` 起本地服务
3. 用 Edge 的 `--app` 模式打开窗口（居中、无地址栏）

几个细节：

- **端口被占用**：如果是本程序上次没退干净的实例，直接复用那个窗口，不会再起第二个；被别的程序占用就自动往后找空端口（最多 +20）
- **启动失败会弹窗**告诉你原因（`pythonw` 没有控制台），同时写进 `logs\webui.log`
- 关掉窗口后服务自己退出；窗口最小化不会误杀（心跳容忍 90 秒）

手动起：`python-embed\python.exe webui\serve.py`

调试用环境变量：`set MI6_WEB_NO_OPEN=1`（只起服务不开窗）、`set MI6_WEB_PORT=8799`（换端口）

## 结构

```
Mi6Toolbox/
├─ 启动.bat
├─ python-embed/          ← 内置 Python 3.11（可移植，别删；删了会退回系统 Python）
├─ webui/
│  ├─ serve.py            ← 后端：静态文件 + JSON API + adb/fastboot + DeepSeek 代理
│  └─ static/index.html   ← 整个前端（单文件：HTML+CSS+JS）
├─ toolbox/
│  ├─ adb.py fastboot.py device.py util.py config.py
│  ├─ rom.py              ← 全量包解析 / 解包 / 整包刷入 / sideload
│  ├─ bootimg.py          ← boot/recovery img 解包 / ramdisk 编辑 / 重打包
│  └─ ai.py               ← DeepSeek function calling + DPAPI 加密存 Key
├─ tools/platform-tools/  ← adb / fastboot
├─ apk/                   ← 一键安装
├─ downloads/             ← ROM、截图、上传下载中转、img_work/
├─ backups/ data/ logs/ images/
```

## 功能

- **概览**：磁贴（电量/温度/存储/运行/面具/TWRP/分区/连接）
- **刷机**：12 个常见分区磁贴 + 自选全部分区；拖 `.img` 到磁贴 = `fastboot flash`，单击磁贴 = 提取该分区到 `backups/`
- **全量包**（刷机页下方）：把 `.tgz / .tar.gz / .zip` 放进 `downloads/`（或直接拖进页面）→「检查」看包里有哪些镜像 →「解包」→「整包刷入」按安全顺序逐个刷；卡刷包（LineageOS 等）识别为 sideload，走 `adb sideload`
- **IMG**：选一个 `boot.img / recovery.img` → 解包（header 解析 + ramdisk 展开）→ 浏览/编辑 ramdisk 里的文本文件 → 重打包成新 img → 直接「刷入分区」
- **文件**：浏览 `/sdcard`，点文件保存到电脑并在资源管理器选中；把文件拖进窗口上传
- **工具**：常用 adb 命令磁贴
- **ADB**：终端，进页面直接就能打字（不用先点输入框）；**Tab 补全**走后端，能补命令名、`adb` 子命令、手机上的属性名（`getprop`）、`dumpsys` 服务、`pm` 包名、手机路径（`ls /sdc` → `/sdcard/…`），连按 Tab 在候选里翻，后端没响应时退回本地命令表；↑↓ 翻历史，命令与报错分色（设置里可改）
- **顶部「关闭」**：停止本地服务并关掉窗口（手机不受影响）。直接关窗口也一样会退服务
- **AI**：只支持 DeepSeek。填 Key（DPAPI 加密存 `data/secrets.json`）→ 用中文说需求 → AI 调工具查状态/解包/刷机。回复是**流式**的，勾上「深度思考」会把思考过程（`reasoning_content`）实时展开给你看；危险操作默认先给你确认，勾上「允许 AI 直接执行」才全自动
- **设置**：主题（浅/深/跟随）、强调色 5 种、磁贴大小、动效开关、自动刷新、终端配色、危险操作确认

## 全量包说明

- **线刷包**：小米官方 `.tgz`（内含 `images/*.img`）。解包后按 `boot → recovery → system → vendor → cust → modem …` 顺序刷，单步失败就停，日志逐行显示
- **默认跳过** `misc / persist / modemst1 / modemst2 / fsg / cache`（这些不该无脑刷）；要刷得自己动手
- **卡刷包**：含 `META-INF/...` 或 `payload.bin` 的 zip → 只能进 TWRP 的 sideload 模式刷
- 解包会拒绝含 `..`、绝对路径、符号链接的包；整包刷入前会先确认手机在 Fastboot，免得解完几个 G 才报错
- 大包（几 GB）第一次扫描会慢一点，之后有 20 秒缓存

## IMG 说明

- 列表里会先标出每个 `.img` 到底是什么：🥾 **boot 镜像**（可解包）/ 💽 **ext4** / 💽 **FAT** / ⬜ **空分区**（整片 0）/ 🧩 **sparse** / 🗜️ **压缩镜像** / ❔ **非 boot**。分区 dump 不都是 boot 镜像——`persist`/`cust` 是 ext4、`modem` 是 FAT、`misc` 常常整片是 0，这些点进去会告诉你它是什么，而不是只丢一句"打不开"
- 支持 boot header **v0 / v1 / v2**（Mi6 实测 v0，page 4096），ramdisk 支持 gzip / xz / 未压缩
- 重打包会重算 cpio、按原方式压缩、重写 header 的 size/offset 和 v1/v2 的 SHA1
- 重打包出来的是紧凑镜像，可能比原始整分区 dump 小很多（实测 64 MB → 15 MB），刷入完全正常
- **不支持**：lz4 压缩的 ramdisk、header v3/v4（Android 13+ 动态分区）、稀疏镜像（要先 `simg2img`）
- ramdisk 里的符号链接不能直接编辑，界面会标 🔗 并提示它链到哪

## AI 说明

- 只支持 DeepSeek 官方接口 `https://api.deepseek.com`，模型默认 `deepseek-flash`（旧名 `deepseek-v4-flash`、`deepseek-chat` 会自动兜底重试）
- **深度思考**：对应 API 的 `thinking: {type: enabled}` + `reasoning_effort`，思维链通过 `reasoning_content` 流式返回，页面上是一块可折叠的「思考过程」，边想边往下滚
- **流式**：走 SSE（`/api/ai/stream`），思考、正文、每次工具调用和执行结果都是实时出现的；流断了会自动退回普通接口
- **随时能停**：生成中发送按钮会变成红色的 ■，点一下就停（同时 `POST /api/ai/stop`，服务端在下一个字/下一次工具调用前收手），停下来后可以直接接着发下一句，不会再卡住
- **不会被"重启断连"骗到**：模型知道小米6 一重启 ADB 就会断 10~60 秒。`reboot_device` 的返回里带 `hint`，`get_device_state` 会带 `recentAction`（最近是不是刚重启/刚刷机），还有一个 `wait_for_device` 工具专门等设备回来；只有等超时了它才会让你去检查数据线
- 带工具的请求会把每轮的 `reasoning_content` 原样回传（DeepSeek 要求，否则 400）
- Key 用 Windows DPAPI 加密后存 `data/secrets.json`，只有当前 Windows 账户能解；换电脑/换账户要重填
- 可用的工具：查设备状态、等设备回来、列分区、列本机包、看包内容、解包、刷单个 img、整包刷、sideload、提取分区、重启、装 apk、执行 adb 命令
- 危险工具（刷机 / 重启 / 装应用 / 提取分区 / adb 命令）默认只「提议」，你在对话里点「执行」才真正跑；想全自动就打开「⚡ 直接执行」
- 需要联网 + 有效 Key；没有 Key 时点右上角 🔑 填一个

## 从源码跑 / 提交

仓库里只放代码（`webui/`、`toolbox/`、`启动.bat`、README），大文件都没进仓库：

```
python-embed/        内置 Python（约 15 MB）
tools/platform-tools/ adb / fastboot
apk/  downloads/  backups/  data/  images/  logs/
```

新机器 clone 下来后，任选一种方式跑：

1. **装个系统 Python 3.8+**，直接 `python webui\serve.py`（`启动.bat` 找不到内置版会自动退回系统 Python）
2. 或者把 `python-embed\`（[python.org embeddable 包](https://www.python.org/downloads/windows/) 解压）和
   `tools\platform-tools\`（[platform-tools](https://developer.android.com/tools/releases/platform-tools) 解压）放回去，双击 `启动.bat`

Git 环境（本机已装好，路径 `G:\zuomian\tools\git`，已加进用户 PATH）：

```bat
cd /d G:\zuomian\Mi6Toolbox
git remote add origin https://github.com/<你的用户名>/Mi6Toolbox.git
git push -u origin main
```

第一次 push 会弹 GitHub 登录（Git Credential Manager，浏览器里授权即可）。
想让提交显示成你自己的名字：

```bat
git config user.name  "你的名字"
git config user.email "你的邮箱"
git commit --amend --reset-author --no-edit
```

## 已知问题 / 限制

- 只操作一台设备：同时插两台时取第一个
- TWRP 里不能用 `adb install` 装应用（没有包管理器）
- `启动.bat` 必须和 `python-embed/` 一起拷；只拷 bat 会退回系统 Python
