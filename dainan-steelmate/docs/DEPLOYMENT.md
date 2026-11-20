# GitHub 与 Cloudflare 部署说明

## 当前交付范围

本目录是可提交到 GitHub 的源码快照，业务源码及 `core/` 算料公式与原项目一致。已增加固定版本依赖、Git 忽略规则、Docker 配置和 GitHub 自动测试。

尚未创建远程仓库、上传 GitHub、创建 Cloudflare 资源，也未配置域名、服务器或自动发布流程。整理机器没有 Docker，本次未执行容器镜像构建或 Linux 容器运行验证。

## 先上传 GitHub

1. 新建仓库，建议名称 `dainan-steelmate`，首次上传建议设为 Private。
2. 将此目录中的文件提交到仓库根目录，入口应是根目录 `app.py`，README、requirements.txt 与其同级。
3. 确认 `.streamlit/config.toml`、`.github/workflows/tests.yml`、`.gitignore` 均存在。
4. 提交中不应出现 `.env`、`secrets.toml`、API Key、SQLite 数据库、用户上传图或本地运行环境。
5. 推送后查看 GitHub Actions 测试结果。自动测试使用模拟模型响应，不需要把真实 Key 放到 Actions。

GitHub 的 Public / Private 只决定源码仓库可见性。应用是否能被外部访问，由运行服务与访问控制决定。

## 推荐接入路径

```text
GitHub 源码
    ↓ 拉取固定提交或版本
常开服务器上的 Python / Docker 服务
    ↓ 本机 HTTP 127.0.0.1:8501
Cloudflare Tunnel
    ↓ HTTPS 域名
访问者浏览器
```

这条路径需要服务器或常开电脑、Cloudflare 账号及接入 Cloudflare 的域名。若使用自己的电脑作为源站，关机、休眠或断网会影响访问。参见 [Cloudflare Tunnel 官方说明](https://developers.cloudflare.com/tunnel/get-started/)。

## 容器启动示例

在已安装 Docker 的服务器上，于源码根目录构建：

```bash
docker build -t dainan-steelmate:20261120 .
```

按 `.env.example` 在服务器本地创建 `.env` 并填入接口配置。该文件会被 Git 与 Docker 构建上下文排除，Key 通过运行环境传入。

```bash
docker run -d --name dainan-steelmate \
  --restart unless-stopped \
  --env-file .env \
  --mount type=volume,source=dainan-steelmate-data,target=/var/lib/dainan-steel-ai \
  -p 127.0.0.1:8501:8501 \
  dainan-steelmate:20261120
```

只运行离线预设时，可以省去 `--env-file .env`。挂载卷用于保存 SQLite 记录与原图，更新镜像时保留该数据卷，正式使用后另做备份。

容器使用 `0.0.0.0:8501` 监听，由上面的端口映射限制到服务器本机。不要为了接入 Tunnel 关闭 Streamlit 的 XSRF 或 CORS 保护。容器健康检查访问 `/_stcore/health`。配置方式参考 [Streamlit Docker 官方文档](https://docs.streamlit.io/deploy/tutorials/docker)。

在服务器本机验证：

```bash
curl --fail http://127.0.0.1:8501/_stcore/health
```

## Cloudflare Tunnel

在 Cloudflare 控制台创建具名 Tunnel，按控制台给出的操作系统命令在源站安装 `cloudflared` 服务。将公开应用路由的 Service URL 设置为 `http://127.0.0.1:8501`，绑定自己的域名。

该地址适用于 `cloudflared` 直接运行在服务器主机上。若 `cloudflared` 也在容器内，应使用正确的容器网络服务名，而不能把其自身的 `127.0.0.1` 当作应用容器。

Tunnel Token 仅保存在服务器配置中，不写入 README、源码或 GitHub。Tunnel 连通后，还要实际验证页面、WebSocket 交互、图片上传与报价下载。

当前项目是 Python Streamlit 服务。此目录提供的 Dockerfile 用于容器化应用，并未包含 Cloudflare Workers / Containers 的路由 Worker、账户配置及持久存储方案。若选择 Cloudflare Containers，需要单独完成适配，不能将此目录作为普通静态站点直接发布后即运行。

## 对外开放前的现有约束

当前 `quote_store.py` 按单人工作台设计，所有连接共用历史报价与固定草稿记录。只更换为空数据库，不能解决多访问者之间的记录共享。

初次上线应先用于受控单人验收，之后再按选定方案实现独立访问者数据隔离或工厂账号隔离。确认隔离前，不在公开实例内输入真实客户信息或业务图纸。

上线验收至少包括：

- 通过外部网络打开首页并进入工作台。
- TP-002 报价正常，TP-015 补厚前拦截、补厚后出价。
- TP-001 双轨金额、余料展示、客户 HTML / CSV 一致。
- 新图识别后必须确认参数，接口异常时可手工填写。
- 两个独立浏览器会话验证记录与草稿隔离方案。
- 重启服务后，预期保留的记录仍可读取。
