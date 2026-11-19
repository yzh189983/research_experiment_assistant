> **Language / 语言**：[中文](README.md) ｜ English

# Research Experiment Assistant

A local tool that turns "a research idea" into "a completed experiment report".

You write down an idea, and it breaks it into a verifiable experiment plan → generates code that can run unattended on a server →
deploys and runs it over SSH → monitors logs and metrics throughout → and automatically delivers a conclusion when the run finishes.

```
Idea → Experiment Plan → Executable Code → SSH Deployment → Live Monitoring → Automatic Summary
```

## Quick Start

```bash
# 1. Install dependencies
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Start
.venv/bin/python app.py

# or
./rekit server start

# 3. Open https://127.0.0.1:7788 (self-signed certificate — click "Advanced" → "Proceed" in your browser), log in as admin or register
```

## Login and Multi-Account

Login is enabled by default, and data is isolated per account (experiments, servers, and API keys are invisible to one another).

- **First run**: An `admin` account is created automatically at startup. The initial password comes from the `REKIT_ADMIN_PASSWORD` environment variable, or is written encrypted to `data/admin_initial_password.txt` (view it with `./rekit admin-password`).
- **Self-registration**: Click "Register" on the login page to create a member account, or call `POST /api/auth/register`. To disable it, set `auth.allow_registration: false` in `data/config.json`.
- **Command line**: First run `./rekit login -u USERNAME -p PASSWORD`; the token is stored in `data/cli_token` (mode 600), after which all commands are authenticated. You can also override it with the `REKIT_TOKEN` environment variable. Related commands: `rekit whoami / token / logout`.
- **Adding members**: Admins can add accounts under "Settings → Account Management" and assign "Member / Admin" roles.
- **Profile**: After logging in, click "Profile" (or visit `/profile`) to fill in your name, institution, research interests, bio, skills, education and work history, email, and academic links. The data is isolated per account and stored in `users.profile_json`.
- **Git snapshot lineage**: Before each experiment run, `data/workspace/exp_<id>` is automatically committed to git and the SHA is written to `runs.git_sha`; the experiment page lets you view the diff, restore historical code, and fork a new experiment from any run.
- **Change password**: In "Profile → Account Security", verify your current password to change it; you can also call `POST /api/users/me/password`.
- **Secure defaults**: HTTPS/WSS with a self-signed certificate, Secure cookies, one-time terminal tickets and read-only observation links; SSH passwords / API keys / CLI tokens are all encrypted at rest and logs are automatically redacted.
- **Token from the web UI**: Generate one under "Settings → CLI Login Token", for CI or multiple machines.

> ⚠️ The web terminal is equivalent to a server shell. Until HTTPS is in place, bind the service to `127.0.0.1` only (the default) and do not expose it to the public internet.

