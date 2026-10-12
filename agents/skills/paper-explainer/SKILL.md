---
name: paper-explainer
description: 把一篇学术论文做成一个"一步步讲清楚"的单文件 HTML 精读文档（8 步：研究动机 → 现有工作分类 → 现有工作的问题(配图) → 根因 → 最直接解法 → Idea 实现细节(配框架图+公式逐项解释) → Claims → 实验设计与 Claim 对应）。当用户给出论文 PDF/arXiv 链接/标题，并说"精读/讲解/拆解/做成HTML/paper review/解读这篇论文"时使用。
---

# Paper Explainer — 论文 8 步精读 HTML

> `$SKILL_DIR` = 本 SKILL.md 所在目录（随实验工作台仓库部署在 `agents/skills/paper-explainer/`）。

输入：论文 PDF 路径 / arXiv 链接（abs、pdf、html 皆可）/ 论文标题。
输出：一个自包含的 HTML 文件 `outputs/<paper-slug>-explained.html`（在当前工作目录下），用户可在 Preview 里直接看。
默认用中文写正文；专有名词、方法名、指标名保留英文。

## 排版硬性规则（用户明确要求，优先级高于下文各 Step 的写法示例）
1. **能分点就分点**：凡是并列的 2 条以上内容，一律用 `ul.pts`（可加 `.r/.g/.v/.a` 变色圆点）或 `.grid2 > .card` 卡片，不要挤在一段里。表格单元格里也用 `<ul>` 分点。
2. **能用图就用图，文字只做辅助**：每个 Step 至少 1 张图（SVG 示意图 / 数据图）；论文里的数字（主表、消融、成本、机制分析）尽量画成条形图，用 Python 生成 SVG（横向条形，带图例、刻度、数值标签；非零起点要在 caption 里用高亮注明）。图后跟 2–4 条要点，而不是一段话。
3. **禁止大段文字**：单个 `<p>` 不超过 ~2 行（≈80 字）；公式的"整体读法""为什么这样设计"都写成 `<ul>`（`.why > .wl + ul`）。
4. **强调 = 加粗 + 变色 + 背景高亮**：模板 CSS 已让 `section.step b` 自带高亮；按语义选色：`<b class="r">` 问题/负面，`<b class="g">` 好结果，`<b class="v">` 本文设计，`<b class="a">` 注意事项，`<b class="t">` 中性标签，默认 `<b>` 为主色。不要用无样式的裸加粗。
5. 图编号按出现顺序 `图 n-k`，正文引用编号要与之一致；交付前在浏览器里检查 SVG 文本是否越界、KaTeX 无报错。

---

## 工作流

### 0. 先落盘骨架（防止中途中断什么都没有）
```bash
mkdir -p outputs work
cp $SKILL_DIR/assets/template.html outputs/<slug>-explained.html
```
之后每完成一步就把对应的 `{{STEPn_CONTENT}}` 替换掉（用 Edit），不要等全部写完才一次性写入。

### 1. 获取全文（必须读全文，不要只看摘要）
优先级：
1. 本地 PDF 或 PDF 链接：
   `python3 $SKILL_DIR/scripts/extract_pdf.py <pdf或url> work/<slug>.txt`
   脚本会打印各章节所在行号，用 Read 的 offset/limit 分段读完。
2. arXiv 论文也可 WebFetch `https://arxiv.org/html/<id>`（公式保留更好），与 PDF 文本互相补充。
3. 只给了标题：先 WebSearch 找到 arXiv / 会议页面，再走 1 或 2。

**必须读到的部分**：Abstract、Introduction、Related Work、Method（含所有公式）、Experiments（Setup、主表、消融、分析）、Conclusion/Limitation、**References**（引用要从这里取）、Appendix 中的实验设置/超参/数据集细节。

### 2. 先做"笔记提纲"再写 HTML（在脑子里或 work/ 下）
抽出以下骨架，后面 8 步都从它展开，保证前后呼应：
- **P（问题）**：现有工作没考虑到的那件事，一句话。
- **R（根因）**：为什么会有 P —— 结构性/机制性原因（例："多个步骤复用同一个 terminal reward，导致步骤级信号被平均掉"）。
- **F（直接解法）**：针对 R 最朴素的解法（例："做 step-level credit assignment"），以及朴素解法为什么难（成本/噪声/不可得的标注……）。
- **I（本文 Idea）**：本文如何把 F 落地并解决其难点。
- **C1..Cn（Claims）**：本文声称的所有结论。
- **E1..Em（实验）**：每个实验 → 证明哪个 Claim。
检查：P→R→F→I 是否一条线；每个 C 是否至少有一个 E；每个 E 是否服务于某个 C。不闭环就回去重读论文。

