# 戴南钢算 · SteelMate AI

**非标不锈钢智能核价与余料匹配工作台**

从图纸到报价，让每一块余料产生价值。

SteelMate AI 面向戴南不锈钢加工厂的询盘核价业务，支持图纸参数识别、人工确认、双轨报价、余料候选匹配，以及询盘记录与复制改价。数学运算由确定性 Python 引擎完成，视觉模型负责提取待确认参数。

## 当前功能

- 上传手绘草图、CAD 截图，识别材质、形状、尺寸、厚度和数量；人工核对确认后生成报价。
- 缺少关键参数时暂停报价，提供微信追问话术和手工补填。
- 并排展示方案 A「理计不含税自提价」和方案 B「过磅含 13% 专票价」。
- 调节目标毛利率，查看材料、加工成本和预估毛利。
- 查询戴南中心 3 km 内余料候选，展示适用件数与预估材料节省额；库存需人工复核。
- 保存询盘、原图与报价快照，复制改价，合并多件报价，恢复本地草稿。
- 免登录匿名访客隔离：每个浏览器凭据关联独立 SQLite，刷新后可继续自己的工作。
- 导出客户报价单 HTML、CSV；HTML 可通过浏览器打印为 PDF。

## 本地运行

使用 **Python 3.12**。本仓库提供源码和示例数据，首次安装依赖需要联网。

首次启动前，按下方「匿名访客与数据保存」生成并配置签名密钥；没有配置时，工作台停止私有数据访问。已有视觉接口配置保持原样。

Windows PowerShell，在项目根目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
```

然后在 Edge 或其他浏览器打开 <http://127.0.0.1:8501>。运行期间保留终端窗口，按 `Ctrl+C` 停止服务。若端口占用，可改用 `--server.port=8502` 并访问对应端口。

Linux / macOS：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
```

## 配置视觉接口

内置 15 张图纸采用预设映射，可以在未配置 API 的情况下运行。新上传图纸的在线识别、客户回复的模型解析需要配置兼容接口。接口不可用时可手工核对填写。

启动程序前，在同一个 PowerShell 窗口中设置环境变量：

```powershell
# 粘贴自己的 Key，输入内容不会直接显示在终端。
$env:OPENAI_API_KEY = Read-Host '输入 API Key' -MaskInput
# 使用兼容服务商时填写其完整 API Base URL；使用默认服务时无需设置。
# $env:OPENAI_BASE_URL = '填写服务商提供的 API Base URL'
# $env:VISION_MODEL = '填写支持图片与 JSON 输出的模型名称'
```

未指定 `VISION_MODEL` 时，现有代码使用 `gpt-4o`。`-MaskInput` 需要 PowerShell 7.1 及以上。Windows PowerShell 5.1 可通过系统环境变量设置 Key，重新打开终端后启动程序。

`.env.example` 仅提供配置字段说明。程序不会自动加载 `.env`，请通过进程环境变量或容器 `--env-file` 注入。密钥、真实接口配置和客户数据库均不应提交到 GitHub。

## 三个快捷示例

### 大图识别与等待时间

客户上传的原图继续用于预览和历史留存。视觉模块仅处理发送给模型的副本：自动纠正拍摄方向、将透明背景铺白，优先保留清晰的 PNG，否则使用 JPEG 优化体积，并匹配实际 MIME 类型。单次发送图片预算为 256 KiB（Base64 后约 342 KiB），该预算并非服务商公布的上限。

默认接口超时为 10 秒，不自动重试。优先单次整图识别，参数缺失时在剩余等待预算内补读重叠局部，最多一张概览和八个局部。已观察到冲突的字段留空，仍需人工核对。超时设置不能保证模型在 10 秒内完成；图片处理、网络与平台调度也会影响总耗时。

处理上限为 200 MB、4000 万像素的静态 PNG / JPEG / WebP；需要超过八个局部时提示按零件分图。接口仍返回 HTTP 413 时保留状态码并提示核实服务商限制。多次成功响应的 Token 用量按接口返回值汇总，失败或异常响应不会生成报价。

| 示例 | 图纸 | 可验证的流程 |
| --- | --- | --- |
| 304 常规垫板 | TP-002 | 完整参数、双轨报价 |
| 手绘法兰漏标厚度 | TP-015 | 缺参拦截、追问、补填后报价 |
| 316L 法兰余料匹配 | TP-001 | 双轨报价、余料候选、圆芯回收估值 |

工作台默认按 **12% 售价毛利率**生成报价。`python -m core.agent_pipeline` 的演示沿用引擎默认 **12% 成本加价**，金额口径不同。核验表沿用流水线口径，不能直接当作工作台默认金额。

## 数据与业务边界

- `Dainan_Pricing_Rules.xlsx` 是本地报价基准表，页面读取其首表规则，不是自动连接的实时行情。
- `Scrap_Inventory.csv` 包含 40 条脱敏演示库存记录，库存可供数量、余料可用形状和交付条件需工厂复核。
- `Test_Images/` 包含 15 张内置示例。`Blind_Test/` 包含 6 张合成识图样例及人工核对基准，不代表独立客户样本测试结论。
- 法兰加工费按外圆及中心孔估算，附加孔和特殊工艺另行核价。方案 B 的实际结算重量以交付过磅为准。
- 界面与报价单中的红章为演示用图形标识，不具有真实企业电子签章效力。

## 记录与部署

### 匿名访客与数据保存

