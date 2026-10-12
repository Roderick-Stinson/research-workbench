# ADR-0029：论文侧实现时的取舍

- 状态：已接受
- 日期：2026-10-11

1. **arXiv 逐个关键词查询再合并**：实测 arXiv API 把多个 `OR` 组合进一个查询时结果不可靠（OR3 = 单个词的结果数），所以每个检索词单独查、按 arXiv ID 合并；
   日期窗口按整天（UTC），每天回看 4 天，由去重保证不重复。
2. **DBLP 有防爬验证**（返回 "Making sure you're not a bot"），录用会议改用 Semantic Scholar 批量接口（只显示会议名，不显示 S2 的年份，因为 S2 的 year 是首发年份）。
3. **OpenAlex 的 arXiv 记录几乎没有引用数据**（MaAS 显示 0），"引用了种子的论文"用 Semantic Scholar 匿名接口（只有 5 个种子，请求很少）。
4. **代码仓库**：优先 HF 论文页的 `githubRepo` 与 arXiv comment 里的链接；GitHub 搜索"README 提到某 arXiv 号"大多返回论文列表类仓库，只用于"新仓库"栏，并经相关性判定。
5. **通过引用等途径发现的旧论文**（发表早于回看窗口 30 天以上）标为回填：进库、做速读，但不算本期新论文、不进精选、不做关注度查询。
6. **HTTP 缓存不能存凭据**：Telegram 的请求 URL 里有 bot token，LLM 请求带 key，这些请求一律不读写缓存，错误信息里也不带 URL。
7. **codex 执行**：`codex exec --ephemeral -s workspace-write -C <任务目录>`，精读不联网（全文和 PDF 预先放进目录），审稿联网查新；
   每次调用记 `reports/codex_usage.jsonl`。实测 MaAS 一篇 8 步精读 7.5 分钟（默认 gpt-5.6-sol / xhigh）。
8. **守护进程不抓工作台自己的任务**：cwd 在 `$WB_HOME` 下的进程（codex 精读时跑的作图脚本等）不算实验。
9. **测试用假模型**：`WB_LLM_PROTOCOL=fake` 只在测试目录使用，不调用任何外部模型。
10. **面板只读挂载**：领域 / 种子配置缺省时面板用默认值，不尝试写盘。
