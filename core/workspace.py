"""本地实验工作区：把生成的代码落盘，支持读取/编辑。"""
import os
import json
import shutil

from config import WORKSPACE_DIR


def exp_dir(experiment_id):
    d = os.path.join(WORKSPACE_DIR, f"exp_{experiment_id}")
    os.makedirs(d, exist_ok=True)
    return d


def write_files(experiment_id, code):
    d = exp_dir(experiment_id)
    # 清理上一轮生成的文件（保留其他）
    for f in code.get("files", []):
        p = os.path.join(d, f["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f["content"])
    with open(os.path.join(d, ".code_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in code.items() if k != "files"}, fh, ensure_ascii=False, indent=2)
    return d


def list_files(experiment_id):
    d = exp_dir(experiment_id)
    out = []
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "__pycache__"]
        for fn in files:
            if fn.startswith("."):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, d)
            out.append({
                "path": rel,
                "size": os.path.getsize(full),
            })
    out.sort(key=lambda x: (x["path"] != "main.py", x["path"]))
    return out


def read_file(experiment_id, rel_path):
    d = exp_dir(experiment_id)
    full = os.path.abspath(os.path.join(d, rel_path))
    if not full.startswith(os.path.abspath(d)):
        raise ValueError("非法路径")
    if not os.path.exists(full):
        return ""
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def save_file(experiment_id, rel_path, content):
    d = exp_dir(experiment_id)
    full = os.path.abspath(os.path.join(d, rel_path))
    if not full.startswith(os.path.abspath(d)):
        raise ValueError("非法路径")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)
    return rel_path


def cleanup(experiment_id):
    d = os.path.join(WORKSPACE_DIR, f"exp_{experiment_id}")
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
