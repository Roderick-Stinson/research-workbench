# ADR-0007：从头开始，不继承 explog

- 状态：已接受
- 日期：2026-10-11

## 决策
- 新工作台是独立的新工具，**不导入** explog-repo 的旧条目，也不与 explog / lab-template 兼容。
- 只借鉴概念（Experiment / Run / Repeat），代码和数据格式重新设计。
- `<watch_root>/explog*`、`lab-template`、`PLAN.md` 原样保留，**不删除**（"不要了" = 不再维护，不是删除）。
