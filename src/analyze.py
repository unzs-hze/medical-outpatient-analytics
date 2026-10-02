"""分析层：写结果表 + 复杂 SQL 分析 + 索引效果验证。"""

from __future__ import annotations

from typing import Callable

# ---------- 结果表：科室日汇总 ----------
BUILD_SUMMARY = """
INSERT INTO dept_daily_summary
  (stat_date, dept_id, dept_name, visit_cnt, patient_cnt, total_fee, avg_fee, top_diagnosis)
SELECT stat_date, dept_id, dept_name, visit_cnt, patient_cnt, total_fee, avg_fee, diagnosis
FROM (
  SELECT v.visit_date AS stat_date,
         v.dept_id,
         d.dept_name,
         COUNT(*)                       AS visit_cnt,
         COUNT(DISTINCT v.patient_id)  AS patient_cnt,
         SUM(v.total_fee)              AS total_fee,
         ROUND(AVG(v.total_fee), 2)    AS avg_fee,
         v.diagnosis,
         ROW_NUMBER() OVER (
           PARTITION BY v.visit_date, v.dept_id
           ORDER BY COUNT(*) DESC, v.diagnosis
         ) AS rn
  FROM visit v
  JOIN department d ON d.dept_id = v.dept_id
  GROUP BY v.visit_date, v.dept_id, d.dept_name, v.diagnosis
) t
WHERE rn = 1
ON DUPLICATE KEY UPDATE
  visit_cnt = VALUES(visit_cnt), patient_cnt = VALUES(patient_cnt),
  total_fee = VALUES(total_fee), avg_fee = VALUES(avg_fee),
  top_diagnosis = VALUES(top_diagnosis);
"""

# ---------- 分析查询 ----------
QUERIES = {
    # 1) 分组聚合：科室维度经营概览
    "dept_overview": """
        SELECT d.dept_name,
               COUNT(*)                        AS visit_cnt,
               COUNT(DISTINCT v.patient_id)    AS patient_cnt,
               ROUND(SUM(v.total_fee), 2)      AS total_fee,
               ROUND(AVG(v.total_fee), 2)      AS avg_fee
        FROM visit v JOIN department d ON d.dept_id = v.dept_id
        GROUP BY d.dept_id, d.dept_name
        ORDER BY total_fee DESC;
    """,

    # 2) 窗口函数：收入排名与占全院比重
    "dept_rank": """
        SELECT dept_name, total_fee,
               RANK() OVER (ORDER BY total_fee DESC) AS fee_rank,
               ROUND(total_fee / SUM(total_fee) OVER () * 100, 2) AS pct
        FROM (
          SELECT d.dept_name, SUM(v.total_fee) AS total_fee
          FROM visit v JOIN department d ON d.dept_id = v.dept_id
          GROUP BY d.dept_id, d.dept_name
        ) s
        ORDER BY fee_rank;
    """,

    # 3) CTE + ROW_NUMBER：每位患者最近一次就诊
    "last_visit": """
        WITH ranked AS (
          SELECT patient_id, visit_date, dept_id, diagnosis, total_fee,
                 ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY visit_date DESC) AS rn
          FROM visit
        )
        SELECT p.patient_name, r.visit_date AS last_visit_date,
               d.dept_name, r.diagnosis, r.total_fee
        FROM ranked r
        JOIN patient p    ON p.patient_id = r.patient_id
        JOIN department d ON d.dept_id = r.dept_id
        WHERE r.rn = 1
        ORDER BY r.visit_date DESC
        LIMIT 15;
    """,

    # 4) LAG：全院日门诊量的环比变化
    "day_over_day": """
        WITH daily AS (
          SELECT visit_date, COUNT(*) AS cnt
          FROM visit GROUP BY visit_date
        )
        SELECT visit_date, cnt,
               LAG(cnt) OVER (ORDER BY visit_date) AS prev_cnt,
               ROUND((cnt - LAG(cnt) OVER (ORDER BY visit_date)) * 100.0
                     / NULLIF(LAG(cnt) OVER (ORDER BY visit_date), 0), 2) AS dod_pct
        FROM daily
        ORDER BY visit_date DESC
        LIMIT 15;
    """,

    # 5) 复诊率：同一患者 30 天内二次就诊占比
    "revisit_rate": """
        SELECT ROUND(SUM(is_revisit) * 100.0 / COUNT(*), 2) AS revisit_pct
        FROM (
          SELECT v.visit_id,
                 EXISTS (
                   SELECT 1 FROM visit p
                   WHERE p.patient_id = v.patient_id
                     AND p.visit_date < v.visit_date
                     AND p.visit_date >= v.visit_date - INTERVAL 30 DAY
                 ) AS is_revisit
          FROM visit v
        ) t;
    """,

    # 6) 年龄段分布
    "age_dist": """
        SELECT CASE
                 WHEN age_at_visit < 18 THEN '未成年(<18)'
                 WHEN age_at_visit < 35 THEN '青年(18-34)'
                 WHEN age_at_visit < 50 THEN '中年(35-49)'
                 WHEN age_at_visit < 65 THEN '中老年(50-64)'
                 ELSE '老年(65+)' END AS age_group,
               COUNT(*)                     AS visit_cnt,
               ROUND(AVG(total_fee), 2)     AS avg_fee
        FROM v_visit_detail
        GROUP BY age_group
        ORDER BY visit_cnt DESC;
    """,

    # 7) 月度趋势
    "monthly_trend": """
        SELECT DATE_FORMAT(visit_date, '%Y-%m') AS ym,
               COUNT(*)                   AS visit_cnt,
               ROUND(SUM(total_fee), 2)   AS total_fee
        FROM visit
        GROUP BY ym
        ORDER BY ym;
    """,
}


def build_summary(db, log: Callable[[str], None] | None = None) -> int:
    db.execute("TRUNCATE TABLE dept_daily_summary;")
    db.execute(BUILD_SUMMARY)
    n = int(db.query_value("SELECT COUNT(*) FROM dept_daily_summary;") or 0)
    if log:
        log(f"  科室日汇总写入 {n} 行")
    return n


def run_queries(db) -> dict[str, list[dict]]:
    return {name: db.query(sql) for name, sql in QUERIES.items()}


def index_experiment(db) -> dict:
    """对比「有组合索引」和「无索引」下同一条查询的执行计划。"""
    sql = """
        SELECT dept_id, COUNT(*) AS cnt, ROUND(SUM(total_fee),2) AS fee
        FROM visit
        WHERE dept_id = 3 AND visit_date BETWEEN '2026-03-01' AND '2026-03-31'
        GROUP BY dept_id;
    """

    def plan() -> dict:
        r = db.query("EXPLAIN " + sql)[0]
        return {"type": r.get("type"), "key": r.get("key"), "rows": r.get("rows")}

    def ensure_indexes() -> None:
        db.execute(
            "ALTER TABLE visit "
            "ADD INDEX idx_visit_date (visit_date), "
            "ADD INDEX idx_visit_dept_date (dept_id, visit_date);"
        )

    def drop_indexes() -> None:
        for name in ("idx_visit_dept_date", "idx_visit_date"):
            try:
                db.execute(f"ALTER TABLE visit DROP INDEX {name};")
            except Exception:
                pass      # 索引不存在时忽略

    drop_indexes()
    ensure_indexes()
    with_idx = plan()

    # 把两个索引都删掉，逼出全表扫描，对比才有意义
    drop_indexes()
    without_idx = plan()

    ensure_indexes()
    return {"with_index": with_idx, "without_index": without_idx, "sql": sql.strip()}
