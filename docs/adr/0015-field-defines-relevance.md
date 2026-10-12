# ADR-0015：由"研究方向 + 种子论文"定义相关性，baseline 自动成为种子

- 状态：已接受
- 日期：2026-10-11

## 决策
- 相关性由**研究方向**定义，不按论文项目拆分。目前只有一个方向：**多智能体架构优化**
  （workflow search、orchestration、拓扑 / graph design，相关的都算）。
- 方向 = 一段文字描述 + 子主题 + **种子论文**。
- **baseline 一律是种子**：配置组标了 `baseline` 且关联了论文的，自动进入种子集。
  现成的一批：`<baseline_dir>/` 下的 AFlow、ADAS、MaAS、GDesigner、ARG-Designer。

## 理由
本人明确给出了一个领域；baseline 是本人最确定"相关"的论文，又和复现实验天然相连。

## 待定
- 以后出现第二个方向时，是并列多个方向，还是挂到项目下。
