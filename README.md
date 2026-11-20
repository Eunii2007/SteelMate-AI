# SteelMate-AI
戴南钢算 · SteelMate AI
非标不锈钢智能核价与余料匹配工作台

从图纸到报价，让每一块余料产生价值。

SteelMate AI 面向戴南不锈钢加工厂的询盘核价业务，支持图纸参数识别、人工确认、双轨报价、余料候选匹配，以及询盘记录与复制改价。数学运算由确定性 Python 引擎完成，视觉模型负责提取待确认参数。

当前功能
上传手绘草图、CAD 截图，识别材质、形状、尺寸、厚度和数量；人工核对确认后生成报价。
缺少关键参数时暂停报价，提供微信追问话术和手工补填。
并排展示方案 A「理计不含税自提价」和方案 B「过磅含 13% 专票价」。
调节目标毛利率，查看材料、加工成本和预估毛利。
查询戴南中心 3 km 内余料候选，展示适用件数与预估材料节省额；库存需人工复核。
保存询盘、原图与报价快照，复制改价，合并多件报价，恢复本地草稿。
导出客户报价单 HTML、CSV；HTML 可通过浏览器打印为 PDF。
本地运行
使用 Python 3.12。本仓库提供源码和示例数据，首次安装依赖需要联网。

Windows PowerShell，在项目根目录执行：

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
然后在 Edge 或其他浏览器打开 http://127.0.0.1:8501。运行期间保留终端窗口，按 Ctrl+C 停止服务。若端口占用，可改用 --server.port=8502 并访问对应端口。

Linux / macOS：

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
配置视觉接口
内置 15 张图纸采用预设映射，可以在未配置 API 的情况下运行。新上传图纸的在线识别、客户回复的模型解析需要配置兼容接口。接口不可用时可手工核对填写。

启动程序前，在同一个 PowerShell 窗口中设置环境变量：

# 粘贴自己的 Key，输入内容不会直接显示在终端。
$env:OPENAI_API_KEY = Read-Host '输入 API Key' -MaskInput
# 使用兼容服务商时填写其完整 API Base URL；使用默认服务时无需设置。
# $env:OPENAI_BASE_URL = '填写服务商提供的 API Base URL'
# $env:VISION_MODEL = '填写支持图片与 JSON 输出的模型名称'
未指定 VISION_MODEL 时，现有代码使用 gpt-4o。-MaskInput 需要 PowerShell 7.1 及以上。Windows PowerShell 5.1 可通过系统环境变量设置 Key，重新打开终端后启动程序。

.env.example 仅提供配置字段说明。程序不会自动加载 .env，请通过进程环境变量或容器 --env-file 注入。密钥、真实接口配置和客户数据库均不应提交到 GitHub。

三个快捷示例
示例	图纸	可验证的流程
304 常规垫板	TP-002	完整参数、双轨报价
手绘法兰漏标厚度	TP-015	缺参拦截、追问、补填后报价
316L 法兰余料匹配	TP-001	双轨报价、余料候选、圆芯回收估值
工作台默认按 12% 售价毛利率生成报价。python -m core.agent_pipeline 的演示沿用引擎默认 12% 成本加价，金额口径不同。核验表沿用流水线口径，不能直接当作工作台默认金额。

数据与业务边界
Dainan_Pricing_Rules.xlsx 是本地报价基准表，页面读取其首表规则，不是自动连接的实时行情。
Scrap_Inventory.csv 包含 40 条脱敏演示库存记录，库存可供数量、余料可用形状和交付条件需工厂复核。
Test_Images/ 包含 15 张内置示例。Blind_Test/ 包含 6 张合成识图样例及人工核对基准，不代表独立客户样本测试结论。
法兰加工费按外圆及中心孔估算，附加孔和特殊工艺另行核价。方案 B 的实际结算重量以交付过磅为准。
界面与报价单中的红章为演示用图形标识，不具有真实企业电子签章效力。
记录与部署
数据库路径可用 DAINAN_QUOTE_DB 环境变量指定。未指定时，Windows 使用 %LOCALAPPDATA%\DainanSteelAI\quotes.sqlite3，Linux / macOS 使用 ~/.dainan-steel-ai/quotes.sqlite3。

测试、部署或与原本地版并行运行时，请使用独立数据库路径。运行测试时现有测试用例使用临时数据库。

当前版本的历史记录和持久草稿共用一套存储，没有多客户身份隔离。对公网开放前，需要先选择受控单人演示，或完成访问者记录隔离。部署副本使用新的空数据库，避免加载本机客户数据。

已提供 Dockerfile 和 Cloudflare 接入说明，见 部署说明。本仓库尚未配置 Cloudflare 自动部署。Streamlit 服务需要 Python 运行环境，单纯上传源码不会生成可访问的工作台。

开发与测试
三层结构保持原项目约定：

app.py                         前端工作台
workbench.css                  页面样式
quote_store.py                 询盘、原图与草稿存储
core/steel_engine.py           确定性算料引擎
core/agent_pipeline.py         三角色标准流水线
core/vision_parser.py          视觉识别和离线预设
core/reply_parser.py           客户回复参数解析
data/                          示例图纸与本地数据表
tests/                         现有回归测试
.github/workflows/tests.yml    GitHub 自动测试
.\.venv\Scripts\python.exe -X utf8 -m core.agent_pipeline
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
Linux / macOS 将解释器路径替换成 .venv/bin/python。自动测试不需要真实 API Key，模型响应由测试替身提供。

上传 GitHub 时，将本目录中的文件放在仓库根目录，保留 .streamlit、.github、.gitignore 等以点开头的文件及目录。不要将本地运行用的整套 Python 环境上传为源码。
