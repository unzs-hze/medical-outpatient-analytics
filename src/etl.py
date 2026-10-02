"""ETL：把生成的 CSV 抽取到 MySQL，清洗后写入正式表，并记录批次与写入性能。

流程
    抽取  CSV -> *_raw 表（全 VARCHAR，先不校验，保证原始数据不丢）
    清洗  *_raw -> 正式表（靠 SQL WHERE 做规则过滤 + 唯一键去重）
    加载  结果写入 dept_daily_summary，批次写入 etl_batch

为什么用 *_raw 过渡：脏数据留在原始层可复盘，清洗规则改了能直接重跑，不用重新造数据。
"""

from __future__ import annotations

import csv
import os
import shutil
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# ------------------------------------------------------------------
# 1. 原始层：字段全部放宽，保证 CSV 能原样进来
# ------------------------------------------------------------------
RAW_DDL = """
DROP TABLE IF EXISTS patient_raw;
CREATE TABLE patient_raw (
  patient_id VARCHAR(20), patient_no VARCHAR(30), patient_name VARCHAR(64),
  gender VARCHAR(4), birth_date VARCHAR(20), phone VARCHAR(30), city VARCHAR(32)
) ENGINE=InnoDB;

DROP TABLE IF EXISTS visit_raw;
CREATE TABLE visit_raw (
  visit_id VARCHAR(20), visit_no VARCHAR(32), patient_id VARCHAR(20),
  doctor_id VARCHAR(20), dept_id VARCHAR(10), visit_date VARCHAR(20),
  visit_type VARCHAR(8), diagnosis VARCHAR(64), total_fee VARCHAR(20)
) ENGINE=InnoDB;

DROP TABLE IF EXISTS fee_raw;
CREATE TABLE fee_raw (
  fee_id VARCHAR(20), visit_id VARCHAR(20), item_type VARCHAR(8),
  item_name VARCHAR(48), amount VARCHAR(20)
) ENGINE=InnoDB;
"""

TABLES = [
    ("department", "department.csv", ["dept_id", "dept_name", "category"]),
    ("doctor", "doctor.csv", ["doctor_id", "doctor_name", "dept_id", "title", "hired_at"]),
    ("patient", "patient.csv", ["patient_id", "patient_no", "patient_name",
                                "gender", "birth_date", "phone", "city"]),
    ("visit", "visit.csv", ["visit_id", "visit_no", "patient_id", "doctor_id",
                            "dept_id", "visit_date", "visit_type", "diagnosis", "total_fee"]),
    ("fee_detail", "fee_detail.csv", ["fee_id", "visit_id", "item_type",
                                      "item_name", "amount"]),
]


# mysql 客户端在 Windows 上打不开含非 ASCII 字符的路径（会报 OS errno 2），
# 所以先把 CSV 复制到一个纯 ASCII 的临时目录再 LOAD DATA。
ASCII_STAGE_CANDIDATES = ["C:/Windows/Temp/oaa_etl", "C:/Temp/oaa_etl", "C:/oaa_etl"]
_STAGE_DIR: str | None = None


def _stage_dir() -> str | None:
    global _STAGE_DIR
    if _STAGE_DIR is not None:
        return _STAGE_DIR
    for cand in ASCII_STAGE_CANDIDATES:
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, "_probe.tmp")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            _STAGE_DIR = cand
            return cand
        except Exception:
            continue
    _STAGE_DIR = ""
    return None


def _csv_path(name: str) -> str:
    """返回可供 LOAD DATA 使用的路径（正斜杠 + 纯 ASCII 目录）。"""
    src = DATA_DIR / name
    stage = _stage_dir()
    if stage:
        dst = os.path.join(stage, name)
        shutil.copyfile(src, dst)
        return dst.replace("\\", "/")
    return src.as_posix()


def init_schema(db) -> None:
    db.execute_file(str(ROOT / "sql" / "schema.sql"))
    db.execute(RAW_DDL)


# ------------------------------------------------------------------
# 2. 抽取：三种写入方式，顺便做性能对比
# ------------------------------------------------------------------
def _load_via_infile(db, table: str, csv_name: str, columns: list[str]) -> int:
    return db.load_infile(_csv_path(csv_name), table, columns)


