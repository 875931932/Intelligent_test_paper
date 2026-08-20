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
- Vitest

### 外部服务
- MinerU（文档解析）
- DeepSeek（大模型生成）
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
BRANCH="master"

sudo mkdir -p "$APP_DIR"
sudo git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"
```

### 2. 配置环境变量

```bash
cp .env.example .env
nano .env
```

至少确保包含以下配置：

```env
DATABASE_URL=postgresql+psycopg://exam:exam@localhost:5432/exam
REDIS_URL=redis://localhost:6379/0

DEEPSEEK_API_KEY=你的DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.xiaomimimo.com/v1
DEEPSEEK_MODEL=mimo-v2.5-pro

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
  "deepseek": "configured"
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

后端 API：

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
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
│       ├── api/v1/          # REST API 路由
│       ├── domain/          # 领域模型
│       ├── workflows/       # LangGraph 工作流
│       ├── services/        # 业务服务
│       ├── adapters/        # 外部适配器
│       ├── db/              # 数据库 schema 与初始化
│       ├── infrastructure/  # 基础设施（Celery 等）
│       └── main.py          # FastAPI 入口
├── frontend/                # 前端代码
├── deploy/                  # 部署脚本
├── docker-compose.dev.yml   # 开发环境 Docker 配置
└── .env.example             # 环境变量模板
```
