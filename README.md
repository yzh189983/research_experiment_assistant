> **语言 / Language**：中文 ｜ [English](README_EN.md)

# 科研实验助手

把「一个研究想法」变成「一份跑完的实验报告」的本地工具。

你写下创新点，它拆成可验证的实验方案 → 写出能在服务器上无人值守跑完的代码 →
通过 SSH 部署执行 → 全程盯日志和指标 → 跑完自动给出结论。

```
创新点 → 实验方案 → 可执行代码 → SSH 部署运行 → 实时监控 → 自动总结
```

## 快速开始

```bash
# 1. 装依赖
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 启动
.venv/bin/python app.py

或者
./rekit server start

# 3. 打开 https://127.0.0.1:7788（自签证书，浏览器点“继续访问”），用 admin 登录或自助注册
```

## 登录与多账号

默认开启登录鉴权，数据按账号隔离（实验、服务器、API Key 互不可见）。

- **首次使用**：启动时会自动创建 `admin`，初始密码来自环境变量 `REKIT_ADMIN_PASSWORD`，或加密写在 `data/admin_initial_password.txt`（用 `./rekit admin-password` 查看）。
- **自助注册**：在登录页点「注册账号」即可创建成员账号；也可调用 `POST /api/auth/register`。如需关闭，在 `data/config.json` 中设置 `auth.allow_registration: false`。
- **命令行**：先 `./rekit login -u 用户名 -p 密码`，令牌存到 `data/cli_token`（权限 600），之后所有命令免登录；也可设环境变量 `REKIT_TOKEN` 覆盖。相关命令：`rekit whoami / token / logout`。
- **加成员账号**：管理员在「设置 → 账号管理」里添加，可分配「成员 / 管理员」角色。
- **个人主页**：登录后点「个人主页」（或访问 `/profile`），可填写姓名、单位、研究方向、简介、技能、教育与工作经历、邮箱和学术链接；资料按账号隔离，存于 `users.profile_json`。
- **Git 快照谱系**：每次运行实验前自动把 `data/workspace/exp_<id>` 提交为 git commit，SHA 写入 `runs.git_sha`；实验页可查看 Diff、恢复历史代码、从任意 run Fork 新实验。
- **修改密码**：在「个人主页 → 账号安全」验证当前密码后修改；也可调用 `POST /api/users/me/password`。
- **安全默认**：HTTPS/WSS + 自签证书、Secure Cookie、一次性终端票据与只读观察链接；SSH 密码 / API Key / CLI Token 均加密落盘，日志自动脱敏。
- **网页端拿令牌**：「设置 → 命令行登录令牌」可生成，用于 CI 或多台机器。

> ⚠️ 网页终端等价于服务器 shell。在补上 HTTPS 之前，服务请只绑 `127.0.0.1`（默认就是），不要暴露到公网。

或者直接用命令行（服务会自动在后台起来）：

```bash
./rekit doctor      # 体检
./rekit auto -i @想法.md      # @ 开头表示读文件，.md/.txt/.docx/.pdf 都行
```

首次使用建议按这个顺序：

1. **设置** 页填 LLM 的 `base_url / api_key / model`（任意 OpenAI 兼容接口），点「测试连通性」
2. **服务器** 页加一台 SSH 主机，点「测试连接」确认能通；服务器卡片会显示本项目启动的任务（含 step 进度，每 5 秒刷新），并通过 SSH `ps` 扫描手动运行的 Python 脚本（PID、脚本、命令、CPU/内存、运行时长，每 10 秒刷新，15 秒短缓存）
3. 回首页写创新点，创建实验

创新点不用手打：首页输入框可以直接**把文件拖进去**，或者点「从文件读取」选文件。
支持 `.md` `.txt` `.tex` `.csv` `.docx` `.pdf`（`.docx` 用标准库解，`.pdf` 需要 `pip install pypdf`）。
读进来会自动填正文，标题还空着就拿文稿第一行填上。

不配 LLM 也能跑：系统会生成占位方案和占位代码，用来验证链路是否通。

## 命令行（rekit）

不想开网页时，所有事都能在终端做完。`rekit` 只用标准库，服务没起会自动在后台拉起。