def _load_via_batch_insert(db, table: str, csv_name: str, columns: list[str],
                           batch_size: int = 1000) -> int:
    """拼多值 INSERT，每批只跑一次 mysql 进程。"""
    path = DATA_DIR / csv_name
    cols = ", ".join(f"`{c}`" for c in columns)
    total = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        buf: list[list[str]] = []
        for row in reader:
            buf.append(row)
            if len(buf) >= batch_size:
                total += _flush(db, table, cols, buf)
                buf = []
        if buf:
            total += _flush(db, table, cols, buf)
    return total


def _flush(db, table: str, cols: str, buf: list[list[str]]) -> int:
    def esc(v: str) -> str:
        return "'" + v.replace("\\", "\\\\").replace("'", "''") + "'"
    values = ",\n".join("(" + ",".join(esc(c) for c in row) + ")" for row in buf)
    db.execute(f"INSERT INTO {table} ({cols}) VALUES\n{values};")
    return len(buf)


def _load_via_single_insert(db, table: str, csv_name: str, columns: list[str],
                            limit: int = 100) -> int:
    """逐行 INSERT。太慢，只跑前 limit 行用于性能采样。"""
    path = DATA_DIR / csv_name
    cols = ", ".join(f"`{c}`" for c in columns)
    n = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if n >= limit:
                break
            _flush(db, table, cols, [row])
            n += 1
    return n


def benchmark_write(db, table: str, csv_name: str, columns: list[str],
                    batch_size: int, total_rows: int) -> dict:
    """对比 逐行 / 批量 / LOAD DATA 三种写入方式，返回耗时（秒）。"""
    res: dict[str, float] = {}

    db.execute(f"TRUNCATE TABLE {table};")
    t0 = time.perf_counter()
    n = _load_via_single_insert(db, table, csv_name, columns, limit=100)
    res["single_per_100"] = time.perf_counter() - t0
    res["single_est_full"] = res["single_per_100"] * total_rows / max(n, 1)

    db.execute(f"TRUNCATE TABLE {table};")
    t0 = time.perf_counter()
    _load_via_batch_insert(db, table, csv_name, columns, batch_size)
    res["batch"] = time.perf_counter() - t0

    db.execute(f"TRUNCATE TABLE {table};")
    t0 = time.perf_counter()
    try:
        _load_via_infile(db, table, csv_name, columns)
        res["infile"] = time.perf_counter() - t0
    except Exception as e:                       # 服务器未开 local_infile 时降级
        res["infile"] = -1
        res["infile_error"] = str(e)
        _load_via_batch_insert(db, table, csv_name, columns, batch_size)
    return res


# ------------------------------------------------------------------
# 3. 清洗规则
# ------------------------------------------------------------------
CLEAN_PATIENT = """
INSERT IGNORE INTO patient (patient_id, patient_no, patient_name, gender, birth_date, phone, city)
SELECT CAST(patient_id AS UNSIGNED), patient_no, patient_name, gender,
       STR_TO_DATE(birth_date, '%Y-%m-%d'), phone, city
FROM patient_raw
WHERE patient_no IS NOT NULL AND patient_no <> ''
  AND gender IN ('M','F')
  AND phone REGEXP '^1[3-9][0-9]{9}$'
  AND STR_TO_DATE(birth_date, '%Y-%m-%d') IS NOT NULL
  AND STR_TO_DATE(birth_date, '%Y-%m-%d') < CURDATE()
;
"""

CLEAN_VISIT = """
INSERT IGNORE INTO visit (visit_id, visit_no, patient_id, doctor_id, dept_id,
                   visit_date, visit_type, diagnosis, total_fee)
SELECT CAST(v.visit_id AS UNSIGNED), v.visit_no,
       CAST(v.patient_id AS UNSIGNED), CAST(v.doctor_id AS UNSIGNED),
       CAST(v.dept_id AS UNSIGNED),
       STR_TO_DATE(v.visit_date, '%Y-%m-%d'), v.visit_type, v.diagnosis,
       CAST(v.total_fee AS DECIMAL(10,2))
FROM visit_raw v
WHERE STR_TO_DATE(v.visit_date, '%Y-%m-%d') BETWEEN '2026-01-01' AND '2026-08-31'
  AND CAST(v.total_fee AS DECIMAL(10,2)) >= 0
  AND v.visit_type IN ('初诊','复诊','急诊')
  AND EXISTS (SELECT 1 FROM patient p WHERE p.patient_id = CAST(v.patient_id AS UNSIGNED))
  AND EXISTS (SELECT 1 FROM doctor  d WHERE d.doctor_id = CAST(v.doctor_id AS UNSIGNED))
  AND EXISTS (SELECT 1 FROM department dp WHERE dp.dept_id = CAST(v.dept_id AS UNSIGNED));
"""

