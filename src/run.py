"""一键跑通全流程：建表 -> 造数 -> ETL -> 分析 -> 报表。

用法：
    python src/run.py                 # 跑全流程
    python src/run.py --rows 5000     # 指定造多少条就诊记录
    python src/run.py --step etl      # 只跑某一步（init/generate/etl/analyze/report）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 让 src 内部互相 import

from config import load_config, get_client          # noqa: E402
import generate_data                                # noqa: E402
import etl as etl_mod                               # noqa: E402
import analyze as analyze_mod                       # noqa: E402
import report as report_mod                         # noqa: E402

LOG_PATH = ROOT / "output" / "run.log"


def log(msg: str) -> None:
    print(msg)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="医疗门诊数据分析流水线")
    p.add_argument("--rows", type=int, default=None, help="生成的就诊记录条数")
    p.add_argument("--seed", type=int, default=None, help="随机种子")
    p.add_argument("--batch-size", type=int, default=None, help="批量插入每批条数")
    p.add_argument("--step", default="all",
                   choices=["all", "init", "generate", "etl", "analyze", "report"])
    p.add_argument("--no-bench", action="store_true", help="跳过写入性能对比（省时间）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    rows = args.rows or cfg["etl"].getint("visit_rows", 20000)
    seed = args.seed or cfg["etl"].getint("seed", 20260921)
    batch_size = args.batch_size or cfg["etl"].getint("batch_size", 1000)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")

    t_all = time.perf_counter()
    log(f"=== 门诊数据分析流水线启动  {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    log(f"数据量：{rows} 条就诊记录　种子：{seed}　批次大小：{batch_size}")

    bootstrap = get_client(cfg, no_database=True)
    bootstrap.execute(
        "CREATE DATABASE IF NOT EXISTS outpatient_analytics DEFAULT CHARSET utf8mb4;"
    )
    # LOAD DATA LOCAL INFILE 默认在服务端是关闭的，打开它才能走高速导入
    try:
        bootstrap.execute("SET GLOBAL local_infile = 1;")
        local_infile = bootstrap.query_value("SELECT @@GLOBAL.local_infile;")
    except Exception as e:
        local_infile = f"0（无法开启：{str(e)[:60]}）"
    log(f"local_infile = {local_infile}")

    db = get_client(cfg)

    # ---------- 1. 建表 ----------
    if args.step in ("all", "init"):
        log("\n[1/5] 初始化数据库结构 ...")
        etl_mod.init_schema(db)
        log("  建表完成（5 张业务表 + 3 张 raw 表 + 2 张结果/批次表 + 1 个视图）")

    # ---------- 2. 造数 ----------
    if args.step in ("all", "generate"):
        log("\n[2/5] 生成模拟业务数据 ...")
        t0 = time.perf_counter()
        stat = generate_data.generate(rows, seed)
        log(f"  department={stat['department']}  doctor={stat['doctor']}  "
            f"patient={stat['patient']}  visit={stat['visit']}  fee_detail={stat['fee_detail']}")
        log(f"  注入脏数据约 {stat['dirty_injected']} 处　耗时 {time.perf_counter()-t0:.2f}s")

    # ---------- 3. ETL ----------
    if args.step in ("all", "etl"):
        log("\n[3/5] ETL 抽取与清洗 ...")
        batch_id = etl_mod.record_batch_start(db)
        t0 = time.perf_counter()
        counts = etl_mod.run_etl(db, log)
        elapsed = time.perf_counter() - t0

        bench = {"single_per_100": 0.0, "single_est_full": 0.0, "batch": 0.0, "infile": -1}
        if not args.no_bench:
            log("  写入方式性能对比（对 visit_raw 全量重放）...")
            bench = etl_mod.benchmark_write(
                db, "visit_raw", "visit.csv",
                ["visit_id", "visit_no", "patient_id", "doctor_id", "dept_id",
                 "visit_date", "visit_type", "diagnosis", "total_fee"],
                batch_size, rows,
            )
            # benchmark 会清空 visit_raw，重新灌回原始数据供后续步骤使用
            try:
                db.load_infile(etl_mod._csv_path("visit.csv"), "visit_raw",
                               ["visit_id", "visit_no", "patient_id", "doctor_id", "dept_id",
                                "visit_date", "visit_type", "diagnosis", "total_fee"])
            except Exception:
                etl_mod._load_via_batch_insert(
                    db, "visit_raw", "visit.csv",
                    ["visit_id", "visit_no", "patient_id", "doctor_id", "dept_id",
                     "visit_date", "visit_type", "diagnosis", "total_fee"], batch_size)

        src = counts["raw"]["patient"] + counts["raw"]["visit"] + counts["raw"]["fee"]
        ok = counts["clean"]["patient"] + counts["clean"]["visit"] + counts["clean"]["fee"]
        etl_mod.record_batch_finish(db, batch_id, src, ok, src - ok,
                                    f"visit_rows={rows}")
        log(f"  原始 {src} 行 -> 入仓 {ok} 行，拦截 {src-ok} 行，耗时 {elapsed:.2f}s")
        if not args.no_bench:
            infile_s = f'{bench["infile"]:.3f}s' if bench.get("infile", -1) >= 0 else "不可用"
            log(f'  逐行(100行) {bench["single_per_100"]:.3f}s / '
                f'批量 {bench["batch"]:.3f}s / LOAD DATA {infile_s}')
    else:
        batch_id = int(db.query_value("SELECT MAX(batch_id) FROM etl_batch;") or 0)
        bench = {"single_per_100": 0.0, "single_est_full": 0.0, "batch": 0.0, "infile": -1}
        counts = {"raw": {"patient": 0, "visit": 0, "fee": 0},
                  "clean": {"patient": 0, "visit": 0, "fee": 0}}

    # ---------- 4. 分析 ----------
    if args.step in ("all", "analyze"):
        log("\n[4/5] 分析与索引验证 ...")
        t0 = time.perf_counter()
        n = analyze_mod.build_summary(db, log)
        results = analyze_mod.run_queries(db)
        log(f"  7 组分析查询完成，耗时 {time.perf_counter()-t0:.2f}s")

        idx = analyze_mod.index_experiment(db)
        log(f'  索引验证：无索引扫描 {idx["without_index"]["rows"]} 行 -> '
            f'有索引扫描 {idx["with_index"]["rows"]} 行 '
            f'(type: {idx["without_index"]["type"]} -> {idx["with_index"]["type"]})')
    else:
        results = analyze_mod.run_queries(db)
        idx = {"with_index": {"type": "-", "key": "-", "rows": 0},
               "without_index": {"type": "-", "key": "-", "rows": 0}, "sql": ""}

    # ---------- 5. 报表 ----------
    if args.step in ("all", "report"):
        log("\n[5/5] 生成报表 ...")
        ov = db.query(
            "SELECT COUNT(*) AS visit_cnt, COUNT(DISTINCT patient_id) AS patient_cnt, "
            "ROUND(SUM(total_fee),2) AS total_fee, ROUND(AVG(total_fee),2) AS avg_fee, "
            "MIN(visit_date) AS min_date, MAX(visit_date) AS max_date FROM visit;"
        )[0]
        path = report_mod.build_report({
            "results": results,
            "overview": ov,
            "bench": bench,
            "index_exp": idx,
            "etl": counts,
            "batch_id": batch_id,
            "generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        })
        log(f"  报表已生成：{path}")
        log(f"  CSV 导出：{report_mod.OUT_DIR / 'dept_overview.csv'} 等 3 个文件")

    log(f"\n=== 全部完成，总耗时 {time.perf_counter()-t_all:.2f}s ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