### 3. 逐步填写 8 个 Step（要求见下一节）

### 4. 质检（见最后的 Checklist），然后告诉用户文件路径 + 可在 Preview 打开。
完成后删除 `work/` 下的临时文本（它是中间产物），除非用户要求保留。

---

## 8 个 Step 的写作要求

### Step 1 · 为什么研究这个问题？为什么重要？
- 先交代背景与任务定义（这个任务输入是什么、输出是什么），让外行能看懂。
- 重要性要具体：应用场景、规模、现有瓶颈造成的实际代价；有论文里的数字就引用数字。
- 结尾一个 `.callout.key` 点出"本文聚焦的子问题"。

### Step 2 · 现有工作分为哪几类？
- 用 `.cats > .cat` 卡片，通常 2–4 类。每类：核心思路一句话 + **2–4 个代表工作** + `.miss`（这一类没考虑到什么）。
- **每个代表工作必须带引用** `<a class="cite" href="#ref-n">[n]</a>`，引用条目**只能来自原论文的参考文献列表**（作者、标题、会议/年份照抄），能找到 arXiv/DOI 链接就附上。禁止编造引用；不确定的宁可不列。
- 分类优先沿用论文 Related Work 的划分；如果论文划分很散，可按"它们处理 P 的方式"重新归类，并注明。

### Step 3 · 现有工作存在什么问题？（按问题梳理，不按方法罗列）
- 先列出 2–4 个**关键问题维度**（其中一个就是 P），再做一张**方法×问题维度**矩阵表：`td.ok ✓ / td.part △ / td.no ✗`，最后一列/最后一行是本文方法（应全 ✓）。
- 结论段明确写出："上述工作都没有考虑到 P，因此……" → 引出本文要解决的问题。
- **必须画一张 SVG 问题示意图**（`figure.fig > svg`，配 figcaption）：直观展示现有做法在哪里出错，例如把一条轨迹画成多个步骤方块，用同一个 terminal reward 箭头指向所有步骤，用红色标出"好步骤被惩罚/坏步骤被奖励"。用模板里的 svg class（box / box-red / box-green / line / line-red …）以自动适配深浅色。

### Step 4 · 这个问题的根因是什么？
- 从"现象"追到"机制"，回答"为什么现有方法**结构上**必然会出现 P"。
- 有数学可写就写（例如说明梯度/优势函数里所有步骤共享同一个标量），用 `.formula` 组件解释。
- 用一个具体小例子（玩具数字）演示根因如何导致错误。
- 结尾 `.callout.warn` 一句话给出根因 R。

### Step 5 · 最直接的解决办法是什么？
- 写出针对 R 的最朴素解法 F（例：根因是"多步复用一个 terminal reward" → 直接解法是"credit assignment，给每一步分配各自的贡献"）。
- 再分析朴素实现的困难（需要人工 step 标注？需要训练 critic/PRM？方差大？算力大？）——这正是本文 Idea 的切入点。
- 可选：列出朴素实现的几种方式及各自缺点（小表格）。
- 结尾 `.callout.idea` 过渡到本文："本文的做法是……"。

### Step 6 · Idea 具体怎么做？（最重要、篇幅最长）
- **必须画一张整体框架 SVG 图**：输入 → 各模块 → 输出，标注与公式对应的符号；关键新模块用 box-violet/box-accent 高亮。
- 按模块/算法步骤逐一讲解；有算法伪代码就转成有序步骤列表。
- **每一个核心公式都用 `.formula` 组件**，且必须包含：
  1. **左边**：这个量代表什么、**取值范围**、取大/取小分别意味着什么；
  2. **右边每一项**：含义 + 取值范围（如概率 $[0,1]$、log-prob $(-\infty,0]$、优势 $(-\infty,+\infty)$、KL $[0,+\infty)$、指示函数 $\{0,1\}$、超参数的常用取值）；
  3. **等式整体读法**：左右两边连起来是什么意思，举一个极端情况/数值例子；
  4. `.why` **为什么这样设计**：对应解决了 Step 4/5 的哪个难点；换成更朴素的写法会出什么问题。
- 公式写法：行内 `$...$`，独立 `$$...$$`（KaTeX 渲染）。符号要和原文一致并注明原文公式编号。
- 最后小结：本文 Idea 如何对应 R 与 F 的难点（可用 2 列表格：难点 ↔ 设计）。

