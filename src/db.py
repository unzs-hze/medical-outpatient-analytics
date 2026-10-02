"""轻量 MySQL 客户端封装。

本机 pip 被代理阻断，装不上 PyMySQL / mysql-connector，所以这里直接驱动
本机已安装的 mysql 命令行客户端完成建表、批量写入与查询。

接口刻意做成和 DB-API 类似的形状（connect / execute / query / executemany），
将来装上 PyMySQL 后只改本文件即可，上层代码不用动。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Iterable, Sequence

_MYSQL_WARNING = "[Warning] Using a password"


class MySQLError(RuntimeError):
    pass


class MySQLClient:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str | None = None,
        client: str = "mysql",
        charset: str = "utf8mb4",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self.client = client
        self.charset = charset

    # ---------- 底层 ----------
    def _base_args(self) -> list[str]:
        args = [
            self.client,
            f"--host={self.host}",
            f"--port={self.port}",
            f"--user={self.user}",
            f"--default-character-set={self.charset}",
            "--local-infile=1",
        ]
        if self.database:
            args.append(f"--database={self.database}")
        return args

    def _run(self, args: Sequence[str], stdin: str | None = None) -> str:
        env = dict(os.environ)
        env["MYSQL_PWD"] = self.password
        proc = subprocess.run(
            [str(a) for a in args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        # mysql 会把「命令行传密码不安全」的警告打到 stderr，不算错误
        err = "\n".join(
            line for line in proc.stderr.splitlines() if _MYSQL_WARNING not in line
        ).strip()
        if proc.returncode != 0:
            raise MySQLError(err or proc.stdout.strip() or f"mysql exit {proc.returncode}")
        return proc.stdout

    # ---------- 对外接口 ----------
    def execute(self, sql: str) -> None:
        """执行不关心返回结果的 SQL（DDL / DML）。"""
        self._run(self._base_args(), stdin=sql)

    def execute_file(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            self.execute(f.read())

    def query(self, sql: str) -> list[dict[str, str]]:
        """执行查询，返回 dict 列表（字段名 -> 字符串值）。

        --batch 模式下首行是列名、其余是制表符分隔的数据行。
        """
        args = self._base_args() + ["--batch", "--raw"]
        out = self._run(args, stdin=sql)
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines:
            return []
        cols = lines[0].split("\t")
        return [dict(zip(cols, line.split("\t"))) for line in lines[1:]]

    def query_value(self, sql: str, default: Any = None) -> Any:
        rows = self.query(sql)
        if not rows:
            return default
        return list(rows[0].values())[0]

    def load_infile(self, path: str, table: str, columns: Iterable[str],
                    ignore_lines: int = 1) -> None:
        """用 LOAD DATA LOCAL INFILE 高速导入 CSV。

        注意：每次 execute 都是一个独立的 mysql 进程/会话，
        所以这里拿不到 ROW_COUNT()，调用方要另行 COUNT(*)。
        """
        cols = ", ".join(f"`{c}`" for c in columns)
        sql = (
            f"LOAD DATA LOCAL INFILE '{path}' REPLACE INTO TABLE {table} "
            f"CHARACTER SET utf8mb4 FIELDS TERMINATED BY ',' "
            f"ENCLOSED BY '\"' LINES TERMINATED BY '\\n' IGNORE {ignore_lines} LINES ({cols});"
        )
        self.execute(sql)
