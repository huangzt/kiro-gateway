<div align="center">

# 👻 Kiro Gateway

**为 Kiro API (Amazon Q Developer / AWS CodeWhisperer) 设计的高性能代理网关**

[🇷🇺 Русский](docs/ru/README.md) • [🇨🇳 中文](docs/zh/README.md) • [🇪🇸 Español](docs/es/README.md) • [🇮🇩 Indonesia](docs/id/README.md) • [🇧🇷 Português](docs/pt/README.md) • [🇯🇵 日本語](docs/ja/README.md) • [🇰🇷 한국어](docs/ko/README.md)

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)

*在 Cursor, Cline, Roo Code, Claude Code 以及任何 OpenAI/Anthropic 兼容工具中使用 Claude 3.5/3.7 模型*

</div>

---

## ✨ 核心特性

- 🔌 **全兼容接口**：同时支持 OpenAI 和 Anthropic 两种 API 格式。
- 🖥️ **可视化后台**：内置美观的管理面板，实时监控请求、管理账号、在线查看日志。
- 🧠 **深度思考支持**：原生支持 Claude 延伸思考（Extended Thinking）块的解析与转发。
- 🔀 **智能账号池**：多账号并发排队，支持自动配额检查、限速冷却以及一键切换。
- 📡 **流式响应**：全量 SSE 流式输出支持，响应速度与原版无异。
- 🌐 **代理支持**：支持 HTTP/SOCKS5 代理，解决国内环境网络连接问题。

---

## 🚀 快速开始

### 方法 A: 本地开发运行 (推荐用于调试)

1. **环境准备**：确保已安装 Python 3.10+。
2. **克隆项目**：
   ```bash
   git clone https://github.com/huangzt/kiro-gateway.git
   cd kiro-gateway
   ```
3. **安装依赖**：
   ```bash
   pip install -r requirements.txt
   ```
4. **配置环境变量**：
   ```bash
   cp .env.example .env
   # 编辑 .env 文件，填写你的凭证信息（详见下方配置说明）
   ```
5. **启动服务**：
   ```bash
   python main.py --port 8000
   ```

### 方法 B: 使用 Docker Compose (推荐生产部署)

1. **配置 `.env`**：确保根目录下已根据 `.env.example` 完成配置。
2. **一键启动**：
   ```bash
   docker compose up -d
   ```
3. **查看效果**：
   - 管理后台：`http://localhost:8000/admin`
   - API 地址：`http://localhost:8000/v1`

---

## ⚙️ 配置说明

项目支持多种认证方式，建议根据你的使用场景选择：

### 1. 基础配置 (必需)
```env
# 网关密码（连接此网关时作为 API Key 使用）
PROXY_API_KEY="your-secret-key"
```

### 2. 账号获取方式
- **方式一：Kiro IDE 登录文件**：
  直接指向 IDE 自动生成的凭证文件。
  `KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"`
- **方式二：多账号池 (推荐)**：
  创建一个文件夹（如 `kiro-accounts`），每个子文件夹存放一套账号凭证。
  `KIRO_MULTI_CREDS_DIR="./kiro-accounts"`
- **方式三：环境变量认证**：
  直接在 `.env` 中填入 `REFRESH_TOKEN`。

### 3. 网络代理 (可选)
针对网络环境受限的用户：
`VPN_PROXY_URL="http://127.0.0.1:7890"`

---

## 🔀 本地账号切换 (Host Mapping)

这是本项目特有的强大功能。通过 Docker 卷映射，你可以直接在管理后台一键将网关中的账号同步到你本机的 IDE/CLI。

1. **宿主机映射**：在 `docker-compose.yml` 中取消下面卷映射的注释：
   ```yaml
   volumes:
     - ~/.aws/sso/cache:/app/host_aws_cache:rw
   ```
2. **环境设置**：在 `.env` 中设置 `KIRO_HOST_CACHE_DIR="/app/host_aws_cache"`。
3. **一键切换**：打开后台，点击账号卡片上的 **"切换账号"**，本机的 Kiro 插件即可立即切换至该账号。

---

## 🛡️ 安全性与隐私

- **脱敏处理**：后台界面会自动遮蔽敏感的 Access Token 和 Refresh Token。
- **本地运行**：程序不上传任何凭证到第三方，所有操作均在你的本地/私有服务器完成。

---

## ⚠️ 免责声明

本工具仅供学习和研究使用，不建议用于任何违反 Amazon 或 Anthropic 条款的商业用途。

<div align="center">

**[⬆ 返回顶部](#-kiro-gateway)**

</div>
