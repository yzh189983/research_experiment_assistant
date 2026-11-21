> **Language / 语言**：[中文](README.md) ｜ English

# Research Experiment Assistant

A local tool that turns "a research idea" into "a completed experiment report": write down an idea, and it generates the plan and code, deploys the run over SSH, watches logs and metrics throughout, then summarizes and verifies the results when the run finishes.

```
Idea → Experiment Plan → Executable Code → SSH Deployment → Live Monitoring → Automatic Summary
```

## Quick Start

```bash
# 1. Install dependencies
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Start the service
.venv/bin/python app.py
# or
./rekit server start
```

Open `https://127.0.0.1:7788` (self-signed certificate — click "Advanced" → "Proceed" in your browser). An `admin` account is created on first start; view its initial password with `./rekit admin-password`. You can also register on the login page.

Recommended setup order:

1. On the **Settings** page, fill in the LLM's `base_url / api_key / model` (any OpenAI-compatible endpoint) and click "Test Connectivity".
2. On the **Servers** page, add an SSH host and click "Test Connection".
3. Go back to the homepage, write your idea, and create an experiment.

It also runs without an LLM configured — placeholder plans and code are generated so you can verify the pipeline. Everything can be done from the terminal instead: `./rekit auto -i @idea.md`.

## Data Storage

By default all data lives in a local SQLite file (`data/app.db`, WAL mode) — zero setup. If you already run PostgreSQL / MySQL, you can point the app at it in two ways:

- **Web UI (recommended)**: admins open "Settings → Data Storage", pick a backend, fill in the connection details and save. The password is never shown in plain text — leaving it blank keeps the current one.
- **Config file**: add a `storage` section to `data/config.json` (example below).

If it is absent or `backend` is `sqlite`, the original SQLite file is used exactly as before.

```json
{
  "storage": {
    "backend": "postgresql",
    "host": "127.0.0.1",
    "port": 5432,
    "database": "rekit",
    "user": "rekit",
    "password": "plain text is fine; encrypted at rest",
    "sslmode": "disable"
  }
}
```

- `backend`: `sqlite` (default) | `postgresql` | `mysql` (use port `3306` for MySQL).
- Create an empty database first (tables are created/migrated automatically) and install the matching driver:

  ```bash
  pip install -e ".[postgres]"   # PostgreSQL
  pip install -e ".[mysql]"      # MySQL
  ```

- The "Settings → Data Storage" card is admin-only (global config, affects every account); editing the file needs no admin but works the same way.
- Either way, **restart** the service to apply — saving keeps the running process on the old database, so it never drops mid-session. The schema matches the SQLite version; SQL is translated by an adapter layer, so no feature changes are needed.
- Avatars, generated code and secrets still live under `data/`; only business data (experiments, runs, metrics, messages, ...) uses the configured database.

## Core Features

**Experiment pipeline**

- Drag `.md/.txt/.tex/.csv/.docx/.pdf` manuscripts straight into the idea input
- Plans break down into hypotheses, datasets, metrics, baselines, ablations, criteria and risks; code is generated automatically and editable on the page
- SFTP upload → venv setup → dependency install → `nohup` background run; every 3 seconds it reads new logs, checks the process, samples GPUs and parses metrics
- On completion it aggregates metrics, judges hypotheses one by one, writes a conclusion with a paper-ready paragraph, and traces every number back to metrics and logs

**Credibility and rigor**

- Git snapshot lineage: every run locks a commit, with diff / restore / fork support
- Result verification, pre-registered criteria locking, multi-run statistics with significance tests, automatic failure diagnosis
- Five-tier AI trust ratings with sentence-level evidence, AI self-review, regression evaluation cases, a whitelisted tool sandbox and full audit logs
- Seed / `pip freeze` / `nvidia-smi` environment freezing

**Projects and Kaka**

- Projects / workspaces with owner / editor / viewer roles and per-account data isolation
- Kaka agent: drafts scripts before a run, watches for anomalies during a run, and summarizes results afterwards; click the cute mascot for a sidebar chat with history, and configure a separate model in Settings
- Project chat: typing `@` pops up a mention menu (Kaka + project members; arrow keys to navigate, Enter to insert, live filtering). `@Kaka` asks a question grounded in experiment records; progress and results are broadcast automatically
- Dataset collection: filter papers by year range, prioritize CCF-A / CAS Q1 venues, extract datasets and provide file-level download links from Hugging Face / Zenodo
- Project GPU queue: register server GPUs, book time slots as a team; a run automatically occupies its slot on start and releases it on finish/failure, with Kaka announcing idle GPUs in project chat
- Read-only share links with passwords, expiry and redaction; visitors can leave comments

**Kaka in three stages**

- Before a run: on the experiment page use the Kaka workbench to draft the plan / script; click the mascot to open a sidebar chat with history
- During a run: while a run is `running`, the workbench shows a monitor summary and "Refresh Kaka monitor", while the right panel shows live logs, metric curves, GPU and process status
- After a run: when a run is `finished` without a conclusion, the workbench offers a single "Let Kaka summarize this result" action, producing an evidence-backed conclusion, criteria checks and a paper paragraph, broadcast to the project chat

**Project GPU queue in practice**

1. On the project page open the **Resources / Schedule** tab → "Register GPU": choose a server owned by a project member, then fill in the GPU index and memory (add the SSH host first on the Servers page)
2. "Book a slot": pick a GPU, set start/end time and optionally link an experiment. Overlapping slots are rejected; viewers are read-only
3. Booking is only a placeholder — **it does not start a run automatically**. Starting a run in the same project, same experiment and same user turns the earliest queued slot into "active"
4. When the run finishes or fails, the slot is released with the actual end time; if nobody else is queued, Kaka posts a "GPU idle" message to the project chat
5. Current limits: no automatic reminder at the booked time, expired bookings are not auto-cancelled (cancel manually), and bookings are never bound to another user's run

**Demo data (no SSH / LLM needed)**

```bash
python3 tools/demo_running.py --reset    # a "running" experiment to try the monitor panel and Kaka watch
python3 tools/demo_finished.py --reset   # a "finished, not yet summarized" experiment to try post-run summary
# Both titles start with [演示] and live in the personal project; --reset deletes the old one and creates a new one (id increments)
```

**Productivity tools**

- Web terminal (real PTY, supports `vim` / `top` / `tail -f`)
- Report export to Markdown, Word and a print page (Save as PDF from the browser)
- `rekit` CLI covers creating experiments, planning, coding, running, logs and reports; restart the service with `./rekit server restart`
- Compact-card workbench, batch experiments and comparison, global timeline, webhook notifications
- One-click Chinese / English toggle in the top-right corner
