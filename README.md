# 门诊数据分析与报表自动化（MySQL + Python）

一个从「原始业务数据」到「可直接看的运营日报」的完整数据流水线。
纯 Python 标准库 + MySQL 8.0，不依赖任何第三方包。

> 为什么不用 PyMySQL / pandas：本机 pip 被代理阻断装不上第三方包，
> 于是直接用 Python 驱动本机 `mysql` 命令行客户端，并自己封装了一层类 DB-API 的接口
> （`src/db.py`）。接口形状刻意做成 `execute / query / load_infile`，
> 将来装上 PyMySQL 只改这一个文件，上层不用动。

---

## 一、快速开始

```powershell
# 1. 确认 MySQL 服务已启动（本机服务名 MySQL80）
Get-Service MySQL80

# 2. 复制一份配置并填好账号密码
#    （仓库里只有 config.example.ini，config.ini 含密码、已被 .gitignore 排除）
copy config.example.ini config.ini
#    然后编辑 config.ini：mysql.password 填你的密码；
#    mysql.client 若不在 PATH 里，就填 mysql.exe 的完整路径

# 3. 跑全流程
& "C:\Users\聪明的泽恩\.workbuddy\binaries\python\envs\default\Scripts\python.exe" src\run.py

# 常用参数
python src\run.py --rows 20000      # 造多少条就诊记录
python src\run.py --step etl        # 只跑某一步 init/generate/etl/analyze/report
python src\run.py --no-bench        # 跳过写入性能对比（省 15 秒）
```

跑完看三个地方：

- `output/report.html` —— 9 个板块的分析日报（浏览器直接打开）
- `output/*.csv` —— 科室概览 / 月度趋势 / 收入排名的结构化导出
- `output/run.log` —— 全流程执行日志

本机实测（20,000 条就诊记录，约 78,000 行原始数据）：全流程 **30.5 秒**。

---

## 二、目录结构

```
medical-outpatient-analytics/
├── config.ini              数据库连接与 ETL 参数（含明文密码，勿提交公开仓库）
├── sql/
│   └── schema.sql          建库建表：5 张业务表 + 3 张 raw 表 + 2 张结果/批次表 + 1 个视图
├── src/
│   ├── db.py               MySQL 客户端封装（mysql CLI 驱动，类 DB-API）
│   ├── config.py           配置读取，支持 MYSQL_PWD 环境变量覆盖密码
│   ├── generate_data.py    造模拟数据，并刻意注入约 3% 脏数据
│   ├── etl.py              抽取 / 清洗 / 加载 + 三种写入方式性能对比
│   ├── analyze.py          结果表构建 + 7 组分析查询 + 索引效果验证
│   ├── report.py           生成 HTML 日报与 CSV 导出（图表用纯 CSS 实现）
│   └── run.py              流水线入口，支持 --rows / --step / --no-bench
├── data/                   生成的 CSV（原始层输入）
└── output/                 报表、CSV、运行日志
```

---

## 三、数据库设计

```
department ──┬──< doctor
             └──< visit >── patient
                   │
                   └──< fee_detail
```

| 表 | 类型 | 说明 |
|---|---|---|
| `department` / `doctor` / `patient` | 维度表 | 科室、医生、患者 |
| `visit` | 事实表 | 就诊记录，20,000 行 |
| `fee_detail` | 事实表 | 费用明细，51,803 行 |
| `*_raw` | 原始层 | 字段全 VARCHAR，脏数据原样保留，便于复盘与重跑 |
| `dept_daily_summary` | 结果表 | 科室日汇总，报表直读 |
| `etl_batch` | 批次表 | 记录每批的原始/入仓/拦截行数，用于审计与排查 |
| `v_visit_detail` | 视图 | 五表拉平，临时分析直接查它 |

几个刻意的设计选择：

- **金额用 `DECIMAL(10,2)` 而不是 FLOAT** —— 钱不能丢精度，FLOAT 累加会有误差。
- **组合索引 `idx_visit_dept_date (dept_id, visit_date)`** —— 报表最常用的查询条件就是
  「某科室某时间段的量」，组合索引能同时吃掉过滤和分组。
- **事实表先落 `*_raw`** —— 脏数据留在原始层，清洗规则改了可以原样重跑，不用重新造数。

---

## 四、ETL 清洗规则

