# ADR-0028：开工前确定的实现选择

- 状态：已接受
- 日期：2026-10-11

## 决策
1. **不接 X**（本人：不需要）。**Reddit 不接**（实验室服务器访问返回 403）。关注度信号只用 HF Daily Papers（点赞）+ Hacker News。
2. **批量任务模型**（收录判定 / 速读 / 标签 / 结果抽取）：本人单独提供，**主要用 Gemini**。
   凭据放 `$WB_HOME/secrets/llm.env`（权限 600）：`WB_LLM_BASE_URL / WB_LLM_API_KEY / WB_LLM_MODEL / WB_LLM_PROTOCOL(openai|gemini)`。
   精读、领域地图、构思、审稿仍由 codex 执行（ADR-0020）。
3. **首次回填**：引用了 5 个 baseline 的全部论文 + arXiv 近 12 个月关键词检索；只做速读 + 标签 + 结果抽取，不做精读。
4. **没有 Semantic Scholar key**：引文以 **OpenAlex** 为主（免费、无 key），S2 匿名限速调用作补充；所有外部请求本地缓存。
5. **OpenReview** api2 返回 403：先试其它入口；不可用时顶会元数据走 DBLP + OpenAlex。
6. 每日流程 01:00 开始，08:30 推送第一版（ADR-0026 默认值，本人未反对）。
7. **回填速读全部读全文**（本人 2026-10-12 选择）：预计 3000–5000 篇、约 1.5 亿 token，Gemini 2.5 Flash 约 40–60 美元；
   每篇都能贡献结果表数字。
