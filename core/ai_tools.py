"""AI 工具调用安全沙箱（产品建议3 P0-38）。

只暴露显式白名单工具；写操作必须确认后执行；所有调用都写 ai_tool_audit。
工具不执行 shell、不读密钥、不写文件系统，权限继承项目角色。
"""
import json

from core import db

TOOLS = {
    "list_projects": {
        "desc": "列出我可见的项目", "write": False,
        "args": {},
    },
    "list_experiments": {
        "desc": "列出实验，可按项目筛选", "write": False,
        "args": {"project_id": "可选，项目 ID"},
    },
    "get_experiment": {
        "desc": "读取单个实验的基本信息和最近指标", "write": False,
        "args": {"experiment_id": "实验 ID"},
    },
    "recent_metrics": {
        "desc": "读取某个实验最近一次运行的指标", "write": False,
        "args": {"experiment_id": "实验 ID", "limit": "可选，最多条数"},
    },
    "list_failed_runs": {
        "desc": "列出我可见的失败运行", "write": False,
        "args": {"limit": "可选，最多条数"},
    },
    "project_members": {
        "desc": "读取项目成员与角色", "write": False,
        "args": {"project_id": "项目 ID"},
    },
    "move_experiment": {
        "desc": "把实验移动到其他项目（需 owner 并确认）", "write": True,
        "args": {"experiment_id": "实验 ID", "project_id": "目标项目 ID"},
    },
}


def describe():
    return [{"name": name, "desc": spec["desc"], "write": spec["write"],
             "args": spec["args"], "requires_confirm": spec["write"]}
            for name, spec in TOOLS.items()]


def _int(args, key, default=0):
    try:
        return int((args or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _role_for(uid, experiment_id=0, project_id=0):
    if project_id:
        return db.get_project_role(int(project_id), uid=uid)
    if experiment_id:
        exp = db.get_experiment(int(experiment_id), uid=uid)
        if not exp:
            return ""
        return db.experiment_role(exp, uid)
    return "owner"


def _handler(uid, name, args):
    if name == "list_projects":
        return {"projects": [{"id": p["id"], "name": p["name"], "role": p["role"]}
                             for p in db.list_projects(uid=uid)]}

    if name == "list_experiments":
        pid = _int(args, "project_id")
        rows = db.list_experiments(uid=uid, project_id=pid or None,
                                   include_archived_projects=True, limit=50)
        return {"experiments": [{"id": e["id"], "title": e["title"],
                                 "status": e["status"], "project_id": e.get("project_id")}
                                for e in rows]}

    if name == "get_experiment":
        eid = _int(args, "experiment_id")
        exp = db.get_experiment(eid, uid=uid)
        if not exp:
            return {"error": "实验不存在或无权限"}
        latest = db.latest_run(eid)
        return {"experiment": {
            "id": exp["id"], "title": exp["title"], "status": exp["status"],
            "innovation": str(exp.get("innovation") or "")[:600],
            "tags": exp.get("tags") or "",
            "latest_run": {"id": latest["id"], "status": latest["status"]} if latest else None,
        }}

    if name == "recent_metrics":
        eid = _int(args, "experiment_id")
        if not db.get_experiment(eid, uid=uid):
            return {"error": "实验不存在或无权限"}
        latest = db.latest_run(eid)
        if not latest:
            return {"metrics": []}
        rows = db.metrics_series(latest["id"])[:_int(args, "limit", 20) or 20]
        return {"run_id": latest["id"],
                "metrics": [{"name": r.get("name"), "value": r.get("value"),
                             "step": r.get("step")} for r in rows]}

    if name == "list_failed_runs":
        limit = _int(args, "limit", 20) or 20
        rows = db.q("""SELECT r.id, r.experiment_id, r.status, r.exit_code, r.finished_at, e.title
                       FROM runs r JOIN experiments e ON e.id=r.experiment_id
                       WHERE r.user_id=? AND r.status IN ('failed','lost','stopped')
                       ORDER BY r.id DESC LIMIT ?""", (uid, limit))
        return {"runs": [dict(r) for r in rows]}

    if name == "project_members":
        pid = _int(args, "project_id")
        if not db.role_allows(db.get_project_role(pid, uid=uid), "read"):
            return {"error": "项目不存在或无权限"}
        return {"members": [{"id": m["id"], "username": m["username"],
                             "role": m["role"], "global_role": m.get("global_role")}
                            for m in db.list_project_members(pid)]}

    if name == "move_experiment":
        eid = _int(args, "experiment_id")
        pid = _int(args, "project_id")
        exp = db.get_experiment(eid, uid=uid)
        if not exp:
            return {"error": "实验不存在或无权限"}
        if not db.role_allows(db.experiment_role(exp, uid), "delete"):
            return {"error": "只有实验所在项目的 owner 可以移动实验"}
        if not db.role_allows(db.get_project_role(pid, uid=uid), "write"):
            return {"error": "对目标项目没有编辑权限"}
        if not db.set_experiment_project(eid, pid, uid=uid):
            return {"error": "移动失败"}
        return {"ok": True, "experiment_id": eid, "project_id": pid}

    return {"error": "未知工具"}


def call(uid, name, args=None, confirm=False):
    args = args or {}
    spec = TOOLS.get(name)
    if not spec:
        db.add_ai_tool_audit(name, args, {"error": "unknown tool"}, "denied", "", uid=uid)
        return {"ok": False, "error": "工具不在白名单中"}

    role = _role_for(uid, _int(args, "experiment_id"), _int(args, "project_id"))
    if spec["write"] and not confirm:
        db.add_ai_tool_audit(name, args,
                             {"preview": "写操作需要二次确认", "args": args},
                             "preview", role, uid=uid)
        return {"ok": False, "needs_confirm": True,
                "preview": {"tool": name, "args": args, "desc": spec["desc"]}}

    try:
        result = _handler(uid, name, args)
        ok = not result.get("error")
        db.add_ai_tool_audit(name, args, result, "ok" if ok else "denied", role, uid=uid)
        return {"ok": ok, "tool": name, "data": result}
    except Exception as e:
        payload = {"error": str(e)[:300]}
        db.add_ai_tool_audit(name, args, payload, "error", role, uid=uid)
        return {"ok": False, "tool": name, "error": payload["error"]}
