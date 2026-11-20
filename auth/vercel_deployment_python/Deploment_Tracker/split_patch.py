#!/usr/bin/env python3
"""split_app.py - divide app.py into a `tracker/` package, one module per feature.

Run from the folder that contains app.py:
    python split_app.py             -> create tracker/*.py, rewrite app.py
                                       (backup: app.py.bak) and write split_by_function.patch
    python split_app.py --dry-run   -> only write split_by_function.patch and print the plan
    python split_app.py --revert    -> restore app.py and delete the generated files

How it works: code is MOVED verbatim, never rewritten.  Every route keeps
`@app.route(...)` (the single Flask `app` lives in tracker/core.py), so URLs,
endpoint names, url_for(...) calls and templates keep working unchanged.
Imports each module needs are worked out automatically from the code itself.

Modules
    core            Flask app, env, logging, DB connection, role helpers
    constants       dropdown values, statuses, slot rules, Git-link validation
    audit_users     audit identities/passcodes, change history
    itsm            ITSM CR validation + CAB dates
    jenkins_service Jenkins fetch/persist + /api/jenkins routes
    notifications   e-mail wrappers (send_* functions)
    release_calendar  booking form, slot availability, save_deployment
    governance      DevOps Governance page + deployment record APIs
    timers          running / rollback timers and daily status
    manager         manager dashboard + exports
    super_manager   super-manager admin pages
"""
import ast
import builtins
import difflib
import json
import shutil
import sys
import symtable
from pathlib import Path

APP, BACKUP = Path("app.py"), Path("app.py.bak")
PATCH, MANIFEST, PKG = Path("split_by_function.patch"), Path(".split_manifest.json"), Path("tracker")

# module -> (description, names that live there)
MODULES = {
    "core": ("Flask app, environment, logging, DB connection and role helpers.", [
        "BASE_DIR", "app", "PORT", "LOG_DIR", "LOG_FORMAT", "LOG_BACKUP_COUNT",
        "get_connection", "write_audit_log", "is_manager_user",
        "is_super_manager_user", "require_super_manager",
        "daily_file_handler", "console_handler"]),
    "constants": ("Dropdown values, statuses, slot rules and Git-link validation.", [
        "CHANGE_TYPES", "DEPLOYMENT_TYPES", "GIT_OPTIONAL_CATEGORIES", "is_git_optional",
        "VERIFIED_BY_OPTIONS", "PENDING_STATUS", "STATUSES", "APPROVAL_STATUSES",
        "STATUS_EMAIL_STATUSES", "SLOT_STARTS", "get_slot_starts", "SLOT_CONSTRAINT",
        "validate_git_link"]),
    "audit_users": ("Audit identities (e-mail + passcode) and field-level change history.", [
        "AUDIT_PASSCODE_PEPPER", "normalize_audit_email", "audit_passcode_digest",
        "get_audit_user_emails", "verify_audit_user_credentials", "register_audit_user",
        "insert_change_history", "audit_value"]),
    "itsm": ("ITSM change-request validation and CAB dates.", [
        "parse_cab_date", "validate_cr_number", "validate_cr"]),
    "jenkins_service": ("Jenkins build/deploy history: fetching, persistence and API routes.", [
        "REQUEST_TIMEOUT", "REFRESH_SECONDS", "VERIFY_SSL", "TZ_NAME"]),  # + any name containing 'jenkins'
    "notifications": ("E-mail notification entry points.", [
        "send_deployment_notification", "send_status_change_notification"]),
    "release_calendar": ("Release Calendar booking form, slot availability and saving.", [
        "get_release_settings", "get_blocked_date", "validate_deployment_date",
        "generate_rfid", "get_existing_rfid", "save_deployment", "form_context",
        "slot_status", "lookup_cr", "Release_Calender"]),
    "governance": ("DevOps Governance page and deployment-record APIs.", [
        "change_dashboard_data", "get_released_by_users", "resolve_verified_deployed_by",
        "devops_page", "get_deployment_by_cr", "update_devops_deployment",
        "pending_deployments", "deployments_by_date", "CR_DETAIL_JSON_FIELDS",
        "_detail_clean", "_detail_slot_text", "deployment_detail", "decide_deployment_request"]),
    "timers": ("Running / rollback timers and the daily running-status views.", [
        "ensure_running_timer_table", "ensure_timer_history_table", "serialize_running_timer",
        "get_deployment_running_timer", "update_deployment_running_timer", "running_status",
        "running_history", "running_category", "running_daily", "ensure_rollback_timer_tables",
        "get_deployment_rollback_timer", "update_deployment_rollback_timer", "rollback_daily"]),
    "manager": ("Manager dashboard and Excel/CSV exports.", [
        "manager_dashboard", "export_manager_team_deployments",
        "export_manager_deployment_records_excel"]),
    "super_manager": ("Super-manager administration pages.", [
        "super_manager_page", "super_manager_update_capacity", "super_manager_block_date",
        "super_manager_delete_requests", "super_manager_delete_user",
        "super_manager_unlock", "super_manager_logout"]),
}
NAME_HOME = {n: m for m, (_, names) in MODULES.items() for n in names}
BUILTINS = set(dir(builtins)) | {"__file__", "__name__"}


