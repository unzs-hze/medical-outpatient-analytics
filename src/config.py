"""读取 config.ini，支持环境变量覆盖密码。"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.ini"


def load_config(path: str | os.PathLike | None = None) -> configparser.ConfigParser:
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"找不到配置文件：{p}")
    cfg = configparser.ConfigParser()
    cfg.read(p, encoding="utf-8")
    # 环境变量优先，方便 CI / 不想把密码写进文件时用
    env_pwd = os.environ.get("MYSQL_PWD")
    if env_pwd:
        cfg.set("mysql", "password", env_pwd)
    return cfg


def get_client(cfg: configparser.ConfigParser | None = None, no_database: bool = False):
    """构造 MySQLClient。

    no_database=True 表示不指定库（执行 CREATE DATABASE 时需要）。
    """
    from db import MySQLClient  # 同目录，run.py 会先把 src 加入 sys.path

    cfg = cfg or load_config()
    m = cfg["mysql"]
    return MySQLClient(
        host=m.get("host", "127.0.0.1"),
        port=m.getint("port", 3306),
        user=m.get("user", "root"),
        password=m.get("password", ""),
        database=None if no_database else m.get("database"),
        client=m.get("client", "mysql"),
        charset=m.get("charset", "utf8mb4"),
    )
