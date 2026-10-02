"""生成模拟门诊业务数据（含刻意注入的脏数据），输出为 CSV。

刻意注入脏数据是为了让 ETL 的清洗规则有东西可洗，也方便验证规则是否真的生效。
随机种子固定，所以每次生成的数据完全一致，方便复现和写测试。
"""

from __future__ import annotations

import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

DEPARTMENTS = [
    (1, "心血管内科", "内科系"), (2, "呼吸内科", "内科系"), (3, "消化内科", "内科系"),
    (4, "普通外科", "外科系"),   (5, "骨科", "外科系"),     (6, "儿科", "专科"),
    (7, "妇产科", "专科"),       (8, "眼科", "专科"),       (9, "口腔科", "专科"),
    (10, "皮肤科", "专科"),
]

# 每个科室的常见诊断，用于让分析结果看起来合理
DIAGNOSIS = {
    1: ["高血压", "冠心病", "心律失常", "心力衰竭"],
    2: ["上呼吸道感染", "支气管炎", "肺炎", "慢性咳嗽"],
    3: ["胃炎", "胃溃疡", "肠易激综合征", "胆囊炎"],
    4: ["阑尾炎", "腹股沟疝", "甲状腺结节", "体表肿物"],
    5: ["腰椎间盘突出", "踝关节扭伤", "骨折术后复查", "颈椎病"],
    6: ["小儿发热", "手足口病", "小儿腹泻", "过敏性咳嗽"],
    7: ["孕期检查", "月经不调", "子宫肌瘤", "阴道炎"],
    8: ["结膜炎", "近视复查", "白内障术前检查", "干眼症"],
    9: ["龋齿", "牙周炎", "智齿冠周炎", "牙齿矫正复查"],
    10: ["湿疹", "痤疮", "荨麻疹", "带状疱疹"],
}

SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡林郭何高罗"
GIVEN = "伟芳娜秀敏静丽强磊洋勇艳杰娟涛明超秀霞平刚桂英建华文博思远雨欣"
TITLES = ["住院医师", "主治医师", "主治医师", "副主任医师", "主任医师"]
CITIES = ["西安市", "咸阳市", "宝鸡市", "渭南市", "汉中市", "榆林市", "延安市"]
VISIT_TYPES = ["初诊", "复诊", "复诊", "急诊"]
FEE_ITEMS = {
    "挂号": ["普通挂号费", "专家挂号费", "急诊挂号费"],
    "检查": ["血常规", "CT平扫", "B超", "心电图", "MR检查"],
    "药品": ["处方西药", "中成药", "外用药"],
    "治疗": ["清创缝合", "雾化治疗", "理疗", "换药"],
}


def _rand_name(rnd: random.Random) -> str:
    return rnd.choice(SURNAMES) + "".join(rnd.choice(GIVEN) for _ in range(rnd.randint(1, 2)))


def _rand_phone(rnd: random.Random) -> str:
    """生成符合国内号段规则的手机号（第二位 3-9）。"""
    return "1" + rnd.choice("3456789") + "".join(str(rnd.randint(0, 9)) for _ in range(9))


def _rand_date(rnd: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rnd.randint(0, (end - start).days))


def generate(cfg_visit_rows: int, seed: int) -> dict[str, int]:
    rnd = random.Random(seed)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- 科室 ----------
    with open(DATA_DIR / "department.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dept_id", "dept_name", "category"])
        w.writerows(DEPARTMENTS)
    dept_ids = [d[0] for d in DEPARTMENTS]

    # ---------- 医生 ----------
    doctors = []
    for i in range(1, 41):
        dept_id = dept_ids[(i - 1) % len(dept_ids)]
        doctors.append([i, _rand_name(rnd), dept_id, rnd.choice(TITLES),
                        _rand_date(rnd, date(2010, 1, 1), date(2024, 12, 31)).isoformat()])
    with open(DATA_DIR / "doctor.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["doctor_id", "doctor_name", "dept_id", "title", "hired_at"])
        w.writerows(doctors)
    doctor_by_dept: dict[int, list[int]] = {}
    for d in doctors:
        doctor_by_dept.setdefault(d[2], []).append(d[0])

    # ---------- 患者 ----------
    patient_n = max(500, cfg_visit_rows // 3)
    patients = []
    for i in range(1, patient_n + 1):
        patients.append([
            i,
            f"P{20230000 + i}",
            _rand_name(rnd),
            rnd.choice("MF"),
            _rand_date(rnd, date(1945, 1, 1), date(2023, 12, 31)).isoformat(),
            _rand_phone(rnd),
            rnd.choice(CITIES),
        ])
    with open(DATA_DIR / "patient.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["patient_id", "patient_no", "patient_name", "gender",
                    "birth_date", "phone", "city"])
        w.writerows(patients)

    # ---------- 就诊 + 费用明细 ----------
    visits, fees = [], []
    fee_id = 0
    start, end = date(2026, 1, 1), date(2026, 8, 31)
    for i in range(1, cfg_visit_rows + 1):
        dept_id = rnd.choice(dept_ids)
        doctor_id = rnd.choice(doctor_by_dept[dept_id])
        patient_id = rnd.randint(1, patient_n)
        vdate = _rand_date(rnd, start, end)
        vtype = rnd.choice(VISIT_TYPES)
        diagnosis = rnd.choice(DIAGNOSIS[dept_id])

        total = 0.0
        for item_type, names in FEE_ITEMS.items():
            if rnd.random() < (0.95 if item_type == "挂号" else 0.55):
                fee_id += 1
                amount = round(rnd.uniform(8, 480), 2)
                total += amount
                fees.append([fee_id, i, item_type, rnd.choice(names), f"{amount:.2f}"])
        visits.append([i, f"V{2026000000 + i}", patient_id, doctor_id, dept_id,
                       vdate.isoformat(), vtype, diagnosis, f"{total:.2f}"])

    # ---------- 注入脏数据（约占 3%），交给 ETL 清洗 ----------
    dirty = 0
    for _ in range(int(patient_n * 0.012)):               # 患者手机号非法
        rnd.choice(patients)[5] = "12345"
        dirty += 1
    for _ in range(int(cfg_visit_rows * 0.008)):          # 就诊日期为空
        rnd.choice(visits)[5] = ""
        dirty += 1
    for _ in range(int(cfg_visit_rows * 0.006)):          # 费用为负
        rnd.choice(visits)[8] = "-88.00"
        dirty += 1
    for _ in range(int(cfg_visit_rows * 0.005)):          # 流水号重复
        rnd.choice(visits)[1] = visits[0][1]
        dirty += 1
    for _ in range(int(patient_n * 0.01)):                # 患者性别非法
        rnd.choice(patients)[3] = "X"
        dirty += 1

    for name, rows, header in [
        ("visit.csv", visits, ["visit_id", "visit_no", "patient_id", "doctor_id",
                               "dept_id", "visit_date", "visit_type", "diagnosis", "total_fee"]),
        ("patient.csv", patients, ["patient_id", "patient_no", "patient_name", "gender",
                                   "birth_date", "phone", "city"]),
        ("fee_detail.csv", fees, ["fee_id", "visit_id", "item_type", "item_name", "amount"]),
    ]:
        with open(DATA_DIR / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    return {
        "department": len(DEPARTMENTS),
        "doctor": len(doctors),
        "patient": len(patients),
        "visit": len(visits),
        "fee_detail": len(fees),
        "dirty_injected": dirty,
    }


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    s = int(sys.argv[2]) if len(sys.argv) > 2 else 20260921
    print(generate(n, s))