```bash
./rekit                      # 概览：服务状态 / 服务器 / 实验
./rekit doctor               # 体检：服务 + 大模型 + 每台服务器连通性

# 鉴权（开启登录后有新命令）
./rekit login -u admin -p 密码   # 登录并保存令牌，之后命令免登录
./rekit whoami                 # 当前账号
./rekit token                  # 生成新令牌（旧令牌立即失效）
./rekit logout                 # 删除本地令牌
```

**一条命令跑完整个实验**

```bash
./rekit auto -i @想法.docx                    # 从想法到报告（文件也能直接喂）
./rekit auto -i "一段想法文本" -t 标题 -H 2    # 指定标题和服务器
```

它会打印 ① 建实验 → ② 出方案 → ③ 写代码 → ④ 部署 → ⑤ 实时日志 → 结论报告，最后给出网页链接。

**分步操作**

```bash
./rekit new -t "标题" -i @想法.md      # @ 开头读文件，支持 .md/.txt/.docx/.pdf
./rekit plan 3                        # 生成方案
./rekit plan 3 -f "数据集再小一点"      # 带意见重做
./rekit code 3                        # 生成代码
./rekit run 3 -H 2                    # 部署 + 实时跟随（默认）
./rekit run 3 -H 2 --detach           # 只提交，立刻返回
./rekit logs 3 -f                     # 跟随日志
./rekit watch 3                       # 实时刷新指标曲线（终端 sparkline）
./rekit report 3                      # 打印结论
./rekit report 3 --markdown 报告.md    # 导出 Markdown
./rekit stop 3                        # 停止
./rekit show 3                        # 方案 + 代码 + 最近运行 + 结论
./rekit list                          # 实验列表
./rekit open 3                        # 在浏览器打开
```

**服务器与配置**

```bash
./rekit host list
./rekit host add -a 1.2.3.4 -u root -p 22 --gpu          # 密码交互输入
./rekit host add -a 1.2.3.4 -u root --key ~/.ssh/id_rsa  # 密钥登录
./rekit host test          # 测全部
./rekit host rm 3

./rekit config             # 看当前配置
./rekit config --set-key sk-xxx        # 只换 Key，并自动验证
./rekit config --edit                  # 编辑器打开 config.json
```

**服务管理**

```bash
./rekit server             # 状态（含 pid、在跑的实验数）
./rekit server restart     # 改了后端代码后重启
./rekit server logs -f     # 看服务日志
```

命令都可以用在脚本里：输出到管道时会自动去掉颜色和动态刷新，只留纯文本。

想在任何目录敲 `rekit`，做个软链：

```bash
ln -s "$PWD/rekit" /usr/local/bin/rekit
```

## 网页终端

不用另开 iTerm / Xshell，直接在浏览器里开服务器终端：顶部导航 **终端** → 选服务器。

- xterm.js 前端 + WebSocket + paramiko shell 通道，真 PTY，不是假终端
- 支持交互式命令（`vim` / `top` / `htop` / `tail -f` 都能用）
- 中文正常显示，ANSI 颜色 / 光标 / 清屏 / 窗口拖动都跟着走
- **自动注入 UTF-8 locale**：很多云主机（AutoDL 之类）默认 `LANG` 为空、`LC_CTYPE=POSIX`，
  这时 GNU `ls` 会把中文名转义成 `'ACL'$'\345\256\236\351\252\214'`，看着像"中文显示不出来"。
  连接时先探一次远端 `locale -a`，挑一个实际存在的 UTF-8（`C.UTF-8` / `en_US.UTF-8` …）通过
  SSH env 请求注入；sshd 没开 `AcceptEnv` 时退回进会话后 `export`。远端本来就是 UTF-8 则不动它。
- 字号可调（A− / A+），记在 localStorage；断线自动重连 2 次
- 服务器密码不会下发到前端：页面只拿 `id / name / host / port / username` 这些安全字段

也可以从 **服务器** 页每行的「终端」按钮直接进。

自测（跑完 5 项，含中文、`stty size`、`$TERM`、ANSI 透传）：

```bash
python tests/terminal_test.py 2          # 2 是主机 id
```

## 支持的 LLM

任何 OpenAI 兼容的 `/chat/completions` 接口：