| 规则 | 作用于 |
|---|---|
| 手机号正则 `^1[3-9][0-9]{9}$` | 患者 |
| 性别枚举 `IN ('M','F')` | 患者 |
| 出生日期可解析且早于今天 | 患者 |
| 就诊日期落在 `2026-01-01 ~ 2026-08-31` | 就诊 |
| 总费用非负 | 就诊 |
| 就诊类型枚举 `IN ('初诊','复诊','急诊')` | 就诊 |
| 外键存在性校验（`EXISTS` 关联患者/医生/科室） | 就诊、费用明细 |
| 业务唯一键去重（`INSERT IGNORE` + `UNIQUE KEY`） | 全部 |

实测：78,469 行原始 → 75,370 行入仓 → 拦截 3,099 行（3.95%）。
拦截量大于注入的脏数据量，是因为被拒患者的就诊记录会级联被外键规则拦掉 —— 这是真实场景里很常见的情况。

---

## 五、几个可以拿去讲的实测结论

### 1. 写入方式：LOAD DATA 比批量 INSERT 快约 11.6 倍

| 写入方式 | 耗时（20,000 行） |
|---|---|
| 逐行 INSERT（100 行采样，线性外推） | ≈ 2,988 s |
| 批量多值 INSERT（每批 1000 行） | 3.179 s |
| `LOAD DATA LOCAL INFILE` | **0.275 s** |

> 注意：本项目每条 SQL 都是一次独立的 `mysql` 进程调用，所以「逐行」那一项的耗时
> 绝大部分是进程启动与握手开销，不是 MySQL 慢。换成长连接驱动后差距会缩小，
> 但「批处理优于逐条提交」的结论不变。

### 2. 组合索引把扫描行数从 19,264 降到 243（约 79 倍）

```sql
SELECT dept_id, COUNT(*), ROUND(SUM(total_fee),2)
FROM visit
WHERE dept_id = 3 AND visit_date BETWEEN '2026-03-01' AND '2026-03-31'
GROUP BY dept_id;
```

| 场景 | EXPLAIN type | 扫描行数 |
|---|---|---|
| 无索引 | ALL（全表扫描） | 19,264 |
| 有 `(dept_id, visit_date)` 组合索引 | range | **243** |

### 3. 复杂 SQL 都落在真实分析需求上

- 分组聚合 + 排序：科室经营概览
- `RANK() OVER` + `SUM() OVER()`：收入排名与占全院比重
- CTE + `ROW_NUMBER() OVER (PARTITION BY ...)`：取每位患者最近一次就诊
- `LAG() OVER`：日门诊量环比
- 相关子查询 + `EXISTS`：30 天复诊率

---

## 六、简历上可以这样写（可直接复制）

> **门诊数据分析与报表自动化**（个人项目，2026.09）
> 技术栈：MySQL 8.0 + Python 3（标准库）
>
> - 设计并落地 5 张业务表的门诊数据仓库（维度/事实分离、金额用 DECIMAL 避免精度丢失），
>   单批次处理约 7.8 万行原始数据。
> - 编写 ETL 清洗流程：手机号正则校验、枚举校验、日期区间校验、外键存在性校验、
>   业务唯一键去重，脏数据拦截率 3.95%，批次结果写入 `etl_batch` 便于审计与重跑。
> - 对比三种写入方式，用 `LOAD DATA LOCAL INFILE` 替代批量 INSERT，
>   2 万行写入耗时从 3.18s 降至 0.275s（约 11.6 倍）。
> - 为「科室 + 日期」查询建立组合索引，EXPLAIN 扫描行数从 19,264 降至 243（约 79 倍），
>   type 由 ALL 转为 range。
> - 用窗口函数（RANK / SUM OVER / ROW_NUMBER / LAG）与 CTE 完成科室收入排名、
>   患者最近就诊、日环比、复诊率等 7 组分析，并自动生成 HTML 日报与 CSV 导出。

---

## 七、已知限制

- 数据是程序生成的模拟数据，不是真实医院数据（也没有真实数据来源）。
- `config.ini` 里有明文密码，别提交到公开仓库；`.gitignore` 已把它排除。
- 依赖本机 `mysql` 命令行客户端；换机器上跑需要改 `config.ini` 里的 `client` 路径。
- Windows 下 `mysql` 客户端打不开含中文字符的路径，所以 `LOAD DATA` 前会把 CSV
  复制到一个纯 ASCII 的临时目录（`C:/Windows/Temp/oaa_etl`），这是 `etl.py` 里那段
  `_stage_dir()` 的由来。