CLEAN_FEE = """
INSERT IGNORE INTO fee_detail (fee_id, visit_id, item_type, item_name, amount)
SELECT CAST(f.fee_id AS UNSIGNED), CAST(f.visit_id AS UNSIGNED),
       f.item_type, f.item_name, CAST(f.amount AS DECIMAL(10,2))
FROM fee_raw f
WHERE CAST(f.amount AS DECIMAL(10,2)) > 0
  AND EXISTS (SELECT 1 FROM visit v WHERE v.visit_id = CAST(f.visit_id AS UNSIGNED));
"""


# 正式表名 -> 原始层表名（维度表直接进正式表，事实表先落原始层）
RAW_TABLE = {
    "department": "department",
    "doctor": "doctor",
    "patient": "patient_raw",
    "visit": "visit_raw",
    "fee_detail": "fee_raw",
}


def run_etl(db, log: Callable[[str], None]) -> dict:
    # 载入原始层（原始数据不校验，保证不丢）
    for table, csv_name, cols in TABLES:
        raw = RAW_TABLE[table]
        db.execute(f"TRUNCATE TABLE {raw};")
        try:
            db.load_infile(_csv_path(csv_name), raw, cols)
        except Exception as e:
            log(f"  LOAD DATA 不可用（{str(e)[:80]}），降级为分批 INSERT")
            _load_via_batch_insert(db, raw, csv_name, cols, 1000)

    raw_counts = {
        "patient": int(db.query_value("SELECT COUNT(*) FROM patient_raw;") or 0),
        "visit": int(db.query_value("SELECT COUNT(*) FROM visit_raw;") or 0),
        "fee": int(db.query_value("SELECT COUNT(*) FROM fee_raw;") or 0),
    }

    # 正式表清空后按「患者 -> 就诊 -> 费用」的顺序写入，保证外键语义成立
    db.execute("SET FOREIGN_KEY_CHECKS=0;")
    for t in ("fee_detail", "visit", "patient", "doctor", "department"):
        db.execute(f"TRUNCATE TABLE {t};")
    for table, csv_name, cols in TABLES:
        if table not in ("department", "doctor"):
            continue
        try:
            db.load_infile(_csv_path(csv_name), table, cols)
        except Exception:
            _load_via_batch_insert(db, table, csv_name, cols, 1000)

    db.execute(CLEAN_PATIENT)
    db.execute(CLEAN_VISIT)
    db.execute(CLEAN_FEE)
    db.execute("SET FOREIGN_KEY_CHECKS=1;")

    clean_counts = {
        "patient": int(db.query_value("SELECT COUNT(*) FROM patient;") or 0),
        "visit": int(db.query_value("SELECT COUNT(*) FROM visit;") or 0),
        "fee": int(db.query_value("SELECT COUNT(*) FROM fee_detail;") or 0),
    }
    return {"raw": raw_counts, "clean": clean_counts}


def record_batch_start(db) -> int:
    db.execute(
        "INSERT INTO etl_batch (source_rows, valid_rows, invalid_rows, started_at, status) "
        "VALUES (0,0,0,NOW(),'RUNNING');"
    )
    return int(db.query_value("SELECT LAST_INSERT_ID();") or 0)


def record_batch_finish(db, batch_id: int, source_rows: int, valid_rows: int,
                        invalid_rows: int, message: str = "") -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "''")
    db.execute(
        f"UPDATE etl_batch SET source_rows={source_rows}, valid_rows={valid_rows}, "
        f"invalid_rows={invalid_rows}, finished_at=NOW(), status='SUCCESS', "
        f"message='{esc(message)}' WHERE batch_id={batch_id};"
    )
