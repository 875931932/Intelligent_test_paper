# Ubuntu 部署指南

以下命令以 Ubuntu 22.04/24.04、部署目录 `/opt/intelligent-test-paper` 为例。生产环境请使用独立数据库、对象存储和密钥管理服务，不要把真实密钥提交到 Git。

## 1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential python3.12 python3.12-venv python3-pip nginx

# Node.js 22 LTS（仅在服务器需要自行构建前端时安装）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version
npm --version
```

如果系统仓库没有 Python 3.12，请使用 pyenv、deadsnakes 或发行版提供的 Python 3.12；后端要求 Python >= 3.12。

## 2. 安装并启动 Redis

```bash
sudo apt update
sudo apt install -y redis-server

# 仅监听本机，避免把 Redis 暴露到公网
sudo sed -i 's/^#\?bind .*/bind 127.0.0.1 ::1/' /etc/redis/redis.conf
sudo sed -i 's/^#\?protected-mode .*/protected-mode yes/' /etc/redis/redis.conf
sudo systemctl enable --now redis-server
sudo systemctl restart redis-server
redis-cli ping
# 预期输出：PONG
```

若 API 和 Worker 不在同一台机器，Redis 应放在私网并设置密码/ACL，同时将 `REDIS_URL` 改为 `redis://:密码@私网地址:6379/0`，不要开放公网 6379。

## 3. 获取代码并创建后端环境

```bash
sudo mkdir -p /opt/intelligent-test-paper
sudo chown -R "$USER":"$USER" /opt/intelligent-test-paper
git clone https://github.com/875931932/Intelligent_test_paper.git /opt/intelligent-test-paper
cd /opt/intelligent-test-paper

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./backend

cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

至少配置以下变量：

```dotenv
DATABASE_URL=postgresql+psycopg://<db_user>:<db_password>@<db_host>:5432/<db_name>
REDIS_URL=redis://127.0.0.1:6379/0
DEEPSEEK_API_KEY=<llm_key>
DEEPSEEK_BASE_URL=<openai-compatible-llm-base-url>
DEEPSEEK_MODEL=<paper-generation-model>
MINERU_API_TOKEN=<mineru_token>
MINERU_BASE_URL=https://mineru.net
EMBEDDING_API_KEY=<dashscope_key>
EMBEDDING_BASE_URL=https://ws-jn5396bzqr92vf9r.cn-beijing.maas.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_API_FORMAT=dashscope
S3_ENDPOINT=http://<minio-or-s3-host>:9000
S3_ACCESS_KEY=<access_key>
S3_SECRET_KEY=<secret_key>
S3_BUCKET=exam-materials
```

`DEEPSEEK_BASE_URL` 需要填写实际模型网关地址；不要照抄示例中的模型名称或把 API Key 写入 shell 历史。

## 4. 初始化 PostgreSQL/pgvector

数据库需要 PostgreSQL 16 和 `vector` 扩展。创建数据库后执行：

```bash
cd /opt/intelligent-test-paper/backend
source ../.venv/bin/activate
PYTHONPATH=. python -m app.db.init_db
```

该命令会创建当前版本表结构并尝试启用 `vector` 扩展；它不是历史迁移工具。数据库用户需要建表和创建扩展的权限，或者由 DBA 预先执行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

## 5. 启动 API

先在前台验证：

```bash
cd /opt/intelligent-test-paper/backend
source ../.venv/bin/activate
set -a && . ../.env && set +a
PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端检查：

```bash
curl http://127.0.0.1:8000/api/v1/health
```

真实出卷前，返回中的 `postgresql`、`redis`、`mineru`、`deepseek` 都必须为可用/已配置状态。

## 6. 启动真实出卷 Worker

API 只创建 durable task 并投递 outbox，不能代替 Worker。生产环境至少运行一个 Celery Worker：

```bash
cd /opt/intelligent-test-paper/backend
source ../.venv/bin/activate
PYTHONPATH=. celery -A app.infrastructure.tasks.celery_app.celery_app worker --loglevel=INFO --concurrency=4
```

