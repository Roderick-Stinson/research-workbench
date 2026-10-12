---
name: idea-explainer
description: 把实验工作台里的一个 IDEA（针对某个 Problem 的解决方式）写成单文件 HTML，按 paper-explainer 的前 6 步：动机 → 现有工作分类 → 现有工作的问题（配图）→ 根因 → 最直接解法 → Idea 实现细节。用户说"把这个 idea 讲清楚 / idea 文档"，或工作台构思流程需要时使用。
---

# IDEA 说明文档（paper-explainer 前 6 步）

写法、排版硬性规则、组件、公式解释格式**全部沿用** `../paper-explainer/SKILL.md`，模板用 `../paper-explainer/assets/template.html`。
不同点：

1. **只写 Step 1–6**（ADR-0018）：删掉模板里 Step 7（Claims）与 Step 8（实验设计）两节及其目录项；
   Claims 和实验设计在论文项目页里（claim → 配置组 → Run），不写进 IDEA 文档。
2. hero 里的 P → R → F → I 链：P、R 来自对应的 Problem 文档；F 是朴素解法；I 是这个 IDEA。
3. **Step 2 现有工作分类**：用工作台领域地图（`$WB_HOME/map/` 当前版本）里的类别与代表工作，引用写论文 id + 标题 + arXiv 链接，只能引用库里真实存在的论文。
4. **Step 3 / 4 的图与例子必须直接来自 Problem 文档的真实证据**（同一张图或同一组样本），不可以重新编例子。
5. **Step 6 Idea 实现细节**：这是还没做过的方法，要写到"可以照着实现"的程度：
   输入输出、模块、伪代码、核心公式（按 paper-explainer 的 `.formula` 格式逐项解释取值范围与设计理由）、
   需要的资源（模型、数据、GPU 时间的估计，标"估计"）、和最接近的已有工作的区别（引用库里论文）。
6. 文末加一节「风险与待验证」：审稿意见里的主要弱点（如有 review.json）+ 每个风险用什么最小实验检验。

质检同 paper-explainer：无 `{{...}}` 占位符、引用闭合、KaTeX 无报错。