### Step 7 · 预期效果 / Claims
- 用 `ol.claims` 列出**尽量完备**的 Claim，通常 4–8 条，覆盖这些维度（论文有就写，没有就不写）：
  有效性（主结果提升）、泛化性（跨数据集/任务/模型规模/域外）、关键设计必要性（每个模块都有用 → 消融）、机制验证（确实解决了 P，例如 credit 分配更准、步骤级指标更好）、效率/成本（训练/推理开销、样本效率）、鲁棒性/稳定性（超参敏感性、训练曲线）、可扩展性、与其他方法正交可组合。
- 每条 Claim 后用 `<span class="ev"><span class="pill">→ E2</span>Tab.3 / Fig.4</span>` 指向 Step 8 的实验。

### Step 8 · 实验设计
分四块写：
1. **数据集**：表格（名称 / 任务类型 / 规模 / 评测指标 / 为什么选它）。说明"follow 了哪篇前人工作的设定"并引用 `[n]`；说明它们覆盖了什么难度/分布，以便证明哪个 Claim。
2. **Baselines**：按 Step 2 的类别分组列出（带引用），说明为什么选它们——每类代表 + 最强 SOTA + 朴素解法 F 的实现（若有）。
3. **评测指标 / Critic / Reward model / Judge**：论文用到的评分器、裁判模型、PRM/ORM、指标，各自是什么、为什么选、范围和方向（越高越好？）。
4. **Claim ↔ 实验 对应矩阵**（必须有）：表头 `实验 | 设置 | 证明的 Claim | 关键结果(数字) | 结论`，确保每个 C 至少被一个实验覆盖；论文没覆盖到的 Claim 要明确标出 `<span class="pill r">未验证</span>`，并在最后的 `.callout.warn` 里写出实验设计上的不足/可补充的实验（这是精读的价值所在）。
- 关键结果写**原文真实数字**（来自表格），注明 Tab./Fig. 编号；不得编造。

---

## HTML 组件速查（都已在模板 CSS 里）
- 逻辑链：hero 里的 `.chain`（P/R/F/I 四个节点）——用一句话填。
- 提示框：`.callout.info|warn|good|idea|key` + `<div class="t">标题</div>`。
- 现有工作卡片：`.cats > .cat`（h4 / .idea / ul / .miss）。
- 矩阵表：普通 `<table>`，单元格 `td.ok`/`td.no`/`td.part`；宽表外包 `.tbl-wrap`。
- 公式：`.formula > .eq(.tag) + .ex > table(tr.row-lhs …) + .why`。
- 图：`figure.fig > svg + figcaption`；svg 内用 class `box|box-accent|box-red|box-green|box-amber|box-violet|line|line-red|line-accent`，文字 class `muted|red|green|accent|violet`；箭头自己在 `<defs><marker>` 里定义，marker 填充用 `fill="currentColor"` 或具体 class。svg 设 `viewBox`，不要写死 width。
- Claims：`ol.claims > li` + `.ev` + `.pill(.g/.r/.a)`。
- 参考文献：`ol.refs > li#ref-n`，正文 `<a class="cite" href="#ref-n">[n]</a>`。
- 模板自带：左侧目录高亮、阅读进度条、"全文模式 / 逐步模式"切换（逐步模式 ← → 翻页）、每步底部上一步/下一步。

## 质检 Checklist（交付前逐条核对）
- [ ] 文件里不再有任何 `{{...}}` 占位符：`grep -n '{{' outputs/<slug>-explained.html` 为空。
- [ ] Step 3 有问题示意 SVG，Step 6 有框架 SVG；SVG 在浅/深色下都可读，文字不溢出框。
- [ ] Step 2 每类 ≥2 个代表工作且都有 [n] 引用；所有 `href="#ref-n"` 都能在 refs 里找到对应 `id`，refs 条目都来自原文参考文献。
- [ ] Step 6 每个公式都有：左边含义+范围、右边逐项含义+范围、整体读法、为什么这样设计。
- [ ] P→R→F→I 在 hero、Step 3/4/5/6 中表述一致。
- [ ] 每个 Claim 至少对应一个实验；对应矩阵里的数字来自原文表格。
- [ ] 用浏览器打开检查一次（可 `open outputs/<slug>-explained.html`，或告诉用户用 Preview 标签查看），KaTeX 渲染无红色报错。
- [ ] 清理 `work/` 中间文件。

可用这个小脚本做引用一致性检查：
```bash
f=outputs/<slug>-explained.html
python3 - "$f" <<'PY'
import re,sys;s=open(sys.argv[1]).read()
c=set(re.findall(r'href="#(ref-\d+)"',s));d=set(re.findall(r'id="(ref-\d+)"',s))
print("缺失定义:",sorted(c-d)," 未被引用:",sorted(d-c)," 占位符:",re.findall(r'\{\{\w+\}\}',s))
PY
```
