# ADR-0032：额度以 GPT-Load 管理端的真实数据为准，不用 token 估算

- 状态：已接受（取代 ADR-0031 第 4 条）
- 日期：2026-10-12

## 背景
本人指出：应该直接读额度，而不是用"5h ≈ 1600 万 token、7d ≈ 9000 万 token"这种估算。
批量模型走的是 GPT-Load（https://<llm-gateway>，分组 5），管理端接口
`GET /api/modern/groups/5/credentials`（`Authorization: Bearer <管理密钥>`）给出每个账号的
`quota_windows`（gemini-5h / gemini-weekly / 3p-5h / 3p-weekly：`remaining` 剩余百分比、`reset_at_ms` 重置时间）、
`model_cooldowns`（模型冷却到何时）、`effective_status`。

## 决策
- 凭据放 `$WB_HOME/secrets/quota.env`（`WB_QUOTA_BASE_URL / WB_QUOTA_AUTH_KEY / WB_QUOTA_GROUP_ID`，权限 600）。
- **每次请求前**读真实额度（进程内缓存 60 秒）：
  - 当前模型所属窗口（gemini-* → gemini-5h / gemini-weekly，其它 → 3p-*）剩余 < 1%，或模型在冷却，或账号不可用 → 暂停到 `reset_at` / 冷却结束；
  - **回填**：5h 剩余 < 40% 或 7d 剩余 < 35% 就停到对应窗口重置（阈值在 `config.json` 的 `llm_budget` 里改）；
  - 每日流程用到额度真正用完为止。
- 撞到限额错误时，恢复时间优先取管理端给的 `reset_at`，其次是错误里的提示，最后才是 30 分钟后试探。
- 管理端接口不可用时，只剩"撞到限额错误就熔断"这一层，不再退回 token 估算。
- **隐私**：接口返回里有账号完整邮箱和代理地址（含用户名），只取额度字段，原始响应不落盘、不缓存、不进日志。
- `wb daily status` 显示真实额度（剩余 % 与重置倒计时）。

## 补充（2026-10-12）：一个分组多个凭据
本人往分组里加了第二个凭据后实测：4 次请求全部落在新凭据上，模型冷却中的旧凭据一次都没被用到——
**GPT-Load 会跳过冷却 / 不可用的凭据**（都可用时是轮询还是有优先级，待两个凭据都恢复后再测）。
因此额度判断改为**按凭据**：某个模型只有在**所有**凭据都不可用（状态不是 available、该模型冷却中、5h / 7d 低于阈值）时才暂停，
暂停到"最早有一个凭据恢复"的时间；回填要求至少有一个凭据的 5h / 7d 剩余高于回填阈值。
另：上游偶发 `503 MODEL_CAPACITY_EXHAUSTED`（Google 侧临时容量不足，不是额度），按普通 5xx 短暂重试，不触发熔断。
