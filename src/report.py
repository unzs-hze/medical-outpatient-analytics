"""生成 HTML 日报 + CSV 导出。不依赖任何第三方库，图表用纯 CSS 条形实现。"""

from __future__ import annotations

import csv
import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "output"


def _fmt_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_int(v) -> str:
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return str(v)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    tb = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table>"


def _bars(items: list[tuple[str, float]], unit: str = "") -> str:
    """纯 CSS 条形图。"""
    if not items:
        return ""
    mx = max(v for _, v in items) or 1
    out = []
    for name, val in items:
        pct = float(val) / mx * 100
        out.append(
            f'<div class="bar-row"><span class="bar-label">{html.escape(name)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span>'
            f'<span class="bar-val">{html.escape(_fmt_money(val) if unit == "money" else _fmt_int(val))}</span></div>'
        )
    return '<div class="bars">' + "".join(out) + "</div>"


CSS = """
:root{--ink:#1f2430;--sub:#5b6472;--line:#e6eaf1;--brand:#2e51db;--soft:#eef2ff;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;color:var(--ink);
     background:#f4f6fa;padding:24px;line-height:1.6;}
.wrap{max-width:1000px;margin:0 auto;background:#fff;padding:28px 32px;
      border-radius:8px;box-shadow:0 4px 18px rgba(31,36,48,.08);}
h1{font-size:22px;margin-bottom:4px;}
.meta{font-size:12.5px;color:var(--sub);margin-bottom:18px;}
h2{font-size:16px;margin:22px 0 10px;padding-bottom:6px;
   border-bottom:2px solid var(--soft);color:var(--brand);}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:6px;}
.kpi{flex:1;min-width:140px;background:var(--soft);border-radius:6px;padding:10px 12px;}
.kpi .l{font-size:12px;color:var(--sub);}
.kpi .v{font-size:19px;font-weight:700;color:var(--brand);}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px;}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;}
th{background:#f7f9ff;font-weight:700;}
tbody tr:nth-child(even){background:#fafbfe;}
.bars{margin-top:8px;}
.bar-row{display:flex;align-items:center;gap:8px;font-size:12px;margin-bottom:4px;}
.bar-label{width:96px;flex:none;color:var(--sub);}
.bar-track{flex:1;background:#eef1f7;border-radius:3px;height:14px;overflow:hidden;}
.bar-fill{display:block;height:100%;background:linear-gradient(90deg,#4a6cf7,#2e51db);}
.bar-val{width:110px;flex:none;text-align:right;font-variant-numeric:tabular-nums;}
.note{font-size:12px;color:var(--sub);margin-top:6px;}
code{background:#f2f4f8;padding:1px 5px;border-radius:3px;font-size:12px;}
"""


