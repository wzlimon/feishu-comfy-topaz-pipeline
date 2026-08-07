# 飞书 → ComfyUI → Topaz → 百度网盘 全自动视频流水线

手机上在飞书表单里填一句提示词，这台电脑自动生成视频、超分到 1080P、扔进百度网盘，
手机网盘 App 里就能看片。中间不用碰电脑。

```
   手机/电脑                本机常驻脚本                     手机
  ┌─────────┐   轮询    ┌──────────────────────┐         ┌────────┐
  │ 飞书表单 │ ────────► │ ① ComfyUI 生成 480P   │         │ 百度网盘│
  │  填提示词 │           │ ② Topaz 超分 1080P    │ ──────► │  App   │
  │  看状态   │ ◄──────── │ ③ 丢进网盘同步目录     │  自动上传 │  看片  │
  └─────────┘   回写     └──────────────────────┘         └────────┘
```

---

## 一、当前状态

已经跑通并验证的部分：

| 环节 | 状态 | 说明 |
|------|------|------|
| ComfyUI 连通 | ✅ 已验证 | 0.30.0，RTX 4070 Ti SUPER 16G，端口 8188 |
| Topaz 超分 | ✅ 已验证 | 5.0.2 便携版，prob-4 模型实测出片 |
| 百度网盘目录 | ✅ 已建好 | `D:\bdwp\minimax_video_1080` |
| 全部代码 | ✅ 已写完 | 自检脚本、调度器、四个模块 |
| **ComfyUI 工作流** | ⬜ **待你导出** | 见第三节 步骤 1 |
| **飞书应用+表格** | ⬜ **待你配置** | 见第三节 步骤 2 |

也就是说：**代码不用你操心了，只剩两件配置的事。**

---

## 二、目录结构

```
feishu-comfy-pipeline/
├── 安装依赖.bat        ← 第一次用，双击一下（已经跑过了，可跳过）
├── 体检.bat            ← 配置过程中反复双击，它会告诉你还差什么
├── 启动.bat            ← 配好之后，双击它开始干活
├── config.yaml         ← 唯一需要你改的文件
├── main.py             调度器（轮询 → 生成 → 超分 → 投递 → 回写）
├── doctor.py           自检 / 排障工具
├── workflows/
│   └── t2v.json        ← 你要放的 ComfyUI API 工作流
├── core/
│   ├── config.py       配置加载
│   ├── feishu.py       飞书多维表格读写
│   ├── comfy.py        ComfyUI 提交与产物定位
│   ├── topaz.py        Topaz CLI 超分
│   ├── delivery.py     网盘投递（先 .tmp 后改名，防半成品）
│   └── state.py        单实例锁 + 防重复处理
├── logs/               日志，按天切分，留 14 天
└── state/              运行状态、锁文件、中间产物
```

---

## 三、配置清单（两件事）

### 步骤 1：导出 ComfyUI 工作流（约 3 分钟）

**1-1. 导出 API 格式**

1. 打开 ComfyUI，加载你平时跑通的那个文生视频工作流
2. 菜单 **工作流 → 导出(API)**（英文界面是 `Workflow → Export (API)` 或 `Save (API Format)`）
3. 存成 `workflows/t2v.json`

> ⚠️ 必须是 **API 格式**。普通的「保存」导出的是编辑器格式，接口不认。
> 区别：API 格式的 JSON 里每个节点有 `class_type` 字段，普通格式有 `nodes` 数组。

**1-2. 找到提示词该塞进哪个节点**

双击 `体检.bat` 旁边不行，这步要用命令。打开 PowerShell，粘贴：

```powershell
cd "C:\Users\办公室-图文\WorkBuddy\2026-08-05-22-26-22\feishu-comfy-pipeline"
.\.venv\Scripts\python.exe doctor.py --inspect-workflow
```

它会把工作流里所有装着文字的节点列出来，像这样：

```
节点 115   class_type = MiniMaxTextToVideoNode
  .prompt = 一只猫在草地上奔跑，电影感镜头
```

找到内容是**你平时填的正向提示词**的那个节点，记下它的编号（比如 `115`）和字段名（比如 `prompt`）。

**1-3. 填进 config.yaml**

