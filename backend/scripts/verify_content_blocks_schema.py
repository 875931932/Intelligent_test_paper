# -*- coding: utf-8 -*-
"""校验 content_blocks 结构：embedding_text_version 列存在（供空库/既有库各跑一次 init_db 后比对）。

用法：uv run python scripts/verify_content_blocks_schema.py [数据库URL]
不传 URL 时用配置的 DATABASE_URL（云端既有库）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect

from app.config import settings

url = sys.argv[1] if len(sys.argv) > 1 else settings.database_url
engine = create_engine(url)
cols = {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("content_blocks")}
print("database:", engine.url.render_as_string(hide_password=True))
print("has embedding_text_version:", "embedding_text_version" in cols, cols.get("embedding_text_version"))
print("has embedding_model:", "embedding_model" in cols, cols.get("embedding_model"))
print("total columns:", len(cols))