def lookup(name):
    if name in NAME_HOME:
        return NAME_HOME[name]
    if "jenkins" in name.lower():
        return "jenkins_service"
    return None


def analyse(src):
    """Return (names defined at module scope, global names used) for a code snippet."""
    top = symtable.symtable(src, "<snippet>", "exec")
    defined, used = set(), set()
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported():
            defined.add(sym.get_name())
        elif sym.is_referenced():
            used.add(sym.get_name())

    def walk(table):
        for child in table.get_children():
            for sym in child.get_symbols():
                if sym.is_referenced() and sym.is_global():
                    used.add(sym.get_name())
            walk(child)
    walk(top)
    return defined, used - defined


def import_bindings(node):
    out = {}
    if isinstance(node, ast.Import):
        for a in node.names:
            out[(a.asname or a.name.split(".")[0])] = ("import", a.name, a.asname)
    elif isinstance(node, ast.ImportFrom):
        for a in node.names:
            out[a.asname or a.name] = ("from", node.module, a.name, a.asname)
    return out


def render_imports(names, bindings, local):
    plain, froms = set(), {}
    for n in sorted(names):
        b = bindings.get(n)
        if not b:
            continue
        if b[0] == "import":
            plain.add(f"import {b[1]}" + (f" as {b[2]}" if b[2] else ""))
        else:
            froms.setdefault(b[1], set()).add(f"{b[2]} as {b[3]}" if b[3] else b[2])
    lines = sorted(plain) + [fmt_from(m, sorted(i)) for m, i in sorted(froms.items())]
    local_lines = [fmt_from(f"tracker.{m}", sorted(i)) for m, i in sorted(local.items())]
    return lines, local_lines


def fmt_from(module, items):
    line = f"from {module} import {', '.join(items)}"
    if len(line) <= 88:
        return line
    return f"from {module} import (\n" + "".join(f"    {i},\n" for i in items) + ")"


def build_plan(old):
    lines = old.splitlines(True)
    tree = ast.parse(old)
    bindings, items, prev_end = {}, [], 0
    for node in tree.body:
        bindings.update(import_bindings(node))
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        seg = "".join(lines[prev_end:node.end_lineno])
        code = "".join(lines[start - 1:node.end_lineno])
        prev_end = node.end_lineno
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        defined, used = analyse(code)
        items.append({"node": node, "seg": seg, "defined": defined, "used": used, "module": None})

    # 1) rule-based placement
    for it in items:
        node = it["node"]
        if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test):
            it["module"] = "__main__"
            continue
        for n in sorted(it["defined"]):
            m = lookup(n)
            if m:
                it["module"] = m
                break
    # 2) unmapped statements: core (side-effect code); unmapped defs: by their users
    home = {}
    for it in items:
        if it["module"]:
            for n in it["defined"]:
                home.setdefault(n, it["module"])
    auto = []
    for it in items:
        if it["module"]:
            continue
        if isinstance(it["node"], (ast.FunctionDef, ast.ClassDef)):
            name = it["node"].name
            users = {o["module"] for o in items if o["module"] and name in o["used"]}
            it["module"] = users.pop() if len(users) == 1 else "core"
            auto.append((name, it["module"]))
        else:
            it["module"] = "core"
        for n in it["defined"]:
            home.setdefault(n, it["module"])
    return items, bindings, home, auto


