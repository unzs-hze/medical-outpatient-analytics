-- =============================================================
--  医疗门诊数据分析  ·  数据库结构
--  目标库：outpatient_analytics    字符集：utf8mb4
--  设计要点：
--    1. 事实表(visit / fee_detail) 与 维度表(department / doctor / patient) 分离
--    2. 事实表按「日期 + 科室」建组合索引，覆盖报表最常用的查询条件
--    3. 金额统一用 DECIMAL(10,2)，不用 FLOAT，避免累计误差
-- =============================================================

CREATE DATABASE IF NOT EXISTS outpatient_analytics
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE outpatient_analytics;

-- ---------- 维度表 ----------
DROP TABLE IF EXISTS department;
CREATE TABLE department (
  dept_id     SMALLINT     NOT NULL COMMENT '科室ID',
  dept_name   VARCHAR(32)  NOT NULL COMMENT '科室名称',
  category    VARCHAR(16)  NOT NULL COMMENT '内科系/外科系/专科',
  PRIMARY KEY (dept_id),
  UNIQUE KEY uk_dept_name (dept_name)
) ENGINE=InnoDB COMMENT='科室维度表';

DROP TABLE IF EXISTS doctor;
CREATE TABLE doctor (
  doctor_id   INT          NOT NULL COMMENT '医生ID',
  doctor_name VARCHAR(32)  NOT NULL,
  dept_id     SMALLINT     NOT NULL,
  title       VARCHAR(16)  NOT NULL COMMENT '主任医师/副主任医师/主治医师/住院医师',
  hired_at    DATE         NOT NULL,
  PRIMARY KEY (doctor_id),
  KEY idx_doctor_dept (dept_id)
) ENGINE=InnoDB COMMENT='医生维度表';

DROP TABLE IF EXISTS patient;
CREATE TABLE patient (
  patient_id   INT          NOT NULL COMMENT '患者ID',
  patient_no   VARCHAR(20)  NOT NULL COMMENT '病历号（业务唯一键）',
  patient_name VARCHAR(32)  NOT NULL,
  gender       CHAR(1)      NOT NULL COMMENT 'M/F',
  birth_date   DATE         NOT NULL,
  phone        VARCHAR(20)  NOT NULL,
  city         VARCHAR(32)  NOT NULL,
  PRIMARY KEY (patient_id),
  UNIQUE KEY uk_patient_no (patient_no),
  KEY idx_patient_phone (phone)
) ENGINE=InnoDB COMMENT='患者维度表';

-- ---------- 事实表 ----------
DROP TABLE IF EXISTS visit;
CREATE TABLE visit (
  visit_id    INT            NOT NULL COMMENT '就诊ID',
  visit_no    VARCHAR(24)    NOT NULL COMMENT '就诊流水号（业务唯一键）',
  patient_id  INT            NOT NULL,
  doctor_id   INT            NOT NULL,
  dept_id     SMALLINT       NOT NULL,
  visit_date  DATE           NOT NULL,
  visit_type  VARCHAR(8)     NOT NULL COMMENT '初诊/复诊/急诊',
  diagnosis   VARCHAR(64)    NOT NULL,
  total_fee   DECIMAL(10,2)  NOT NULL DEFAULT 0 COMMENT '本次就诊总费用',
  PRIMARY KEY (visit_id),
  UNIQUE KEY uk_visit_no (visit_no),
  KEY idx_visit_date (visit_date),
  KEY idx_visit_dept_date (dept_id, visit_date),
  KEY idx_visit_patient (patient_id, visit_date)
) ENGINE=InnoDB COMMENT='就诊事实表';

DROP TABLE IF EXISTS fee_detail;
CREATE TABLE fee_detail (
  fee_id     INT            NOT NULL,
  visit_id   INT            NOT NULL,
  item_type  VARCHAR(8)     NOT NULL COMMENT '挂号/检查/药品/治疗',
  item_name  VARCHAR(48)    NOT NULL,
  amount     DECIMAL(10,2)  NOT NULL,
  PRIMARY KEY (fee_id),
  KEY idx_fee_visit (visit_id)
) ENGINE=InnoDB COMMENT='费用明细事实表';

-- ---------- ETL 与结果表 ----------
DROP TABLE IF EXISTS etl_batch;
CREATE TABLE etl_batch (
  batch_id     INT AUTO_INCREMENT PRIMARY KEY,
  source_rows  INT          NOT NULL DEFAULT 0 COMMENT '读取到的原始行数',
  valid_rows   INT          NOT NULL DEFAULT 0 COMMENT '通过清洗写入的行数',
  invalid_rows INT          NOT NULL DEFAULT 0 COMMENT '被规则拦截的行数',
  started_at   DATETIME     NOT NULL,
  finished_at  DATETIME     NULL,
  status       VARCHAR(12)  NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING/SUCCESS/FAILED',
  message      VARCHAR(255) NULL
) ENGINE=InnoDB COMMENT='ETL批次记录表，用于断点排查与重跑审计';

DROP TABLE IF EXISTS dept_daily_summary;
CREATE TABLE dept_daily_summary (
  stat_date     DATE           NOT NULL,
  dept_id       SMALLINT       NOT NULL,
  dept_name     VARCHAR(32)    NOT NULL,
  visit_cnt     INT            NOT NULL DEFAULT 0,
  patient_cnt   INT            NOT NULL DEFAULT 0,
  total_fee     DECIMAL(12,2)  NOT NULL DEFAULT 0,
  avg_fee       DECIMAL(10,2)  NOT NULL DEFAULT 0,
  top_diagnosis VARCHAR(64)    NULL COMMENT '当日该科室第一诊断',
  PRIMARY KEY (stat_date, dept_id)
) ENGINE=InnoDB COMMENT='科室日汇总结果表（分析层落库，供报表直读）';

-- ---------- 视图：把五张表拉平，报表与临时分析直接查它 ----------
DROP VIEW IF EXISTS v_visit_detail;
CREATE VIEW v_visit_detail AS
SELECT v.visit_id,
       v.visit_no,
       v.visit_date,
       v.visit_type,
       v.diagnosis,
       v.total_fee,
       p.patient_id, p.patient_name, p.gender, p.birth_date, p.city,
       TIMESTAMPDIFF(YEAR, p.birth_date, v.visit_date) AS age_at_visit,
       d.dept_id, d.dept_name, d.category,
       doc.doctor_id, doc.doctor_name, doc.title
FROM visit v
JOIN patient p    ON p.patient_id = v.patient_id
JOIN department d ON d.dept_id    = v.dept_id
JOIN doctor doc   ON doc.doctor_id = v.doctor_id;