Or use the command line directly (the service is started in the background automatically if it isn't running):

```bash
./rekit doctor             # health check
./rekit auto -i @idea.md   # a leading @ reads a file — .md/.txt/.docx/.pdf all work
```

For the first run, we recommend this order:

1. On the **Settings** page, fill in the LLM's `base_url / api_key / model` (any OpenAI-compatible endpoint) and click "Test Connectivity".
2. On the **Servers** page, add an SSH host and click "Test Connection" to confirm it works. The server card shows tasks launched by this project (with step progress, refreshed every 5 seconds), and scans manually running Python scripts over SSH `ps` (PID, script, command, CPU/memory, runtime — refreshed every 10 seconds with a 15-second cache).
3. Go back to the homepage, write your idea, and create an experiment.

You don't have to type the idea by hand: you can **drag a file straight into** the homepage input box, or click "Read from File" to pick one.
Supported: `.md`, `.txt`, `.tex`, `.csv`, `.docx`, `.pdf` (`.docx` is parsed with the standard library; `.pdf` requires `pip install pypdf`).
The content is filled in automatically, and if the title is empty the first line of the document is used.

It also runs without an LLM configured: the system generates placeholder plans and code so you can verify the pipeline end to end.

## Command Line (rekit)

If you'd rather stay in the terminal, everything can be done there. `rekit` uses only the standard library and automatically starts the service in the background if it isn't running.

```bash
./rekit                      # overview: service status / servers / experiments
./rekit doctor               # health check: service + LLM + connectivity of each server

# authentication (new commands when login is enabled)
./rekit login -u admin -p PASSWORD   # log in and save the token; later commands need no login
./rekit whoami                 # current account
./rekit token                  # generate a new token (the old one is invalidated immediately)
./rekit logout                 # remove the local token
```

**Run the whole experiment in one command**

```bash
./rekit auto -i @idea.docx                       # from idea to report (a file can be passed directly)
./rekit auto -i "an idea in text" -t TITLE -H 2  # specify a title and a server
```

It prints ① create experiment → ② generate plan → ③ write code → ④ deploy → ⑤ live logs → conclusion report, and finally gives you a web link.

**Step-by-step**

```bash
./rekit new -t "TITLE" -i @idea.md     # leading @ reads a file: .md/.txt/.docx/.pdf
./rekit plan 3                        # generate the plan
./rekit plan 3 -f "make the dataset smaller"  # redo with feedback
./rekit code 3                        # generate the code
./rekit run 3 -H 2                    # deploy + follow along (default)
./rekit run 3 -H 2 --detach           # submit only, return immediately
./rekit logs 3 -f                     # follow the logs
./rekit watch 3                       # live metric curves (terminal sparkline)
./rekit report 3                      # print the conclusion
./rekit report 3 --markdown report.md # export Markdown
./rekit stop 3                        # stop
./rekit show 3                        # plan + code + latest run + conclusion
./rekit list                          # list experiments
./rekit open 3                        # open in the browser
```

**Servers and configuration**

```bash
./rekit host list
./rekit host add -a 1.2.3.4 -u root -p 22 --gpu          # password entered interactively
./rekit host add -a 1.2.3.4 -u root --key ~/.ssh/id_rsa  # key-based login
./rekit host test          # test all
./rekit host rm 3

./rekit config             # show the current config
./rekit config --set-key sk-xxx        # change only the key, and verify it
./rekit config --edit                  # open config.json in an editor
```

**Service management**

```bash
./rekit server             # status (pid, number of running experiments)
./rekit server restart     # restart after changing backend code
./rekit server logs -f     # follow the service log
```

All commands work in scripts: when piped, colors and live refresh output are automatically stripped, leaving plain text.

To type `rekit` from any directory, create a symlink:

```bash
ln -s "$PWD/rekit" /usr/local/bin/rekit
```

## Web Terminal

No need for a separate iTerm / Xshell — open a server terminal right in the browser: top navigation **Terminal** → pick a server.

- xterm.js frontend + WebSocket + paramiko shell channel — a real PTY, not a fake terminal
- Interactive commands work (`vim`, `top`, `htop`, `tail -f`, etc.)
- Chinese displays correctly, and ANSI colors / cursor / screen clearing / window resizing all follow along
- **Automatic UTF-8 locale injection**: many cloud hosts (AutoDL and the like) ship with `LANG` empty and `LC_CTYPE=POSIX`, which makes GNU `ls` escape Chinese filenames as `'ACL'$'\345\256\236\351\252\214'` — looking like "Chinese doesn't display". On connect it probes the remote `locale -a`, picks an existing UTF-8 locale (`C.UTF-8` / `en_US.UTF-8`, …) and injects it via an SSH env request; if sshd doesn't allow `AcceptEnv`, it falls back to `export` inside the session. If the remote is already UTF-8, nothing is changed.
- Adjustable font size (A− / A+), remembered in localStorage; auto-reconnects twice on disconnect
- Server passwords never reach the frontend: the page only receives safe fields like `id / name / host / port / username`

You can also enter from the "Terminal" button on any row of the **Servers** page.

Self-test (5 checks, including Chinese text, `stty size`, `$TERM`, and ANSI passthrough):

```bash
python tests/terminal_test.py 2          # 2 is the host id
```

## Supported LLMs

Any OpenAI-compatible `/chat/completions` endpoint:

| Service | base_url | Example model |
|---|---|---|
| Zhipu AI | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-max` |

Use a capable model for code generation (both the plan and the code depend on it); `glm-4-plus` / `deepseek-chat` are enough.

## How It Works

**① Idea → Experiment Plan**
Not just a summary — it breaks the idea into: core proposition, falsifiable hypotheses, dataset, metrics, baselines, ablations, success/failure criteria, and risks.
Metric names are required to be lowercase English with underscores, because they're used to plot curves later.

**② Plan → Code**
The generated code follows a few hard constraints: non-interactive, metrics reported via the protocol, automatic GPU detection, graceful degradation to synthetic data when data is unavailable, and a traceback plus non-zero exit on errors.
You can edit the code directly on the page and click Save.

**③ Deploy and Run**
SFTP upload → create a venv → install dependencies (Tsinghua mirror by default, falling back to the official index) → background start with `nohup`.
Long-running tasks always use "background + polling an exit-code file", so they are not interrupted by SSH channel timeouts.

**④ Live Monitoring**
Polls every 3 seconds: incremental log reading, process liveness checks, GPU status collection, and metric parsing.
It warns when logs stall, and marks the run as failed if the process disappears without writing an exit code.

**⑤ Automatic Summary**
Triggered as soon as an experiment finishes: aggregate metric statistics (first / last / best / change rate) → combine with the log tail → judge each hypothesis one by one →
output key findings, anomalies, limitations, next steps, and a paragraph ready to drop into a paper's Results section.
Then it automatically performs **result verification**: every number in the report is checked back against `metrics`, unsourced numbers are listed, and measured data is distinguished from inferred interpretation;
if criteria have been locked, each is also judged pass / fail.

## Research Credibility and Collaboration

- **Git snapshot lineage**: an automatic commit before every run, with `runs.git_sha` locking the code version; supports diff / restore / fork, and reports carry the commit too.
- **Push code to GitHub**: configure a fine-grained PAT and a repository on the Settings page (the token is encrypted and isolated per account), then push any run's code snapshot to your own repository from the lineage panel on the experiment page. Suspected secrets are scanned and confirmed before pushing; on success `git_sha` becomes a clickable commit link in reports and the observation page, closing the loop from result to exact code version.
- **Result verification**: the "Result Verification" panel on the conclusion page shows evidence sources (metric + step + raw log line), unverified numbers, and measured vs. inferred.
- **Human-in-the-loop checkpoints**: switch the experiment page to "Copilot mode" and the plan / code / run phases each pause for confirmation; annotations feed into the next phase.
- **Pre-registered criteria locking**: visually edit structured criteria (metric / operator / threshold); once locked, changes require a reason, and results are automatically judged one by one.
- **Full audit log**: all key actions (plan, code, criteria, snapshot, run, summary) are recorded, with a timeline on the experiment page and a process summary attached to the report.
- **Environment freezing**: each run generates a seed written to the `SEED` environment variable, and archives `pip freeze` / `nvidia-smi` / Python version with the run.
- **Report export**: the conclusion area offers Markdown, Word (paper-style three-line tables), and a print page `/report/<id>` (Save as PDF from the browser) — all including verification, criteria judgments, an audit summary, and reproduction settings.
- **Templates and batch runs**: the homepage ships four templates (train a classifier / LoRA / hyperparameter grid / ablation); the batch panel generates up to 50 experiments at once from a JSON parameter matrix; "Batch Comparison" lines up the latest metrics by parameter combination, and failed items can resume from where they stopped with one click.
- **Cost and usage**: `usage_log` records the tokens and model of every LLM call; the experiment page shows the ledger and supports budget alerts.
- **Interaction details**: "swap document" reuses the extractor for ideas; logs support search / type filtering / repeated-line collapsing; the homepage has a "recent runs" status card with failure alerts; narrow screens fall back to a single column.

## Metric Reporting Protocol

The generated code follows this automatically, but **code you write by hand** can use the same format and will be monitored and plotted just the same:

```bash
@@METRIC name=loss value=0.234 step=10
@@METRIC name=accuracy value=0.91 step=10
```

One metric per line; use `print(..., flush=True)` so output is real-time. Logs that don't follow the protocol are still displayed, just without curves.
Common forms like `loss: 0.23` / `acc=0.9` are also recognized as a fallback.

## Project Structure

```
app.py              Flask routes + background task scheduling
cli.py              command line (rekit)
rekit               CLI launcher (shell wrapper that finds python)
config.py           config loading (unified read/write of data/config.json)
core/
  db.py             SQLite: hosts / experiments / runs / metrics / events / audit / usage
  llm.py            OpenAI-compatible calls + JSON extraction + token usage
  planner.py        idea → experiment plan
  coder.py          plan → set of code files
  ssh_runner.py     upload / env setup / launch / incremental log reading + env snapshot
  monitor.py        monitoring loop + completion detection
  summarizer.py     result summary (LLM first, rule fallback) + verification trigger
  verify.py         result verification: number check / evidence sources / criteria judgment
  report.py         report export: Markdown / Word / print-page data
  gitlineage.py     Git snapshot lineage: commit / diff / restore / fork
  crypto.py         credential encryption / redaction / self-signed certificate
  workspace.py      local experiment file management
  bus.py            in-memory event bus (SSE push)
  terminal.py       SSH interactive terminal session (paramiko shell ↔ WebSocket)
templates/          page templates
static/             CSS / JS (including a hand-written canvas line chart, no CDN dependency)
  vendor/xterm/     local copy of xterm.js (no CDN)
tests/              end-to-end self-tests + local fake SSH server + terminal self-test
data/               config.json / app.db / workspace (generated at runtime)
```

## Tests

**Unit tests (no service or server needed, zero dependencies)**:

```bash
python -m unittest discover -s tests -p 'test_*.py'   # 35 tests: crypto / verify / db / report / api
```

Tests use an isolated temporary `data` directory and never touch real data; you can also `pip install -r requirements.txt` and use `pytest`.

**End-to-end tests**: when no usable server is available, a local fake SSH server runs the whole pipeline:

```bash
# terminal 1
python app.py

# terminal 2 (a real SSH endpoint on local port 2222, password test/test)
python tests/fake_ssh_server.py

# terminal 3
python tests/e2e_test.py
```

It verifies in order: add host → connect over SSH → create experiment → generate plan → generate code → deploy → monitor to completion → collect metrics → automatic summary.

Web terminal self-test (the fake SSH server also supports interactive shells and resize):

```bash
python tests/terminal_test.py 4
```

## Configuration

Everything lives in `data/config.json`, and can also be changed on the "Settings" page:

```json
{
  "llm": { "base_url": "...", "api_key": "...", "model": "...", "temperature": 0.35, "timeout": 240 },
  "monitor": { "interval_sec": 3, "stall_warn_sec": 300 },
  "server": { "host": "127.0.0.1", "port": 7788 }
}
```

A smaller `interval_sec` means more real-time updates but more frequent SSH connections; `stall_warn_sec` is how long logs can stay idle before a "possibly stuck" warning is raised.

## Notes

- Binds to `127.0.0.1` by default with HTTPS/WSS and login enabled; before exposing it publicly, replace the certificate with a real one and bind a trusted domain.
- SSH passwords / API keys / CLI tokens are all encrypted on disk with `enc:v1:`, and logs and error responses are redacted automatically; view the initial password with `./rekit admin-password`.
- After a service restart, experiments still running are picked up automatically (`bootstrap()`), so monitoring is not lost.
- Flask's built-in server is fine for a single user; for multiple users, switch to waitress/gunicorn.

## UI

The visual tone follows academic journal typesetting, not a SaaS dashboard: white paper, hairline rules, serif headings, three-line tables — no shadows or large rounded corners.

- Headings and core propositions use a serif (Songti-style) face, body text and forms use sans-serif, metrics and code use monospace with `tabular-nums`
- Small labels are always "small size + wide letter spacing + all caps", like figure captions; section headings are followed by a thin rule that pushes the controls away
- Metric summaries use booktabs-style three-line tables: thick top and bottom rules, a thin middle rule, no vertical lines
- Data cells in the monitoring panel are a shared-border table, not individual rounded cards
- The "paper results paragraph" in the conclusion uses serif + generous line spacing + justified alignment, so it reads as body text
- Curves use a low-saturation, print-friendly palette (dark green / blue / ochre / purple / brick red / cyan)
- `Cmd/Ctrl + P` prints directly: navigation, buttons, and the terminal are hidden automatically, leaving only the content

Styles live in two files: `static/css/app.css` (colors, typography, buttons, forms) and `static/css/pages.css` (per-page layouts).

**Accessibility and i18n**:
- The "文/A" button in the top-right toggles Chinese / English; the preference is remembered locally. English covers navigation, headings, buttons, status badges, logs and dynamic notices (350 phrase mappings, with dynamically inserted content translated automatically)
- Keyboard friendly: skip-to-content link, consistent `:focus-visible` outlines, Esc to close dialogs, `aria-label` on icon buttons, and `aria-live` announcements for toasts
- All color combinations meet WCAG AA (≥4.5:1), for long-time use and users with low vision