def generate(old):
    items, bindings, home, auto = build_plan(old)
    bodies, uses = {}, {}
    for it in items:
        bodies.setdefault(it["module"], []).append(it["seg"].strip("\n"))
        uses.setdefault(it["module"], set()).update(it["used"])

    deps = {}
    for mod, used in uses.items():
        deps[mod] = {home[n] for n in used if n in home and home[n] != mod and home[n] != "__main__"}
    # cycle check
    state = {}

    def visit(m, path):
        if state.get(m) == 2:
            return
        if state.get(m) == 1:
            sys.exit("Circular dependency between modules: " + " -> ".join(path + [m]) +
                     "\nNo files were written.  Move one of those names into 'core' in MODULES.")
        state[m] = 1
        for d in sorted(deps.get(m, ())):
            visit(d, path + [m])
        state[m] = 2
    for m in deps:
        visit(m, [])

    files = {}
    for mod in list(MODULES) + ["__main__"]:
        if mod not in bodies:
            continue
        used = uses[mod]
        local = {}
        for n in sorted(used):
            h = home.get(n)
            if h and h != mod and h != "__main__":
                local.setdefault(h, set()).add(n)
        ext, loc = render_imports(used, bindings, local)
        body = "\n\n\n".join(b for b in bodies[mod] if b.strip())
        if mod == "__main__":
            doc = '"""Entry point. Importing the tracker modules registers all routes on `app`."""'
            reg = [m for m in MODULES if m in bodies and m != "core"]
            registrations = "import " + ", ".join(f"tracker.{m}" for m in reg) + "  # noqa: F401 (registers routes)"
            head = "\n".join(ext + loc + [registrations])
            files["app.py"] = f"{doc}\n{head}\n\n\n{body}\n"
        else:
            if mod == "core":
                fixed = body.replace("Path(__file__).resolve().parent",
                                     "Path(__file__).resolve().parent.parent")
                if fixed == body:
                    print("WARNING: BASE_DIR line not found in core; check template/static paths.")
                body = fixed
            doc = f'"""{MODULES[mod][0]}"""'
            parts = [doc, "\n".join(ext)] if ext else [doc]
            if loc:
                parts.append("\n".join(loc))
            files[f"tracker/{mod}.py"] = "\n\n".join(parts) + f"\n\n\n{body}\n"
    files["tracker/__init__.py"] = '"""Deployment Tracker package (split from app.py by functionality)."""\n'

    # verification: syntax + unresolved global names
    problems = []
    for path, text in files.items():
        try:
            compile(text, path, "exec")
        except SyntaxError as e:
            sys.exit(f"Internal error: {path} is not valid Python ({e}). No files written.")
        defined, used = analyse(text)
        missing = sorted(n for n in used if n not in BUILTINS)
        if missing:
            problems.append((path, missing))
    return files, auto, problems, items


def main():
    if "--revert" in sys.argv:
        if not BACKUP.exists():
            sys.exit("app.py.bak not found.")
        APP.write_text(BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
        for f in json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []:
            Path(f).unlink(missing_ok=True)
        shutil.rmtree(PKG / "__pycache__", ignore_errors=True)
        if PKG.exists() and not any(PKG.iterdir()):
            PKG.rmdir()
        MANIFEST.unlink(missing_ok=True)
        print("Reverted: app.py restored and generated files removed.")
        return

    if not APP.exists():
        sys.exit("Run this next to app.py.")
    old = APP.read_text(encoding="utf-8")
    if "from tracker." in old or "import tracker." in old:
        sys.exit("app.py has already been split.")
    files, auto, problems, items = generate(old)

    counts = {}
    for it in items:
        counts[it["module"]] = counts.get(it["module"], 0) + 1
    print("Plan (top-level blocks per module):")
    for m, c in sorted(counts.items()):
        print(f"  {'app.py' if m == '__main__' else 'tracker/' + m + '.py':32s} {c}")
    if auto:
        print("Placed automatically (not in MODULES):", ", ".join(f"{n}->{m}" for n, m in auto))
    for path, missing in problems:
        print(f"NOTE {path}: names not defined anywhere (also true in the original?): {', '.join(missing)}")

    diff = list(difflib.unified_diff(old.splitlines(True), files["app.py"].splitlines(True),
                                     "a/app.py", "b/app.py"))
    for path, text in files.items():
        if path != "app.py":
            diff += list(difflib.unified_diff([], text.splitlines(True), "/dev/null", f"b/{path}"))
    PATCH.write_text("".join(diff), encoding="utf-8")
    print(f"Wrote {PATCH}.")
    if "--dry-run" in sys.argv:
        print("Dry run: nothing else was changed.")
        return

    clash = [p for p in files if p != "app.py" and Path(p).exists()]
    if clash:
        sys.exit("Refusing to overwrite existing files: " + ", ".join(clash))
    PKG.mkdir(exist_ok=True)
    BACKUP.write_text(old, encoding="utf-8")
    written = []
    for path, text in files.items():
        Path(path).write_text(text, encoding="utf-8")
        if path != "app.py":
            written.append(path)
    MANIFEST.write_text(json.dumps(written))
    print(f"Done. Created {len(written)} files in tracker/, rewrote app.py (backup: app.py.bak).")


if __name__ == "__main__":
    main()