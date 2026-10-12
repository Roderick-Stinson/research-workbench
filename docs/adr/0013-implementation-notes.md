# ADR-0013：M1–M3 实现时的取舍

- 状态：已接受
- 日期：2026-10-11
- 关系：落实 ADR-0012；下面几条是写代码时才暴露出来、与提议稿有出入或提议稿没写到的地方。

## 1. 守护进程用 cron + flock 常驻，不用 systemd / 容器
实验室服务器上 `loginctl` 显示 `Linger=no`，用户态 systemd 服务在退出登录后会被杀；放进容器又要 `pid: host` + 读宿主机 `/proc`，权限更大。
cron 每分钟执行一次 `flock -n lock python -m workbench daemon`：已在运行就立即退出，挂了最多 1 分钟后自动恢复。
守护进程把运行中 Run 的状态存在 `daemon/state.json`，重启后接着跟踪。

## 2. 不做 index.sqlite（暂时）
`runs/*/run.json` + `claim.json` 按 mtime 缓存读取，几千个 Run 以内足够快。等真的慢了再加派生索引（届时新增 ADR）。

## 3. 退出码通常拿不到
守护进程不是实验的父进程，进程结束后内核只把退出码交给父进程。只有恰好看到僵尸状态时才能读到（`/proc/<pid>/stat` 第 52 列）。
其余情况根据日志尾部推断（Traceback / OOM / Killed），面板上明确标注"推断"。

## 4. 快照范围
- 目录是 git 仓库：用户跟踪的文件 + 未被 .gitignore 忽略的 untracked 文件（untracked 超过 2 万个时只收代码 / 配置类扩展名）。
- 目录不是 git 仓库（如 `baseline/`，38 万文件、其中 32 万是 results）：遍历时跳过 results / outputs / logs / data 等目录，只收代码和配置类扩展名。
- 嵌套的 git 仓库（`baseline/methods/<别人的代码>`）有未提交改动时，单独再拍一份。
- 大于 20MB 的文件、权重 / 数据格式（`*.safetensors` 等）、`.env` / `.secrets/` 不进快照，只记路径 + 大小 + mtime。
- 同时启动的多个种子共用同一次快照（启动后 30 秒内、且快照晚于进程启动）。
- 实测：<项目 A> 首次 0.25s / 之后 0.06s；<项目 B 的目录>（6.7k 文件）首次 7.6s / 之后 1.4s。

## 5. 产物保存
- 结束时收集"本 Run 打开过写句柄的文件 + 这些文件所在目录 + `--output_dir` 类参数指向的目录"里、修改时间落在运行期间的文件。
- 默认**复制**到 `runs/<ID>/outputs/` 并设为只读：硬链接挡不住 `torch.save` 这类"原地截断重写"。
- 单个 Run 超过 50GB，或复制后 数据盘剩余空间会低于 300GB 时，改为**硬链接**（零额外空间，挡得住删除和 rename 替换，挡不住原地重写），面板标"产物仅硬链接"。
- 被 `wb ignore` 的 Run 结束时只记清单、不复制。
- 默认排除下载工具（`hf download`、`modelscope` 等）：上线时抓到一个 `hf download`，它写出的是数据集，不是实验产物。

## 6. 环境变量和对话的脱敏
实验进程的环境里有 OAuth token 等密钥。按变量名（KEY / TOKEN / SECRET / PASS / AUTH …）和值的形态（`sk-…`、`hf_…`、JWT 等）两层脱敏；
命令行参数（`--api_key xxx`）、面板展示的日志和对话片段也走同一套规则。

## 7. 关联对话
codex 的工具调用存的是转义过的 JS 字符串，先反转义再匹配；循环启动（`for s in 0 1 2; … --seed $s`）时命令里是变量、进程参数里是数字，匹配时把数字视为可替换。
用 7 月 31 日 Gemini 72 任务消融的真实记录验证：orchestrator 和它拉起的子任务都以 high 置信度定位到启动它们的那次调用。

## 8. 子进程
- `torchrun` / `accelerate` / multiprocessing worker 等并入父 Run；
- 父 Run 拉起的、命令不同的 python 子进程单独成 Run，带 `parent_run`（例如一个 orchestrator 并发 72 个任务 → 1 个父 Run + 72 个子 Run，认领时可 `--with-children`）。

## 9. 面板部署
`panel/docker-compose.yml`，`network_mode: host`，端口 8765，**所有挂载只读**，以实验用户 uid运行。
构建时实验室服务器的代理访问 deb.debian.org 返回 502，改为直连清华镜像（构建参数可改）。
