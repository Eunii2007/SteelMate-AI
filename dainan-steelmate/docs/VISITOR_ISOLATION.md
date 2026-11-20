# 匿名访客隔离与部署验收

## 版本和范围

本地目标仅为 `E:\dainan-steelmate`；对应仓库 `Eunii2007/SteelMate-AI` 的 `dainan-steelmate/`。实施前通过只读 GitHub 比对，`main` 的提交为 `4fcd38349e6d77a6544954720138e588e2d32153`，app、存储、核价、流水线、视觉模块和依赖版本文本一致。

本轮不提交或推送 GitHub，不修改线上 Secrets，不触发部署。`E:\竞赛`、桌面项目、录制版压缩包不在修改范围。相关源码备份在 `.runtime/visitor-backup-20261120-185105/`，其中 `integrity.json` 记录受保护的 core、数据文件及旧共享数据库的实施前 SHA256。Docker 文件的原始副本另外核对留存。

## 实现

- `visitor_context.py`：安全随机凭据、HMAC-SHA256 校验、30 天到期、服务器根目录与 SHA256 子目录。每次连接均重新校验，不按 IP、UA 或编号识别访客。
- `visitor_ui.py`：当前固定版本 Streamlit 1.67.0 内置 v2 组件读写真实 Cookie。先读浏览器 Cookie，再由后端签发，写入并读回后才允许私有操作。没有虚构 `st.context.cookies` 的写接口，也不依赖 iframe 访问跨域父页面。参见 [v2 组件官方说明](https://docs.streamlit.io/develop/api-reference/custom-components/st.components.v2.component)。
- `quote_store.py`：统一在数据库路径入口获取当前访客上下文，复用原有表、SQL、图纸 BLOB、事务及快照。没有在访客之间切换全局 `DAINAN_QUOTE_DB`。
- UI 回调绑定生成该控件时的访客，执行前再与当前有效凭据比较；因此回调先于 `main()` 执行也不会跨访客操作。身份变化清理全部业务 Session State，包括草稿引用、清单、原图和打印内容。
- ContextVar 只提供执行上下文隔离的后端任务/测试作用域，不保存进程共享的“当前访客”；正常 UI 存储调用逐次读取当前 Streamlit 会话中的已验证浏览器观测。
- 私有缩略图与大图使用内嵌图像，HTML、CSV 和核验记录通过当前浏览器 Blob 下载，避免生成可转发的 Streamlit 公共媒体 URL；客户字段白名单不变。

### 配置

在本机生成一次，保留同一个值，不要把命令的真实输出放进报告：

```powershell
py -3.12 -c "import secrets; print(secrets.token_urlsafe(48))"
```

合并到本机 `.streamlit/secrets.toml` 或 Community Cloud 的应用 Secrets，已有视觉配置不动：

```toml
DAINAN_STORAGE_MODE = "visitor"
DAINAN_VISITOR_SIGNING_KEY = "<generated-key>"
# 可选，仅由服务器配置；免费云端此路径不构成持久化承诺
# DAINAN_VISITOR_DATA_ROOT = "/tmp/dainan-steelmate/visitors"
```

环境变量优先。默认 `visitor`，缺少必要配置会阻止私有数据访问，不降级到共享库。`single_user` 只供显式配置的本地单人工具使用，不能用于公网实例。

默认文件为 `<系统临时目录>/dainan-steelmate/visitors/<已验证身份的哈希>/quotes.sqlite3`。Cookie 不包含文件路径或业务数据。目录只从已签名校验的随机身份生成，不接受前端路径参数。旧共享库保留，不迁移、不自动删除。

Cookie 有效期为签发起 30 天，清除、到期或签名密钥轮换后建立新身份，原文件仍保留。相同浏览器配置文件（含多个标签页）共享身份；多人共用该配置文件时也共享记录。无痕会话、另一配置文件或另一设备分别隔离。匿名模式无法找回已丢失的凭据。

HTTPS Cookie 为 `Secure; SameSite=None; Path=/`，本机 HTTP 为 `SameSite=Lax`。浏览器不允许 Cookie 时，用户可选择「仅本次会话使用」，仍然独立存储；刷新、断线或关闭后可能找不回。不关闭 XSRF/CORS。JS 写入的 Cookie 不是 HttpOnly，凭据属于访问凭证，须避免泄露；没有引入账号认证。

免费 Community Cloud 本地磁盘不保证长期保存，Cookie 有效并不能保证记录文件仍存在。长期保存须另行配置持久卷与备份。本轮不执行旧数据清理，也不提供跨设备同步。

## 本地验证方式

本轮实际通过：修改前 83 项，修改后完整回归 96 项（含新增 13 项）；本机 Edge 9 个浏览器场景完成。详见 [本轮验收记录](VISITOR_ISOLATION_ACCEPTANCE.md)，线上仍为待验证。

自动测试完全使用临时数据库、合成/内置图纸和模拟模型，测试进程不使用真实视觉 API Key。

```powershell
python -B -m unittest discover -s tests -v
```

原有测试显式选择 `single_user` 并指向各自临时库；隔离用例显式选择 `visitor`，且准备旧共享库哨兵逐项确认其字节未改变。新增覆盖 A/B 历史与搜索、详情和原图、同编号不同快照、删除/复制/导出入口、草稿和清单、逐项复核、并发存储、回调先于 main、凭据未就绪/无效/过期、身份变化清理及刷新恢复。金额、缺参、复制改价、多件报价和大图识别沿用原有回归。

浏览器验收脚本为 `tests/browser_visitor_acceptance.py`。Playwright 仅是可选测试工具，不增加应用运行依赖。使用安装了 Playwright 的测试环境执行：

```powershell
python -B tests/browser_visitor_acceptance.py
```

脚本启动独立本机临时端口，只用内置样例；使用真实 Edge 的普通持久化配置文件及独立无痕上下文。签名密钥仅在测试进程环境内生成并传给其子服务，不写日志或报告。测试完成停止该服务，不影响其他项目服务。证据生成到 `.runtime/visitor-acceptance-<时间>/`，包括截图、客户 HTML、内部 CSV、完成状态和无凭据日志。`.runtime` 不应上传 GitHub。

## GitHub 更新路径

待你确认后，手工将以下文件更新到仓库 `dainan-steelmate/` 下：

- `app.py`、`quote_store.py`、新增 `visitor_context.py`、`visitor_ui.py`。
- `tests/test_visitor_isolation.py`、`tests/browser_visitor_acceptance.py` 和现有测试的显式本地模式 fixture 改动。
- `README.md`、`.env.example`、`Dockerfile`、`.dockerignore`、`docs/DEPLOYMENT.md`、本文及本轮本地验收报告。

`requirements.txt` 不变。不要上传 `.runtime`、Cookie 浏览器配置文件、任何数据库、密钥或客户资料。当前本地目录没有 `.git` 元数据，本轮通过只读远程比对及备份 diff 核对改动，不初始化仓库、不自动提交。

## Community Cloud 线上验收（本轮未执行）

1. 确认应用入口为 `dainan-steelmate/app.py`，依赖安装自同级 `requirements.txt`，版本为 Streamlit 1.67.0。你确认代码后自行更新 GitHub。
2. 在应用 Secrets 设置稳定的随机 `DAINAN_VISITOR_SIGNING_KEY` 和 `DAINAN_STORAGE_MODE="visitor"`，保留已有视觉参数。不要在网页输入或日志打印 Key，也不要把旧共享库上传云端。
3. 由你授权并执行重部署后，通过实际 `*.streamlit.app` 链接访问。分别测试直接打开和 Community Cloud 的嵌入页面，不能用本地 HTTP Cookie 的成功代替 HTTPS/嵌入验证。
4. 普通浏览器 A 保存一张带明显测试客户简称的 20 件 TP-001；留一份未完成草稿、两项清单和原图；刷新、关闭后重开，应仍为自己的记录，确认前不能恢复成有效新报价。
5. 独立无痕浏览器 B 打开同一应用：列表和客户搜索无 A；粘贴 A 的 `?view=records&quote=1` 或删除链接不能读取 A、复制 A、导出 A 或修改其复核。B 自己保存出同为 1 的编号后，只能看到 B 的记录。两边分别复核模拟件数，检查互不干扰。
6. A 从历史打开图纸并下载客户 HTML 与内部 CSV，核对双轨金额和原有效期；B 不能用 A 的详情链接取得这些内容。确认所有下载都成功，客户输出仍是白名单字段。
7. 用浏览器开发者工具本机查看 Cookie 属性（不要截图/共享凭据值）：名称、30 天到期、Secure、SameSite 及是否被浏览器阻止；测试第三方 Cookie 默认策略和阻止策略。Cookie 不可用时明确选择临时会话，应可独立保存，且没有公共库兜底。
8. 修改/删除该 Cookie，或在验收副本轮换签名密钥：应建立新身份或阻止访问，清除旧报价、原图与下载内容；不得把旧文件归给新身份。变更前做好业务资料备份，不为测试删除真实数据。
9. 分别从两边同时保存、加入清单、复制改价；最后复查 TP-015 缺参、TP-001 双轨金额和多件最早有效期。若免费云端重启后文件不在，应按磁盘持久性限制记录，不能宣称匿名凭据能恢复已丢失的文件。

验收记录需区分直接访问/嵌入访问、普通/无痕、Cookie 允许/阻止等结果。本机通过不能写为线上通过。容器镜像构建与真实视觉接口均不在此次离线隔离验收范围。
