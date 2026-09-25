# 智能组卷/阅卷系统

AI 驱动的组卷与阅卷平台，支持课程资料解析、考纲框架构建、知识图谱组织、合同式组卷生成和试卷导出。

## 技术栈

### 后端
- FastAPI + Uvicorn
- SQLAlchemy 2.0 + PostgreSQL
- Redis
- Celery
- LangGraph
- pydantic-settings

### 前端
- React 19 + TypeScript
- Vite
- Zustand（状态管理）
- Oxlint（代码检查；质量门禁 = `npm run build` + `npm run lint`，无单测框架）

### 外部服务
- MinerU（文档解析）
- LLM（大模型生成）
- Embedding（向量检索）
- MinIO / S3（对象存储）

## 环境要求

- Ubuntu 22.04 / 24.04 LTS
- Python 3.12
- Node.js >= 20
- PostgreSQL
- Redis
- Nginx

## 快速启动

### 1. 克隆代码

```bash
APP_DIR="/opt/intelligent-test-paper"
REPO_URL="https://gitee.com/yan-ace/zhinengchujuanxitong.git"
BRANCH="main"

sudo mkdir -p "$APP_DIR"
sudo git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"
```

> 双远程：`gitee`（当前上游，`main`）+ `origin`（GitHub）。两边都以 `main` 为主干。

### 2. 配置环境变量

```bash
cp .env.example .env
nano .env
```

至少确保包含以下配置：

```env
DATABASE_URL=postgresql+psycopg://exam:exam@localhost:5432/exam
REDIS_URL=redis://localhost:6379/0

LLM_API_KEY=你的LLM API Key
LLM_BASE_URL=https://api.stepfun.com/step_plan/v1
LLM_MODEL=step-3.7-flash

EMBEDDING_API_KEY=你的Embedding API Key
EMBEDDING_BASE_URL=https://ws-jn5396bzqr92vf9r.cn-beijing.maas.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_API_FORMAT=dashscope

MINERU_API_TOKEN=你的MinerU Token
MINERU_BASE_URL=https://mineru.net

S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=minio-dev
S3_SECRET_KEY=minio-dev-password
S3_BUCKET=exam-materials
S3_REGION=us-east-1
```

> 完整清单（含分阶段选模、抽取预算、json_schema 开关等调优项）见 `.env.example`；
> 换模型 / 模型故障速查见 [`docs/LLM_TUNING.md`](docs/LLM_TUNING.md)。

### 3. 初始化数据库

```bash
cd /opt/intelligent-test-paper
source .venv/bin/activate
cd backend
PYTHONPATH=. python -m app.db.init_db
```

如需插入开发测试数据：

```bash
PYTHONPATH=. python -m app.db.init_db --seed
```

### 4. 启动服务

项目提供了启动/停止脚本，位于 `deploy/` 目录：

```bash
cd /opt/intelligent-test-paper
sudo bash deploy/start.sh
```

该脚本会自动完成：
- 检查 `.env` 是否存在
- 初始化数据库
- 启动 API 服务（监听 `127.0.0.1:8000`）
- 启动 Celery Worker
- 可选构建前端（默认会构建）

停止服务：

```bash
sudo bash deploy/stop.sh
```

### 5. 构建前端

如果启动时跳过了前端构建，或需要单独构建：

```bash
cd /opt/intelligent-test-paper/frontend
npm ci --registry=https://registry.npmmirror.com
npm run build
```

构建产物位于：`frontend/dist/`

### 6. 配置 Nginx

参考配置模板：

```bash
sudo nano /etc/nginx/sites-available/exam-system
```

```nginx
server {
    listen 80;
    server_name YOUR_SERVER_DOMAIN_OR_IP;
    client_max_body_size 200m;

    root /opt/intelligent-test-paper/frontend/dist;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1800s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

启用站点：

```bash
sudo ln -sf /etc/nginx/sites-available/exam-system /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