| 服务 | base_url | 模型名示例 |
|---|---|---|
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-max` |

生成代码的模型建议用能力强的（方案和代码都靠它），`glm-4-plus` / `deepseek-chat` 够用。

## 它怎么工作

**① 创新点 → 实验方案**
不是简单总结，而是拆成：核心命题、可证伪假设、数据集、指标、基线、消融、成功/失败判据、风险。
指标名会被要求用英文小写下划线，因为后面要用它画曲线。

**② 方案 → 代码**
生成的代码受几条硬约束：无交互、指标按协议上报、自动检测 GPU、数据拿不到就降级到合成数据继续跑、异常打印 traceback 并非零退出。
你可以直接在页面上改代码，改完点保存。

**③ 部署运行**
SFTP 上传 → 建 venv → 装依赖（默认清华镜像，失败自动回退官方源）→ `nohup` 后台启动。
长任务全部走"后台 + 轮询退出码文件"，不会因为 SSH 通道超时中断。

**④ 实时监控**
每 3 秒轮询一次：增量读日志、检查进程存活、采集 GPU 状态、解析指标。
日志卡住不动会提醒，进程消失且没写退出码会判失败。

**⑤ 自动总结**
实验一结束自动触发：聚合指标统计（首值/末值/最优/变化率）→ 结合日志尾部 → 逐条判断假设成立与否 →
输出关键发现、异常、局限性、下一步，以及一段可直接进论文 Results 的话。
随后自动做**结果核验**：把报告中的数字逐条回查 `metrics`，列出无来源的可疑数值，区分实测数据与推断性解读；
若已锁定判据，还会逐条判定通过/未通过。

## 科研可信度与协作

- **Git 快照谱系**：每次运行前自动 commit，`runs.git_sha` 锁定代码版本；支持 Diff / 恢复 / Fork，报告也带 commit。
- **推送代码到 GitHub**：在设置页配置 fine-grained PAT 与仓库（Token 加密存储、按账号隔离），实验页谱系面板即可把任意一次 run 的代码快照推到自己的 GitHub 仓库。推送前扫描疑似敏感信息并二次确认，成功后 `git_sha` 在报告 / 观察页变成可点击的 commit 链接，让「结果 → 代码版本」真正闭环。
- **结果核验**：结论页「结果核验」面板展示证据来源（指标 + step + 原始日志行）、未验证数字、实测 vs 推断。
- **人在环 checkpoint**：实验页切「副驾驶模式」后，方案 / 代码 / 运行各阶段会暂停等待确认，批注可回灌给下一阶段。
- **预注册判据锁定**：可视化编辑结构化判据（指标 / 运算符 / 阈值），锁定后修改必须写原因，结果自动逐条判定。
- **完整审计日志**：方案、代码、判据、快照、运行、总结等关键动作全部记录，实验页时间线可回溯，报告附过程摘要。
- **环境固化**：每次运行生成 seed 并写入 `SEED` 环境变量，随 run 存档 `pip freeze` / `nvidia-smi` / Python 版本。
- **报告导出**：结论区提供 Markdown、Word（论文式三线表）、打印页 `/report/<id>`（浏览器另存为 PDF）三种格式，均含核验、判据判定、审计摘要与复现配置。
- **模板与批量**：首页内置训练分类器 / LoRA / 超参网格 / 消融四个模板；批量面板用 JSON 参数矩阵一次生成最多 50 个实验；「批量实验对比」按参数组合对照最新指标，失败项可一键断点续跑。
- **成本用量**：`usage_log` 记录每次 LLM 调用的 token 与模型，实验页显示账本并支持设置预算告警。
- **交互细节**：创新点「换一份文稿」复用抽取器；日志支持搜索 / 类型筛选 / 重复行折叠；首页有「最近运行」状态卡与失败告警；窄屏自动单列布局。

## 指标上报协议

生成的代码会自动遵守，但**你手写的代码**也用这个格式，一样能被监控和画图：

```bash
@@METRIC name=loss value=0.234 step=10
@@METRIC name=accuracy value=0.91 step=10
```

一行一个指标，`print(..., flush=True)` 确保实时。没接协议的日志也照常显示，只是不会有曲线。

另外也能识别常见的 `loss: 0.23` / `acc=0.9` 写法做兜底。

## 项目结构

```
app.py              Flask 路由 + 后台任务调度
cli.py              命令行（rekit）
rekit               命令行启动器（shell 包装，自动找 python）
config.py           配置加载（统一读写 data/config.json）
core/
  db.py             SQLite：主机 / 实验 / 运行 / 指标 / 事件 / 审计 / 用量
  llm.py            OpenAI 兼容调用 + JSON 抽取 + token 用量
  planner.py        创新点 → 实验方案
  coder.py          方案 → 代码文件集
  ssh_runner.py     上传 / 建环境 / 启动 / 增量读日志 + 环境快照
  monitor.py        监控循环 + 结束判定
  summarizer.py     结果总结（LLM 优先，规则兜底）+ 核验触发
  verify.py         结果核验：数字回查 / 证据来源 / 判据判定
  report.py         报告导出：Markdown / Word / 打印页数据
  gitlineage.py     Git 快照谱系：commit / diff / restore / fork
  crypto.py         凭据加密 / 脱敏 / 自签证书
  workspace.py      本地实验文件管理
  bus.py            内存事件总线（SSE 推送）
  terminal.py       SSH 交互式终端会话（paramiko shell ↔ WebSocket）