```yaml
comfyui:
  inject:
    prompt_node: "115"        # ← 改成你的节点号
    prompt_field: "prompt"    # ← 改成上面看到的字段名
```

如果工作流有独立的负向提示词节点，一并填 `negative_node` / `negative_field`；没有就留空。

---

### 步骤 2：飞书表单 + 自建应用（约 10 分钟）

> **前提：需要飞书企业版账号。**
> 个人版没有开发者后台，拿不到 app_id/app_secret。
> 免费注册一个企业即可（飞书官网 → 注册 → 创建企业，个人也能建，免费版够用）。

**2-1. 建多维表格**

在飞书里新建一个**多维表格**（不是普通表格），建这几列，**名字必须一字不差**：

| 列名 | 类型 | 说明 |
|------|------|------|
| 提示词 | 多行文本 | 必填，表单里你填的内容 |
| 状态 | 单选 | 选项填 4 个：`待处理` `生成中` `已完成` `失败` |
| 反向提示词 | 多行文本 | 可选 |
| 随机种子 | 数字 | 可选，留空则每次随机 |
| 比例 | 单选 | **可选**，控制画面比例。选项文字**已容错**，写 `9:16` / `9：16` / `9:16 竖屏` 等都能识别；也可直接用完整值 `9:16 (Portrait Widescreen)`。不填则用默认 `16:9 (Widescreen)` |
| 视频时长 | 单选 | **可选**，控制生成秒数。选项例如 `4秒` `5秒` … `20秒`（脚本从文字里取整数，1~60 都行；本机已验证可到 20 秒）。留空则默认 5 秒 |
| 分辨率 | 单选 | **可选**，控制生成清晰度。选项填 `480P` `720P` `1080P`（对应 megapixels 0.4 / 0.9 / 2.0）；也可直接填数字如 `0.4` `2`。不填则用默认 `480P` |
| 是否超分 | 单选 | **可选**，控制生成后是否再用 Topaz 超分到 1080P。选项填 `是` `否`（写 `不` `关` `no` `false` `0` 也识别为否）。不填则默认 `是` |
| 成品文件 | 单行文本 | 脚本回写 |
| 网盘路径 | 单行文本 | 脚本回写 |
| 错误信息 | 多行文本 | 脚本回写 |
| 耗时秒 | 数字 | 脚本回写 |

> 列名必须与 `config.yaml` 里 `feishu.fields` 的「值」一一对应（本机当前用英文列名：`prompt / status / negative / seed / ratio / video_len / resolution / upscale / result_name / result_path / error / duration`）。改了列名要同步改 config。
>
> 「状态」列的默认值记得设成 **待处理**，否则表单提交后脚本捞不到。
>
> 比例 / 视频时长 / 分辨率 / 是否超分 是可选增强项，不加也照常跑（默认值 16:9、5 秒、480P、超分）。

**2-2. 加一个表单视图**

多维表格左下角 **+ → 表单**。表单里放「提示词」（必填），以及可选的「反向提示词」「随机种子」「比例」「视频时长」「分辨率」「是否超分」，
其余回写字段全部隐藏。

建好后点右上角**分享**，拿到表单链接，在手机上收藏 / 加到桌面。以后就填这个表单。

**2-3. 记下表格的两个 ID**

浏览器打开多维表格，看地址栏：

```
https://xxx.feishu.cn/base/bascnAbCdEfGhIjK123456?table=tblXyZ7890&view=vew...
                            └────── app_token ──────┘       └ table_id ┘
```

**2-4. 建自建应用**

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → **创建企业自建应用**（名字随便，比如「视频流水线」）
2. 进应用 → **凭证与基础信息** → 抄下 **App ID** 和 **App Secret**
3. **权限管理** → 搜索并开通这三个权限：
   - `bitable:app`（查看、评论、编辑和管理多维表格）
   - `bitable:app:readonly`（查看多维表格）
   - `base:record:retrieve`（如果搜得到）
4. **版本管理与发布** → 创建版本 → 申请发布（自建应用一般管理员点一下就通过，你自己就是管理员）

**2-5. ⚠️ 最容易漏的一步：把应用加成表格协作者**

