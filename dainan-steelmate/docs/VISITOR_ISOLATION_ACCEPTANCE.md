# 匿名访客隔离：本地验收记录

日期：2026-11-20，北京时间。目标目录仅为 `E:\dainan-steelmate`。

## 结果

| 验证 | 实际结果 |
| --- | --- |
| 修改前基线 | 83 项通过，217.102 秒 |
| 新增隔离用例 | 13 项通过 |
| 最终全量回归 | 96 项通过，189.580 秒，0 失败、0 错误 |
| 真实浏览器 | 本机 headless Edge，普通持久化 profile 与独立无痕上下文；9 个场景全部完成 |
| 受保护文件 | `core/` 与 `data/` 共 29 个文件 SHA256 与实施前一致 |
| 原共享库 | 发现的 1 份原共享 SQLite SHA256 与实施前一致；未迁移或分配给新访客 |
| Community Cloud | 未验证；本轮未推送、修改线上 Secrets 或重新部署 |
| 容器构建 / 真实视觉调用 | 未执行；使用已有离线图纸与模拟模型，未读取真实视觉 Key |

最终浏览器证据：`E:\dainan-steelmate\.runtime\visitor-acceptance-20261120-192118\`。`results.json` 的 `completed` 为 `true`，包含 9 个已通过场景。

## 实际浏览器操作

1. 普通 profile A 得到真实签名 Cookie，URL 和普通页面正文没有访客凭据。TP-001 在 12% 售价毛利率下 A/B 为 **¥4,729.79 / ¥5,102.40**，保存为本库 Q-000001。
2. A 将法兰及垫板加入清单，再填写第三项未确认询盘（材质 2205），数据库保留两条已确认明细及第三项草稿。
3. 独立无痕 B 的历史为空；用 A 的 `quote=1` 不能打开、复制或导出，用 `remove=1` 没有可删除的 A 记录。B 没有 A 的恢复入口。
4. B 自己保存 TP-002，同样得到 Q-000001；两份不同数据库的同编号分别指向各自图纸和快照。
5. 仅拦截本应用的访客 Cookie，保留 Streamlit 自身 Cookie 与 XSRF/CORS。浏览器必须明确选择「仅本次会话使用」才进入工作台，之后能在独立临时空间保存记录。
6. 完全停止本轮临时 Streamlit 服务、关闭普通 profile，再用同一签名密钥和目录启动。A 的 Cookie 保持，恢复第三项 2205 草稿及前两项清单，确认前没有保存询盘入口。
7. A 历史详情实际下载客户 HTML 与内部 CSV；HTML 保留 20 件与双轨金额。登记**模拟回填**确认可供 14 件，显示系统预计 16 件、人工确认 14 件、待备料 6 件；B 的同编号没有这条复核。
8. 将 B 的 Cookie 改为无效文本后重新访问，后端签发新的随机身份，旧 B 记录没有分给新身份，也未被删除。
9. 清除 A 的 Cookie，页面检测身份变化，旧客户信息、原图引用和下载入口消失；新身份无法用原链接打开 A。使用测试后端作用域核对旧 A 的报价和草稿文件仍在。

浏览器验证为本机 HTTP/localhost，不代表 Community Cloud 的 HTTPS、嵌入页面或其他浏览器策略通过。完整线上步骤在 `E:\dainan-steelmate\docs\VISITOR_ISOLATION.md`。

## 自动用例覆盖

新增 `test_visitor_isolation.py` 覆盖：列表与客户搜索、数字 ID 和详情链接、原图、保存/删除/复制来源/导出入口、草稿和清单、复核与复制后独立性；双线程并发写入；篡改、缺失、过期和异常类型凭据；缺签名配置不触碰共享库；同 Cookie 的新会话恢复；身份变化清理全部业务状态；旧保存回调先于 main 时被拦截；临时会话不回退公共库；临时身份不会在 Cookie 身份变化后复用；有效作用域到期后拒绝连接；私有上传原图以内嵌形式显示、确认后仅保存到本人的库。

原有 83 项测试保持业务断言，仅在需要存储的 fixture 显式设置 `single_user` 和临时路径。15 张内置样例、TP-015 补参、复制改价、客户回复、单/多件快照、客户白名单、有效期、原图恢复以及大图优化/MIME/10 秒等待预算/HTTP 诊断回归均通过。不调用真实视觉 API。

联调修正了新身份初始化阶段的页面等待显示，以及临时凭据在身份变化后的清理。另在真实浏览器发现滑块控件重新挂载时可能回落到最小值，改为显式沿用当前值、缺省 12%；报价公式和已存快照未改。

## 修改文件

运行代码：

- `E:\dainan-steelmate\app.py`
- `E:\dainan-steelmate\quote_store.py`
- `E:\dainan-steelmate\visitor_context.py`（新增）
- `E:\dainan-steelmate\visitor_ui.py`（新增）

测试：

- `E:\dainan-steelmate\tests\test_visitor_isolation.py`（新增）
- `E:\dainan-steelmate\tests\browser_visitor_acceptance.py`（新增，可选 Playwright 测试工具）
- `E:\dainan-steelmate\tests\test_contest_flows.py`
- `E:\dainan-steelmate\tests\test_customer_reply.py`
- `E:\dainan-steelmate\tests\test_customer_validity.py`
- `E:\dainan-steelmate\tests\test_drawing_images.py`
- `E:\dainan-steelmate\tests\test_integration_states.py`
- `E:\dainan-steelmate\tests\test_order_item_edit.py`
- `E:\dainan-steelmate\tests\test_scrap_reviews.py`
- `E:\dainan-steelmate\tests\test_shape_drafts.py`
- `E:\dainan-steelmate\tests\test_work_drafts.py`
- `E:\dainan-steelmate\tests\test_workbench_layout_validity.py`

说明及打包：

- `E:\dainan-steelmate\README.md`
- `E:\dainan-steelmate\.env.example`
- `E:\dainan-steelmate\Dockerfile`
- `E:\dainan-steelmate\.dockerignore`
- `E:\dainan-steelmate\docs\DEPLOYMENT.md`
- `E:\dainan-steelmate\docs\VISITOR_ISOLATION.md`（新增）
- `E:\dainan-steelmate\docs\VISITOR_ISOLATION_ACCEPTANCE.md`（本记录，新增）

依赖版本、core 及原库存表不变。GitHub 更新到仓库的 `dainan-steelmate/` 子目录；不上传 `.runtime`、数据库、浏览器 profile、密钥或原业务资料。

## 证据与待办

证据目录内有 `A-workbench.png`、`A-history-top.png`、`A-history-review.png`、`B-private-empty.png`、`blocked-cookie-session.png`、`A-after-cookie-clear.png`、`A-client-quote.html`、`A-internal-quote.csv`、`results.json` 和日志。所有客户简称及复核内容均为合成验收数据，复核标注模拟。

由你生成一次并配置新的访客签名密钥，保留现有视觉 API 配置。待确认代码后，由你更新 GitHub、配置线上 Secrets 并部署，再执行真实 Community Cloud 的直接/嵌入访问、Cookie 允许/阻止、普通/无痕、刷新及重启验收。免费云端本地文件不保证长期保存，需持久化时另配存储与备份。未部署前不能将本机结果写成线上验收通过。