def build_report(ctx: dict, path: Path | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or OUT_DIR / "report.html"
    r = ctx["results"]
    ov = ctx["overview"]

    kpis = "".join(
        f'<div class="kpi"><div class="l">{lab}</div><div class="v">{val}</div></div>'
        for lab, val in [
            ("就诊总人次", _fmt_int(ov["visit_cnt"])),
            ("覆盖患者数", _fmt_int(ov["patient_cnt"])),
            ("医疗总收入(元)", _fmt_money(ov["total_fee"])),
            ("人均次费用(元)", _fmt_money(ov["avg_fee"])),
            ("数据日期范围", f'{ov["min_date"]} ~ {ov["max_date"]}'),
        ]
    )

    dept_rows = [
        [d["dept_name"], _fmt_int(d["visit_cnt"]), _fmt_int(d["patient_cnt"]),
         _fmt_money(d["total_fee"]), _fmt_money(d["avg_fee"])]
        for d in r["dept_overview"]
    ]
    dept_bars = _bars([(d["dept_name"], float(d["total_fee"])) for d in r["dept_overview"]], "money")

    rank_rows = [
        [x["dept_name"], _fmt_money(x["total_fee"]), x["fee_rank"], f'{x["pct"]}%']
        for x in r["dept_rank"]
    ]

    trend_rows = [[m["ym"], _fmt_int(m["visit_cnt"]), _fmt_money(m["total_fee"])]
                  for m in r["monthly_trend"]]
    trend_bars = _bars([(m["ym"], float(m["visit_cnt"])) for m in r["monthly_trend"]])

    age_rows = [[a["age_group"], _fmt_int(a["visit_cnt"]), _fmt_money(a["avg_fee"])]
                for a in r["age_dist"]]

    last_rows = [
        [x["patient_name"], x["last_visit_date"], x["dept_name"], x["diagnosis"],
         _fmt_money(x["total_fee"])]
        for x in r["last_visit"]
    ]

    dod_rows = [
        [x["visit_date"], _fmt_int(x["cnt"]), _fmt_int(x["prev_cnt"] or 0),
         f'{x["dod_pct"]}%' if x["dod_pct"] not in (None, "None") else "-"]
        for x in r["day_over_day"]
    ]

    revisit = r["revisit_rate"][0]["revisit_pct"] if r["revisit_rate"] else "-"

    b = ctx["bench"]
    idx = ctx["index_exp"]
    etl = ctx["etl"]

    ratio = ""
    if b.get("infile", -1) > 0 and b.get("batch", 0) > 0:
        ratio = f"　→　LOAD DATA 约为批量 INSERT 的 {b['batch'] / b['infile']:.1f} 倍"

    bench_rows = [
        ["批量多值 INSERT（每批 1000 行）", f'{b["batch"]:.3f} s', "全量实测" + ratio],
        ["LOAD DATA LOCAL INFILE",
         (f'{b["infile"]:.3f} s' if b.get("infile", -1) >= 0 else "不可用"),
         "全量实测" + ("" if b.get("infile", -1) >= 0
                     else f'（{b.get("infile_error", "")[:60]}）')],
        ["逐行 INSERT（仅 100 行采样）", f'{b["single_per_100"]:.3f} s',
         f'线性外推全量约 {b["single_est_full"]:.0f} s，故不可用于全量'],
    ]

    idx_rows = [
        ["无索引", idx["without_index"]["type"], idx["without_index"]["key"] or "NULL",
         _fmt_int(idx["without_index"]["rows"])],
        ["组合索引 (dept_id, visit_date)", idx["with_index"]["type"],
         idx["with_index"]["key"] or "NULL", _fmt_int(idx["with_index"]["rows"])],
    ]

    etl_rows = [
        ["患者", _fmt_int(etl["raw"]["patient"]), _fmt_int(etl["clean"]["patient"]),
         _fmt_int(etl["raw"]["patient"] - etl["clean"]["patient"])],
        ["就诊", _fmt_int(etl["raw"]["visit"]), _fmt_int(etl["clean"]["visit"]),
         _fmt_int(etl["raw"]["visit"] - etl["clean"]["visit"])],
        ["费用明细", _fmt_int(etl["raw"]["fee"]), _fmt_int(etl["clean"]["fee"]),
         _fmt_int(etl["raw"]["fee"] - etl["clean"]["fee"])],
    ]

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>门诊运营分析日报</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>门诊运营分析日报</h1>
<div class="meta">数据源：MySQL 8.0 · outpatient_analytics　|
生成时间：{html.escape(ctx['generated_at'])}　|　ETL 批次 #{ctx['batch_id']}</div>

<div class="kpis">{kpis}</div>

<h2>1. 科室经营概览</h2>
{_table(['科室','就诊人次','患者数','总收入(元)','人均次费用(元)'], dept_rows)}
{dept_bars}

<h2>2. 科室收入排名与占比（窗口函数 RANK / SUM OVER）</h2>
{_table(['科室','总收入(元)','排名','占全院比重'], rank_rows)}

<h2>3. 月度趋势</h2>
{_table(['月份','就诊人次','收入(元)'], trend_rows)}
{trend_bars}

<h2>4. 患者年龄段分布</h2>
{_table(['年龄段','就诊人次','人均次费用(元)'], age_rows)}
<div class="note">30 天内二次就诊占比（复诊率）：<b>{revisit}%</b></div>

<h2>5. 最近就诊记录（CTE + ROW_NUMBER 取每人最近一次）</h2>
{_table(['患者','最近就诊日','科室','诊断','费用(元)'], last_rows)}

<h2>6. 日门诊量环比（LAG 窗口函数）</h2>
{_table(['日期','就诊人次','前一日','环比'], dod_rows)}

<h2>7. ETL 清洗结果</h2>
{_table(['实体','原始行数','入仓行数','拦截行数'], etl_rows)}
<div class="note">清洗规则：手机号正则校验、性别枚举校验、日期区间校验、金额非负、
外键存在性校验（EXISTS）、业务唯一键去重（ON DUPLICATE KEY）。</div>

<h2>8. 写入方式性能对比</h2>
{_table(['写入方式','耗时','说明'], bench_rows)}
<div class="note">说明：本项目用 Python 驱动本机 <code>mysql</code> 命令行客户端，
每条 SQL 都是一次独立连接，因此「逐行 INSERT」的耗时里绝大部分是进程启动与握手开销，
并非 MySQL 本身慢；换 PyMySQL 长连接后差距会小很多，但「批处理优于逐条提交」的结论不变。</div>

<h2>9. 索引效果验证（EXPLAIN）</h2>
{_table(['场景','type','key','扫描行数'], idx_rows)}
<div class="note">示例查询：<code>{html.escape(' '.join(idx['sql'].split()))}</code></div>

</div></body></html>
"""
    path.write_text(doc, encoding="utf-8")

    # CSV 导出
    for name, headers, rows in [
        ("dept_overview.csv", ['科室', '就诊人次', '患者数', '总收入', '人均次费用'], dept_rows),
        ("monthly_trend.csv", ['月份', '就诊人次', '收入'], trend_rows),
        ("dept_rank.csv", ['科室', '总收入', '排名', '占比'], rank_rows),
    ]:
        with open(OUT_DIR / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
    return path