## 验证服务

### 检查后端 API

```bash
curl http://127.0.0.1:8000/api/v1/health
```

期望返回：

```json
{
  "api": "ok",
  "postgresql": "ok",
  "redis": "ok",
  "mineru": "configured",
  "llm": "configured"
}
```

### 检查前端

浏览器访问：

```
http://YOUR_SERVER_DOMAIN_OR_IP/
```

### 检查进程

```bash
ps aux | grep uvicorn
ps aux | grep celery
```

### 检查服务状态

```bash
sudo systemctl status nginx
sudo systemctl status postgresql
sudo systemctl status redis-server
```

## 常用运维命令

| 操作 | 命令 |
|------|------|
| 启动服务 | `sudo bash /opt/intelligent-test-paper/deploy/start.sh` |
| 停止服务 | `sudo bash /opt/intelligent-test-paper/deploy/stop.sh` |
| 重启服务 | `sudo bash /opt/intelligent-test-paper/deploy/stop.sh && sudo bash /opt/intelligent-test-paper/deploy/start.sh` |
| 查看 API 日志 | `tail -f /opt/intelligent-test-paper/var/log/api.log` |
| 查看 Worker 日志 | `tail -f /opt/intelligent-test-paper/var/log/worker.log` |
| 重启 Nginx | `sudo systemctl reload nginx` |

## 开发环境（Docker Compose）

本地开发可使用 Docker Compose 启动中间件：

```bash
docker compose -f docker-compose.dev.yml up -d
```

后端 API（包管理器 `uv`，见 `AGENTS.md` §3.1）：

```bash
cd backend
uv sync
uv run python -m app.db.init_db --seed   # 首次：建表 + 测试账号
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Celery Worker（真实生成必须）：

```bash
cd backend
uv run celery -A app.infrastructure.tasks.celery_app.celery_app worker --loglevel=INFO
```

前端：

```bash
cd frontend
npm install
npm run dev
```

## 目录说明

```
.
├── backend/                 # 后端代码
│   └── app/
│       ├── api/v1/          # REST API 路由（7 个 router）
│       ├── domain/          # 领域模型与确定性算法（含考核规则归一化）
│       ├── workflows/       # LangGraph 工作流（框架/整理/生成/知识目录）
│       ├── services/        # 业务服务
│       ├── adapters/        # 外部适配器（LLM/解析/存储，含型号调优档案 model_profiles）
│       ├── db/              # 数据库 schema 与初始化
│       ├── infrastructure/  # 基础设施（Celery 等）
│       └── main.py          # FastAPI 入口
├── frontend/                # 前端代码（React 19 + TS）
│   └── src/
│       ├── pages/           # 页面：登录/课程空间/概览/资料库/命题框架/知识目录/试卷
│       ├── components/      # 布局与 UI 基础组件
│       ├── api/             # HTTP 层，按业务域拆分的客户端
│       └── stores/ hooks/ lib/
├── deploy/                  # 部署脚本（install/start/restart/stop）
├── docs/                    # 项目文档与素材
│   ├── backend-api.md       # 接口权威清单
│   ├── DEPLOY_UBUNTU.md     # Ubuntu 部署
│   ├── HANDOVER.md          # 交接文档
│   ├── LLM_TUNING.md        # 模型调优 / 换模型手册
│   ├── CONVERSATIONAL_GENERATION.md  # 对话式出卷接线提案（未实现）
│   └── 素材/                # 演示课程素材与卷面范本
├── CODE_WIKI.md             # 代码全景（架构/领域/工作流/API/数据库/前端）
├── docker-compose.dev.yml   # 开发环境 Docker 配置
└── .env.example             # 环境变量模板
```

产品形态：一个「试卷」模块承载后半程——项目详情页有两个页签，「出卷流水线」
（蓝图 → 合同 → 生成）与「试卷」（查看 / 编辑 / 定稿 / 导出的双栏阅读器）。
