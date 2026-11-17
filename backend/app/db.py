from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def data_dir() -> Path:
    """按环境变量动态解析数据目录，便于测试/容器切换库位。"""
    return Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))


def fixtures_dir() -> Path:
    return Path(os.environ.get("FIXTURES_DIR", DEFAULT_FIXTURES_DIR))


def db_path() -> Path:
    return data_dir() / "app.db"


# 兼容旧引用；运行时读写请用 db_path()/data_dir()/fixtures_dir()（环境变量可能晚于 import 设置）。
DATA_DIR = data_dir()
FIXTURES_DIR = fixtures_dir()
DB_PATH = db_path()


def connect() -> sqlite3.Connection:
    """显式事务模式：BEGIN/COMMIT 由 transaction() 控制。"""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True):
    """原子事务；immediate=True 先取写锁，避免读后写之间的装载插入。"""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