templates/          页面模板
static/             CSS / JS（含手写 canvas 折线图，无 CDN 依赖）
  vendor/xterm/     xterm.js 本地副本（不用 CDN）
tests/              端到端自测 + 本地假 SSH 服务器 + 终端自测
data/               config.json / app.db / workspace（运行时生成）
```

## 自测

**单元测试（无需服务 / 服务器，零依赖）**：

```bash
python -m unittest discover -s tests -p 'test_*.py'   # 35 项：crypto / verify / db / report / api
```

测试用临时 `data` 目录隔离，绝不触碰真实数据；也可 `pip install -r requirements.txt` 后用 `pytest`。

**端到端测试**：没有可用服务器时，用本地假 SSH 服务器跑通全链路：

```bash
# 终端 1
python app.py

# 终端 2（在本机 2222 端口提供一个真 SSH 端点，密码 test/test）
python tests/fake_ssh_server.py

# 终端 3
python tests/e2e_test.py
```

会依次验证：建主机 → 连 SSH → 建实验 → 出方案 → 出代码 → 部署 → 监控到结束 → 采集指标 → 自动总结。

网页终端的自测（假 SSH 服务器也支持交互式 shell 和 resize）：

```bash
python tests/terminal_test.py 4
```

## 配置项

都存在 `data/config.json`，也可以在「设置」页改：

```json
{
  "llm": { "base_url": "...", "api_key": "...", "model": "...", "temperature": 0.35, "timeout": 240 },
  "monitor": { "interval_sec": 3, "stall_warn_sec": 300 },
  "server": { "host": "127.0.0.1", "port": 7788 }
}
```

`interval_sec` 调小更实时但 SSH 连接更频繁；`stall_warn_sec` 是日志多久不动就提醒可能卡住。

## 注意

- 默认绑 `127.0.0.1` 并启用 HTTPS/WSS + 登录鉴权；对外暴露前请替换正式证书并绑定可信域名。
- SSH 密码 / API Key / CLI Token 均以 `enc:v1:` 加密落盘，日志与错误响应自动脱敏；初始密码用 `./rekit admin-password` 查看。
- 服务重启后会自动接管还在跑的实验（`bootstrap()`），不会丢监控。
- Flask 自带服务器够单用户用；多人共用请换 waitress/gunicorn。

## 界面

视觉基调照着期刊排版来，不是 SaaS 后台那一套：白纸、发丝线、衬线标题、三线表，不用阴影和大圆角。

- 标题、核心命题用衬线（宋体系），正文与表单用无衬线，指标和代码用等宽 + `tabular-nums`
- 小标签一律「小字号 + 宽字距 + 全大写」，像图注；分节标题后面拖一条细线把操作区推出去
- 指标汇总用三线表（booktabs）：上下粗线、中间细线、没有竖线
- 监控面板的数据格是共边表格，不是一个个圆角小卡片
- 结论里的「论文结果段落」用衬线 + 宽行距 + 两端对齐，直接能看出是正文
- 曲线配色换成了低饱和、可印刷的一组（深绿 / 蓝 / 赭 / 紫 / 砖红 / 青）
- `Cmd/Ctrl + P` 直接打印：自动隐掉导航、按钮和终端，只留正文

样式集中在两个文件：`static/css/app.css`（配色、排版、按钮、表单）+ `static/css/pages.css`（各页面版式）。