建议使用 systemd 托管 API 和 Worker。创建 `/etc/systemd/system/exam-api.service`：

```ini
[Unit]
Description=AI Exam FastAPI
After=network.target redis-server.service

[Service]
User=<deploy_user>
WorkingDirectory=/opt/intelligent-test-paper/backend
EnvironmentFile=/opt/intelligent-test-paper/.env
Environment=PYTHONPATH=/opt/intelligent-test-paper/backend
ExecStart=/opt/intelligent-test-paper/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

创建 `/etc/systemd/system/exam-worker.service`：

```ini
[Unit]
Description=AI Exam Celery Worker
After=network.target redis-server.service exam-api.service

[Service]
User=<deploy_user>
WorkingDirectory=/opt/intelligent-test-paper/backend
EnvironmentFile=/opt/intelligent-test-paper/.env
Environment=PYTHONPATH=/opt/intelligent-test-paper/backend
ExecStart=/opt/intelligent-test-paper/.venv/bin/celery -A app.infrastructure.tasks.celery_app.celery_app worker --loglevel=INFO --concurrency=4
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now exam-api exam-worker
sudo systemctl status exam-api exam-worker --no-pager
sudo journalctl -u exam-api -u exam-worker -f
```

## 7. Outbox 重投递与任务恢复

创建任务时 API 会立即尝试投递；Redis 短暂不可用时事件会留在 PostgreSQL 的 `pending` 状态。部署环境应每分钟运行一次 dispatcher/recovery，或将同等逻辑接入现有运维调度。项目已提供全课程扫描脚本：

```bash
cd /opt/intelligent-test-paper/backend
source ../.venv/bin/activate
set -a && . ../.env && set +a
PYTHONPATH=. python scripts/dispatch_tasks.py
```

建议用 systemd timer 而不是 cron。创建 `/etc/systemd/system/exam-dispatcher.service`：

```ini
[Unit]
Description=AI Exam outbox dispatcher and task recovery
After=network.target redis-server.service

[Service]
Type=oneshot
User=<deploy_user>
WorkingDirectory=/opt/intelligent-test-paper/backend
EnvironmentFile=/opt/intelligent-test-paper/.env
Environment=PYTHONPATH=/opt/intelligent-test-paper/backend
ExecStart=/opt/intelligent-test-paper/.venv/bin/python scripts/dispatch_tasks.py
```

创建 `/etc/systemd/system/exam-dispatcher.timer`：

```ini
[Unit]
Description=Run AI Exam dispatcher every minute

[Timer]
OnBootSec=30
OnUnitActiveSec=60
Persistent=true

[Install]
WantedBy=timers.target
```

启用它：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now exam-dispatcher.timer
systemctl list-timers exam-dispatcher.timer
```

## 8. 构建前端并配置 Nginx

```bash
cd /opt/intelligent-test-paper/frontend
npm ci
npm run build
```

创建 `/etc/nginx/sites-available/exam-system`：

```nginx
server {
    listen 80;
    server_name <your-domain-or-server-ip>;
    root /opt/intelligent-test-paper/frontend/dist;
    index index.html;
    client_max_body_size 210m;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/exam-system /etc/nginx/sites-enabled/exam-system
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

资料上传使用 S3 预签名地址时，浏览器必须能够访问 `S3_ENDPOINT`。若使用 MinIO，请通过同一域名反向代理或设置该 bucket 的 CORS，仅允许部署站点域名发起 `PUT` 请求。

## 9. 最小验收

1. `curl http://127.0.0.1:8000/api/v1/health`。
2. 浏览器创建课程并上传大纲/教学材料。
3. 解析资料，确认考核大纲和教学大纲均完成整理并由教师确认。
4. 发布知识目录，确认蓝图、命题合同后启动生成。
5. 观察任务状态 `queued -> running -> succeeded/failed`，Worker 日志应出现正式模型调用。
6. 在数据库 `model_calls` 检查 `stage = paper_generation`，确认不是 mock graph。

生产部署前轮换开发沟通中曾暴露的数据库、Redis、对象存储和模型密钥，并使用 HTTPS、防火墙和最小权限账号。