默认 `DAINAN_STORAGE_MODE=visitor`。后端用 `secrets` 生成随机凭据并用 HMAC-SHA256 签名；有效期为签发起 **30 天**，不是每次刷新延长 30 天。Cookie 仅保存签名凭据，不保存客户字段、数据库路径或 API Key。凭据不会写入分享链接、查询参数或普通页面文本。

在本机执行以下命令生成一次密钥，仅在你本机查看输出，不提交、不截图或发到聊天中：

```powershell
py -3.12 -c "import secrets; print(secrets.token_urlsafe(48))"
```

把生成值保存在本机 `.streamlit/secrets.toml`，或部署时填入 Community Cloud 的 **Settings → Secrets**。合并进已有配置，保留已配置的视觉接口字段。模板如下，`<generated-key>` 必须替换：

```toml
DAINAN_STORAGE_MODE = "visitor"
DAINAN_VISITOR_SIGNING_KEY = "<generated-key>"
# 可选：服务器固定数据根目录，不由浏览器指定
# DAINAN_VISITOR_DATA_ROOT = "/tmp/dainan-steelmate/visitors"
```

同名环境变量优先于 Secrets。签名密钥至少 32 字节；请保留同一个随机值，**不要每次启动重新生成**。缺少配置、凭据未就绪或校验失败时不会访问共享库。Cookie 缺失、篡改或过期后会生成新的随机身份；密钥轮换同样会使旧凭据失效。旧库仍保留，不自动迁移、不分配给第一个访问者，也不自动删除。

每次存储操作和页面回调都校验访客上下文。默认根目录为 `tempfile.gettempdir()/dainan-steelmate/visitors`，Linux 通常是 `/tmp/dainan-steelmate/visitors`，Windows 通常是 `%TEMP%\dainan-steelmate\visitors`。数据库为 `<根目录>/<后端生成的访客哈希>/quotes.sqlite3`，报价、原图 BLOB、草稿、清单快照、余料复核均保存在该访客库中。`DAINAN_QUOTE_DB` 在访客模式下不生效。

Cookie 写入使用已固定的 Streamlit 1.67.0 v2 组件，不增加 Cookie 或认证依赖，不关闭 XSRF/CORS。HTTPS 使用 `Secure; SameSite=None`，本机 HTTP 使用 `SameSite=Lax`。浏览器阻止 Cookie 时，可明确选择「仅本次会话使用」，它仍使用独立随机数据库，关闭、刷新或连接重建后可能找不回。缺少签名配置时不能进入该降级模式。私有图片内嵌显示，文件由当前浏览器 Blob 下载，不创建 Streamlit 的公共 `/media` 下载链接。

**边界：**同一个浏览器配置文件被多人共用时，会共用同一匿名身份与记录；无痕窗口、独立配置文件和其他设备是独立访客。清除 Cookie 或凭据过期后无法自动找回旧身份；匿名模式不提供密码找回或跨设备同步。凭据是浏览器持有的访问凭证，须防止泄露；JS 写 Cookie 不能设置 HttpOnly，后续需要更强的身份/会话安全时应使用服务端认证。

**隔离不等于长期保存。** 免费 Community Cloud 的本地磁盘不保证长期保存；休眠、重启或重新部署可能使记录和原图丢失，即使 Cookie 仍有效。需要长期留存时，由运维另配持久卷与备份，本轮不提供云端持久化承诺。

仅在你明确选择本机单人工作台时，设置 `DAINAN_STORAGE_MODE=single_user` 才允许使用旧 `DAINAN_QUOTE_DB`；未指定路径时 Windows 为 `%LOCALAPPDATA%\DainanSteelAI\quotes.sqlite3`，其他系统为 `~/.dainan-steel-ai/quotes.sqlite3`。此模式会显示共用记录提示，**不得用于公开部署**。现有共享库完整保留，新匿名访客不会浏览它。

已提供 `Dockerfile` 和 Cloudflare 接入说明，见 [部署说明](docs/DEPLOYMENT.md)。本仓库尚未配置 Cloudflare 自动部署。Streamlit 服务需要 Python 运行环境，单纯上传源码不会生成可访问的工作台。

## 开发与测试

三层结构保持原项目约定：

```text
app.py                         前端工作台
workbench.css                  页面样式
quote_store.py                 询盘、原图与草稿存储
visitor_context.py             签名校验与当前执行上下文分库
visitor_ui.py                  浏览器 Cookie、回调身份检查与私有下载
core/steel_engine.py           确定性算料引擎
core/agent_pipeline.py         三角色标准流水线
core/vision_parser.py          视觉识别和离线预设
core/reply_parser.py           客户回复参数解析
data/                          示例图纸与本地数据表
tests/                         现有回归测试
.github/workflows/tests.yml    GitHub 自动测试
```

```powershell
.\.venv\Scripts\python.exe -X utf8 -m core.agent_pipeline
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

Linux / macOS 将解释器路径替换成 `.venv/bin/python`。自动测试不需要真实 API Key，模型响应由测试替身提供。

对应仓库为 [Eunii2007/SteelMate-AI](https://github.com/Eunii2007/SteelMate-AI)，应用位于仓库的 `dainan-steelmate/`。修改文件更新到这个子目录；Community Cloud 入口为 `dainan-steelmate/app.py`。保留 `.streamlit`、`.gitignore` 等文件，勿上传 `.runtime`、Secrets、数据库或 Python 环境。具体更新清单与线上验收步骤见 [匿名隔离说明](docs/VISITOR_ISOLATION.md)。本轮不自动提交、推送或部署。
