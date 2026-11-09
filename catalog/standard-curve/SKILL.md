---
name: standard-curve
description: >-
  HPLC/UV 标准曲线建立、LOD/LOQ 估算、未知样浓度反算。Use when user mentions
  标准曲线, calibration curve, LOD, LOQ, 线性回归, 浓度反算, or provides
  standards CSV with concentration and signal/area columns.
---

# 标准曲线分析

## 输入要求

- 标准品 CSV：至少含 **浓度** 和 **响应** 两列（如 `conc_ug_mL`, `area`）
- 可选样品 CSV：含 `sample_id` 和响应列
- 确认单位（默认 µg/mL）并在代码开头声明

## 执行步骤

1. 读取 `@data/` 下 CSV，展示 `head()` 与基本统计
2. `scipy.stats.linregress` 或加权回归（用户要求时）
3. 计算 R²、方程、LOD/LOQ（空信号标准差 × 3.3 / 10 ÷ 斜率，或用户指定方法）
4. 若有样品：反算浓度，保留合理有效数字
5. 绘图：标准点 + 拟合线 + 样品标注；轴标签含单位；标题含关键实验条件
6. 输出到 `output/`：`calibration.png`、`results.csv`
7. 中文 Markdown 摘要：方程、R²、LOD/LOQ、样品结果表

## 输出规范

- 浓度单位写进表头和图轴
- 有效数字与原始数据精度一致（通常 2–3 位）
- 原始 CSV 只读，不修改

## 触发短语

标准曲线、校准曲线、LOD、LOQ、浓度反算、HPLC 定量