回到多维表格 → 右上角 **···** → **更多** → **添加文档应用** → 搜索你刚建的应用名 → 添加，权限给**可编辑**。

> 这步不做，接口一定报 `99991672 无权限`，而且报错信息看不出是这个原因。

**2-6. 填进 config.yaml**

```yaml
feishu:
  app_id: "cli_a1b2c3d4e5f6g7h8"
  app_secret: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  app_token: "bascnAbCdEfGhIjK123456"
  table_id: "tblXyZ7890"
```

---

### 步骤 3：确认网盘目录（30 秒）

脚本已经在 `D:\bdwp\minimax_video_1080` 建好了目录。

打开百度网盘客户端 → **设置 → 同步盘**，确认同步的本地文件夹**就是** `D:\bdwp`。

- 如果是别的路径，改 `config.yaml` 里的 `baidu.sync_root`
- 如果你还没开同步盘功能，在客户端里开一下，把 `D:\bdwp` 设为同步目录

---

## 四、验证与试跑

### 4-1. 体检

双击 **`体检.bat`**。全绿就往下走，有红的照着提示修。

### 4-2. 分段试跑（推荐，出问题好定位）

打开 PowerShell，`cd` 到项目目录后：

```powershell
# 只测 ComfyUI 出片
.\.venv\Scripts\python.exe doctor.py --test-comfy "一只橘猫在夕阳下奔跑"

# 只测 Topaz 超分（拿上一步的产物，或你现成的 480P 视频）
.\.venv\Scripts\python.exe doctor.py --test-topaz "D:\cfu\ComfyUI\output\xxx.mp4"

# 只测网盘投递
.\.venv\Scripts\python.exe doctor.py --test-delivery "D:\xxx_1080p_test.mp4"
```

### 4-3. 全链路试跑

1. 在飞书表单里填一条提示词，提交
2. 命令行跑一轮：
   ```powershell
   .\.venv\Scripts\python.exe main.py --once
   ```
3. 盯着日志看四步是否依次完成，最后飞书里那条记录状态变成「已完成」

### 4-4. 正式启动

双击 **`启动.bat`**，窗口留着别关。它每 30 秒查一次飞书。

---

## 五、日常使用

1. 手机打开飞书表单 → 填提示词 → 提交
2. 表格里状态自动变化：`待处理` → `生成中` → `已完成`
3. 完成后「网盘路径」列会写上文件名
4. 打开手机百度网盘 → `minimax_video_1080` → 看片

一次可以连着提交多条，脚本按提交顺序排队跑。

---

## 六、开机自启（可选）

配好且稳定跑过几天后再弄这个。

**方法：任务计划程序**

1. `Win + R` → 输入 `taskschd.msc` → 回车
2. 右侧 **创建基本任务**
3. 名称：`飞书视频流水线`
4. 触发器：**当前用户登录时**
5. 操作：**启动程序**
   - 程序：`C:\Users\办公室-图文\WorkBuddy\2026-08-05-22-26-22\feishu-comfy-pipeline\启动.bat`
   - 起始于：`C:\Users\办公室-图文\WorkBuddy\2026-08-05-22-26-22\feishu-comfy-pipeline`
6. 完成后在任务属性里勾上「**如果任务失败，按以下频率重新启动**」

> 注意 ComfyUI 也得开机自启，否则脚本连不上。可以在同一个任务计划里加一条。

---

## 七、排障

| 现象 | 原因 / 解法 |
|------|------------|
| `99991672 无权限` | 90% 是漏了「添加文档应用」协作者，见步骤 2-5 |
| `91403 Forbidden` | 同上，或者应用版本没发布 |
| `99991663 app_id/secret 错误` | 抄错了，或者用了个人版账号 |
| `字段 XX 不存在` | 表格列名和 config.yaml 里对不上。跑 `doctor.py --feishu-fields` 看真实列名 |
| `Model not found: prob-4` | `topaz.model_dir` 指错了，必须指到 **models 子目录** |
| 工作流报 `不是 API 格式` | 用了普通「保存」。重新用「导出(API)」 |
| 生成的片子找不到 | 看 `comfyui.output_dir` 是否指对；日志里有兜底查找记录 |
| 网盘里看不到文件 | 客户端同步盘没开，或 `sync_root` 指错；先确认本地目录里有文件 |
| 提示「已有实例在运行」 | 上个窗口没关干净。删掉 `state\pipeline.lock` 再启动 |
| 任务卡在「生成中」 | 脚本崩了或被关了。手动把状态改回「待处理」，重启脚本 |

**看日志**：`logs\pipeline.log`，出错时把报错段落发我。

**常用命令**：

```powershell
.\.venv\Scripts\python.exe main.py --dry-run     # 只自检+列任务，不真跑
.\.venv\Scripts\python.exe main.py --once        # 跑一轮就退出
.\.venv\Scripts\python.exe main.py --no-topaz    # 跳过超分，快速验证链路
.\.venv\Scripts\python.exe main.py --record recXXX  # 只跑指定那条
```

---

## 八、设计上的几个考虑

- **为什么用轮询不用 Webhook**：家里/公司电脑没公网 IP，飞书回调进不来。轮询 30 秒一次，
  对使用体验没影响，也不会撞飞书频率限制。
- **为什么先写 `.tmp` 再改名**：百度网盘同步盘监听文件变化，视频还在写就会被传上去半截。
  写完再改名成 `.mp4`，网盘看到的就是完整文件。
- **为什么本地也记一份状态**：万一飞书回写失败（网络抖动），下一轮不会重复生成同一条。
- **单实例锁**：防止你不小心开了两个窗口，两个进程抢同一条任务、抢同一块显卡。
- **480P 原片归档**：留在 `D:\video\480p_archive`，万一超分参数想重调，不用重新生成。
  不想留就把 `runtime.keep_source_dir` 置空。

---

## 九、完成通知（飞书群机器人，可选）

视频生成完成（或失败）后，自动往飞书群推一张卡片，手机上即时收到，不用盯着表格状态。

**开启方式（约 1 分钟，零权限申请）：**

1. 在飞书里建一个群（可以只有你自己，专门用于收通知）。
2. 群设置 → 群机器人 → 添加机器人 → **自定义机器人** → 复制 **Webhook 地址**。
3. 打开 `config.yaml`，把 `notify.enabled` 保持 `true`，将复制的地址填进
   `notify.feishu_webhook_url`（替换掉 `[需填写]...` 占位符）。
4. （可选）机器人开启「签名校验」后，把签名密钥填进 `notify.feishu_webhook_secret`；
   不开启就留空。

**相关配置：**

```yaml
notify:
  enabled: true                       # 总开关
  channel: "feishu_webhook"           # 目前只支持飞书群机器人
  on: "both"                          # both=成功+失败都通知 / done=仅成功 / failed=仅失败
  feishu_webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
  feishu_webhook_secret: ""           # 签名校验密钥，不开启留空
```

**卡片内容（成功）：** 文件名、提示词、比例、时长、分辨率、超分、网盘路径、总耗时。
**卡片内容（失败）：** 记录 ID、提示词、失败原因。

> 通知走独立通道，即使飞书表格接口抖动也不影响主流程；未配置 webhook 时静默跳过。


---

## 十、开源与协作

本项目已在 GitHub 开源。仓库不包含任何密钥：`config.yaml`（含飞书 App 密钥、本机绝对路径）已被
`.gitignore` 忽略，仅 `config.example.yaml` 入库作为配置模板。

**从源码获取并运行（他人视角）：**

```bash
git clone <本仓库地址>
cd feishu-comfy-topaz-pipeline
python -m venv .venv && .venv/Scripts/activate      # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml                   # 然后填入你自己的飞书/ComfyUI/Topaz/网盘配置
python doctor.py                                     # 自检配置与节点
python main.py                                       # 常驻轮询；或 python main.py --once 跑一轮
```

**如何参与：**

- 提 Issue 反馈问题或建议；
- Fork 后开分支开发，PR 合并进 `main`；
- 任何改动都请确保 `config.yaml` 永不进入版本库（本地配置只留在自己机器上）。

**本地开发的日常同步：**

```bash
git add -A
git commit -m "描述这次改动"
git push origin main        # 推送到 GitHub，保持云端与本地一致
```
