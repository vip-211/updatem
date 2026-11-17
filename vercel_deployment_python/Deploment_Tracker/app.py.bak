import logging
from logging.handlers import TimedRotatingFileHandler
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
import json
import csv
import io
import os
import secrets
import hashlib
import hmac
from datetime import datetime, time, timedelta, date
from pathlib import Path
from urllib.parse import urlparse
import re
import psycopg2
from psycopg2 import errors
from dotenv import load_dotenv
from flask import (Flask, flash, jsonify, redirect, render_template, request, url_for, session, send_file)
import requests
import time as time_module
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "")
if not app.secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY is missing. Add it to your .env file."
    )


# ============================================================
# JENKINS CONFIGURATION
# ============================================================

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.auth import HTTPBasicAuth
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

JENKINS_USER = os.getenv("JENKINS_USER", "devops").strip()
JENKINS_TOKEN = os.getenv("JENKINS_TOKEN", "").strip()
VERIFY_SSL = os.getenv("JENKINS_VERIFY_SSL", "true").strip().lower() == "true"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "5"))
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "120"))
PORT = int(os.getenv("PORT", "5050"))
TZ_NAME = os.getenv("TZ", "Asia/Kolkata")

JENKINS_URLS = [
    item.strip().rstrip("/")
    for item in os.getenv(
        "JENKINS_URLS",
        "https://jenkins-build-linux.rblbank.com,"
        "https://jenkins-deploy-linux.rblbank.com,"
        "https://jenkins-build-win.rblbank.com,"
        "https://jenkins-deploy-win.rblbank.com",
    ).split(",")
    if item.strip()
]

JENKINS_BUILD_PAGE_SIZE = max(1, int(os.getenv("JENKINS_BUILD_PAGE_SIZE", "100")))
# 0 = fetch all available build history for each job.
JENKINS_MAX_BUILDS = max(0, int(os.getenv("JENKINS_MAX_BUILDS", "0")))
JENKINS_JOB_WORKERS = max(1, int(os.getenv("JENKINS_JOB_WORKERS", "8")))

try:
    JENKINS_TIMEZONE = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:
    app.logger.warning("Unknown TZ '%s'; using Asia/Kolkata", TZ_NAME)
    JENKINS_TIMEZONE = ZoneInfo("Asia/Kolkata")

_JENKINS_CACHE = {"created_at": 0.0, "data": None}
_JENKINS_CACHE_LOCK = Lock()


def _jenkins_get_json(url, params=None):
    """GET a Jenkins JSON API endpoint using the configured credentials."""
    response = requests.get(
        url,
        params=params,
        auth=HTTPBasicAuth(JENKINS_USER, JENKINS_TOKEN),
        timeout=REQUEST_TIMEOUT,
        verify=VERIFY_SSL,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def _jenkins_status(result=None, building=False, color=None):
    if building:
        return "RUNNING"
    if result:
        return str(result).upper()
    color = (color or "").lower()
    if "anime" in color:
        return "RUNNING"
    if color.startswith("blue"):
        return "SUCCESS"
    if color.startswith("red"):
        return "FAILURE"
    if color.startswith("yellow"):
        return "UNSTABLE"
    if color.startswith("aborted"):
        return "ABORTED"
    if color.startswith("disabled"):
        return "DISABLED"
    if color.startswith("notbuilt"):
        return "NOT_BUILT"
    return "NOT_BUILT"


def _jenkins_is_container(item):
    class_name = (item.get("_class") or "").lower()
    return any(
        marker in class_name
        for marker in (
            "folder",
            "multibranchproject",
            "organizationfolder",
        )
    )


def _jenkins_discover_jobs(item_url, server_name, server_url, item_hint=None):
    """Recursively traverse folders and multibranch projects."""
    data = _jenkins_get_json(
        item_url.rstrip("/") + "/api/json",
        params={"tree": "name,url,_class,color,jobs[name,url,_class,color]"},
    )
    children = data.get("jobs") or []
    if children:
        found = []
        for child in children:
            child_url = child.get("url")
            if child_url:
                found.extend(
                    _jenkins_discover_jobs(
                        child_url, server_name, server_url, child
                    )
                )
        return found

    job_class = data.get("_class") or (item_hint or {}).get("_class") or ""
    # Empty folders are containers, not buildable jobs.
    if _jenkins_is_container({"_class": job_class}):
        return []

    return [{
        "server": server_name,
        "server_url": server_url,
        "name": data.get("name") or (item_hint or {}).get("name"),
        "url": data.get("url") or item_url,
        "job_class": job_class,
        "color": data.get("color") or (item_hint or {}).get("color"),
        "status": _jenkins_status(color=data.get("color") or (item_hint or {}).get("color")),
    }]


def _jenkins_build_local_time(timestamp_ms):
    if timestamp_ms is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(timestamp_ms) / 1000.0, tz=JENKINS_TIMEZONE
        ).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _jenkins_extract_build_details(data):
    parameters, causes, repositories, branches = [], [], [], []
    commits, commit_records = [], []

    for action in data.get("actions") or []:
        if not isinstance(action, dict):
            continue
        for parameter in action.get("parameters") or []:
            if isinstance(parameter, dict):
                parameters.append({
                    "name": parameter.get("name"),
                    "value": parameter.get("value"),
                })
        for cause in action.get("causes") or []:
            if isinstance(cause, dict):
                causes.append({
                    "user": cause.get("userName"),
                    "user_id": cause.get("userId"),
                    "description": cause.get("shortDescription"),
                    "upstream_project": cause.get("upstreamProject"),
                    "upstream_build": cause.get("upstreamBuild"),
                })
        repositories.extend(action.get("remoteUrls") or [])
        revision = action.get("lastBuiltRevision") or {}
        for branch in revision.get("branch") or []:
            if branch.get("name"):
                branches.append(branch["name"])

    for change_set in data.get("changeSets") or []:
        for item in change_set.get("items") or []:
            if not isinstance(item, dict):
                continue
            commit_id = item.get("commitId")
            if commit_id:
                commits.append(commit_id)
            author = item.get("author") or {}
            author_name = (
                author.get("fullName") if isinstance(author, dict)
                else str(author) if author else None
            )
            commit_records.append({
                "commit_id": commit_id,
                "author": author_name,
                "message": item.get("msg") or item.get("comment"),
            })

    triggering_user = next(
        (cause.get("user") or cause.get("user_id") for cause in causes
         if cause.get("user") or cause.get("user_id")),
        None,
    )
    return {
        "parameters": parameters,
        "causes": causes,
        "triggered_by": triggering_user,
        "git_branches": sorted(set(branches)),
        "git_repositories": sorted(set(repositories)),
        "commit_ids": sorted(set(commits)),
        "commits": commit_records,
    }


def _jenkins_normalize_cr(value):
    """Normalize CR values without dropping meaningful leading zeroes."""
    value = str(value or "").strip()
    value = re.sub(r"^CR[\s:_-]*", "", value, flags=re.IGNORECASE)
    return value if value.isdigit() else ""


def _jenkins_extract_cr_numbers(data, job_name=None):
    """Find explicit CR references in Jenkins parameters and build metadata."""
    found = set()
    cr_pattern = re.compile(r"\bCR[\s:_-]*([0-9]+)\b", re.IGNORECASE)
    cr_parameter_names = {
        "cr", "crno", "crnumber", "crnum", "crid",
        "changerequest", "changerequestno", "changerequestnumber",
    }

    for action in data.get("actions") or []:
        if not isinstance(action, dict):
            continue
        for parameter in action.get("parameters") or []:
            if not isinstance(parameter, dict):
                continue
            name = re.sub(r"[^a-z0-9]", "", str(parameter.get("name") or "").lower())
            value = str(parameter.get("value") or "").strip()
            if name in cr_parameter_names or name.startswith("crnumber"):
                normalized = _jenkins_normalize_cr(value)
                if normalized:
                    found.add(normalized)
            for match in cr_pattern.finditer(value):
                found.add(match.group(1))

    for field in ("displayName", "fullDisplayName", "description"):
        value = str(data.get(field) or "")
        found.update(match.group(1) for match in cr_pattern.finditer(value))

    # Job names are accepted only when they contain an explicit CR-<digits> token.
    for match in cr_pattern.finditer(str(job_name or "")):
        found.add(match.group(1))
    return sorted(found)


def _jenkins_execution_metadata(job):
    server_url = str(job.get("server_url") or "").lower()
    server_name = str(job.get("server") or "").lower()
    job_name = str(job.get("name") or "").lower()
    combined = " ".join((server_url, server_name, job_name))

    if "deploy" in combined:
        build_type = "Deploy"
    elif "build" in combined:
        build_type = "Build"
    else:
        build_type = "Unknown"

    if any(token in combined for token in ("windows", "win-", "-win", "win/")):
        platform = "Windows"
    elif "linux" in combined:
        platform = "Linux"
    else:
        platform = "Unknown"
    return build_type, platform


def _jenkins_fetch_build(build_summary, job=None):
    build_url = build_summary.get("url")
    if not build_url:
        return None
    data = _jenkins_get_json(
        build_url.rstrip("/") + "/api/json",
        params={
            "tree": (
                "number,url,result,timestamp,duration,estimatedDuration,building,"
                "displayName,description,fullDisplayName,"
                "actions[parameters[name,value],"
                "causes[userName,userId,shortDescription,upstreamProject,upstreamBuild],"
                "remoteUrls,lastBuiltRevision[branch[name]]],"
                "changeSets[items[commitId,author[fullName],msg,comment]]"
            )
        },
    )
    timestamp = data.get("timestamp")
    duration_ms = data.get("duration")
    building = bool(data.get("building", False))
    job = job or {}
    build_type, platform = _jenkins_execution_metadata(job)
    details = _jenkins_extract_build_details(data)
    start_time = _jenkins_build_local_time(timestamp)
    end_time = None
    if timestamp is not None and duration_ms is not None and not building:
        try:
            end_time = _jenkins_build_local_time(float(timestamp) + float(duration_ms))
        except (TypeError, ValueError, OverflowError):
            end_time = None
    return {
        "number": data.get("number"),
        "url": data.get("url") or build_url,
        "job_name": job.get("name"),
        "server": job.get("server"),
        "server_url": job.get("server_url"),
        "display_name": data.get("displayName"),
        "full_display_name": data.get("fullDisplayName"),
        "description": data.get("description"),
        "result": data.get("result"),
        "status": _jenkins_status(data.get("result"), building),
        "building": building,
        "timestamp": timestamp,
        "timestamp_local": start_time,
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": duration_ms,
        "estimated_duration_ms": data.get("estimatedDuration"),
        "build_type": build_type,
        "platform": platform,
        "cr_numbers": _jenkins_extract_cr_numbers(data, job.get("name")),
        **details,
    }


def _persist_jenkins_builds(builds):
    """Persist each Jenkins build against every explicitly detected CR number."""
    rows = []
    for build in builds or []:
        build_url = str(build.get("url") or "").strip()
        if not build_url:
            continue
        for raw_cr in build.get("cr_numbers") or []:
            cr_number = _jenkins_normalize_cr(raw_cr)
            if not cr_number:
                continue
            rows.append((
                cr_number, build_url, build.get("job_name"),
                str(build.get("number")) if build.get("number") is not None else None,
                build.get("server"), build.get("build_type"), build.get("platform"),
                build.get("start_time"), build.get("end_time"),
                build.get("triggered_by"), build.get("status"),
                bool(build.get("building")), build.get("duration_ms"),
                json.dumps({
                    "display_name": build.get("display_name"),
                    "full_display_name": build.get("full_display_name"),
                    "description": build.get("description"),
                    "causes": build.get("causes") or [],
                    "parameters": build.get("parameters") or [],
                    "git_branches": build.get("git_branches") or [],
                    "git_repositories": build.get("git_repositories") or [],
                    "commit_ids": build.get("commit_ids") or [],
                    "commits": build.get("commits") or [],
                }, default=str),
            ))
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.jenkins_build_history (
                cr_number TEXT NOT NULL,
                build_url TEXT NOT NULL,
                job_name TEXT,
                build_number TEXT,
                server TEXT,
                build_type TEXT,
                platform TEXT,
                start_time TIMESTAMPTZ,
                end_time TIMESTAMPTZ,
                triggered_by TEXT,
                status TEXT,
                building BOOLEAN NOT NULL DEFAULT FALSE,
                duration_ms BIGINT,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (build_url, cr_number)
            )
        """)
        if rows:
            cur.executemany("""
                INSERT INTO public.jenkins_build_history (
                cr_number, build_url, job_name, build_number, server,
                build_type, platform, start_time, end_time, triggered_by,
                status, building, duration_ms, metadata, last_seen_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW()
            )
            ON CONFLICT (build_url, cr_number) DO UPDATE SET
                job_name = EXCLUDED.job_name,
                build_number = EXCLUDED.build_number,
                server = EXCLUDED.server,
                build_type = EXCLUDED.build_type,
                platform = EXCLUDED.platform,
                start_time = COALESCE(EXCLUDED.start_time, public.jenkins_build_history.start_time),
                end_time = COALESCE(EXCLUDED.end_time, public.jenkins_build_history.end_time),
                triggered_by = COALESCE(EXCLUDED.triggered_by, public.jenkins_build_history.triggered_by),
                status = EXCLUDED.status,
                building = EXCLUDED.building,
                duration_ms = COALESCE(EXCLUDED.duration_ms, public.jenkins_build_history.duration_ms),
                metadata = EXCLUDED.metadata,
                last_seen_at = NOW()
        """, rows)
        conn.commit()
        return len(rows)
    except Exception:
        if conn:
            conn.rollback()
        app.logger.exception("Could not persist Jenkins build history")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def _load_persisted_jenkins_builds(cr_number):
    """Load saved Jenkins build records for a normalized CR number."""
    normalized = _jenkins_normalize_cr(cr_number)
    if not normalized:
        return []
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT cr_number, build_url, job_name, build_number, server,
                   build_type, platform, start_time, end_time, triggered_by,
                   status, building, duration_ms, metadata
            FROM public.jenkins_build_history
            WHERE cr_number = %s
            ORDER BY start_time DESC NULLS LAST, build_number DESC NULLS LAST
        """, (normalized,))
        columns = [d[0] for d in cur.description]
        saved = []
        for row in cur.fetchall():
            item = dict(zip(columns, row))
            item["url"] = item.pop("build_url")
            item["number"] = item.pop("build_number")
            for key in ("start_time", "end_time"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            metadata = item.pop("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except ValueError:
                    metadata = {}
            item.update(metadata if isinstance(metadata, dict) else {})
            saved.append(item)
        return saved
    except Exception:
        # An older installation may not have run the table-creation path yet.
        app.logger.exception("Could not load saved Jenkins build history")
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def _all_jenkins_builds(jenkins_data):
    return [
        build
        for server in (jenkins_data or {}).get("servers") or []
        for job in server.get("jobs") or []
        for build in job.get("builds") or []
    ]


def _jenkins_fetch_job_history(job):
    """Fetch build summaries in pages, then retrieve details for each build."""
    builds, offset = [], 0
    while True:
        page_size = JENKINS_BUILD_PAGE_SIZE
        if JENKINS_MAX_BUILDS:
            remaining = JENKINS_MAX_BUILDS - offset
            if remaining <= 0:
                break
            page_size = min(page_size, remaining)
        end = offset + page_size
        response = _jenkins_get_json(
            job["url"].rstrip("/") + "/api/json",
            params={
                "tree": (
                    "builds[number,url,result,timestamp,duration,building]"
                    f"{{{offset},{end}}}"
                )
            },
        )
        page = response.get("builds") or []
        if not page:
            break
        builds.extend(page)
        if len(page) < page_size:
            break
        offset = end

    detailed = []
    for summary in builds:
        try:
            build = _jenkins_fetch_build(summary, job)
            if build:
                detailed.append(build)
        except requests.RequestException as exc:
            app.logger.warning(
                "Could not fetch Jenkins build %s: %s", summary.get("url"), exc
            )
            detailed.append({
                "number": summary.get("number"),
                "url": summary.get("url"),
                "result": summary.get("result"),
                "status": _jenkins_status(
                    summary.get("result"), summary.get("building", False)
                ),
                "building": bool(summary.get("building", False)),
                "timestamp": summary.get("timestamp"),
                "timestamp_local": _jenkins_build_local_time(summary.get("timestamp")),
                "duration_ms": summary.get("duration"),
                "start_time": _jenkins_build_local_time(summary.get("timestamp")),
                "end_time": None,
                "job_name": job.get("name"),
                "server": job.get("server"),
                "server_url": job.get("server_url"),
                "build_type": _jenkins_execution_metadata(job)[0],
                "platform": _jenkins_execution_metadata(job)[1],
                "cr_numbers": [],
                "error": str(exc),
            })

    job["builds"] = detailed
    job["build_count"] = len(detailed)
    job["last_build"] = detailed[0] if detailed else None
    return job


def _jenkins_fetch_queue(server_url):
    data = _jenkins_get_json(
        server_url.rstrip("/") + "/queue/api/json",
        params={
            "tree": (
                "items[id,why,blocked,buildable,stuck,inQueueSince,"
                "task[name,url],actions[parameters[name,value]]]"
            )
        },
    )
    queue = []
    for item in data.get("items") or []:
        task = item.get("task") or {}
        queue.append({
            "id": item.get("id"),
            "job_name": task.get("name"),
            "job_url": task.get("url"),
            "why": item.get("why"),
            "blocked": item.get("blocked"),
            "buildable": item.get("buildable"),
            "stuck": item.get("stuck"),
            "in_queue_since": item.get("inQueueSince"),
            "actions": item.get("actions") or [],
            "status": "QUEUED",
        })
    return queue


def _jenkins_fetch_server(server_url):
    parsed = urlparse(server_url)
    server_name = parsed.hostname or server_url
    result = {
        "server": server_name,
        "server_url": server_url,
        "success": False,
        "jobs": [],
        "job_count": 0,
        "build_count": 0,
        "queue": [],
        "queue_count": 0,
        "error": None,
    }
    try:
        root = _jenkins_get_json(
            server_url.rstrip("/") + "/api/json",
            params={"tree": "jobs[name,url,_class,color]"},
        )
        jobs = []
        for item in root.get("jobs") or []:
            if item.get("url"):
                jobs.extend(
                    _jenkins_discover_jobs(
                        item["url"], server_name, server_url, item
                    )
                )

        if jobs:
            with ThreadPoolExecutor(max_workers=JENKINS_JOB_WORKERS) as pool:
                futures = [pool.submit(_jenkins_fetch_job_history, job) for job in jobs]
                for future in as_completed(futures):
                    try:
                        result["jobs"].append(future.result())
                    except Exception:
                        app.logger.exception("A Jenkins job could not be fetched on %s", server_name)

        result["jobs"].sort(key=lambda job: (job.get("name") or "").lower())
        result["job_count"] = len(result["jobs"])
        result["build_count"] = sum(job.get("build_count", 0) for job in result["jobs"])
        result["queue"] = _jenkins_fetch_queue(server_url)
        result["queue_count"] = len(result["queue"])
        result["success"] = True
    except requests.RequestException as exc:
        result["error"] = str(exc)
        app.logger.exception("Jenkins server request failed: %s", server_url)
    except (ValueError, KeyError) as exc:
        result["error"] = str(exc)
        app.logger.exception("Invalid Jenkins response from: %s", server_url)
    return result


def fetch_all_jenkins_data(force_refresh=False):
    """Fetch all configured Jenkins servers; cache successful/partial results."""
    now = time_module.monotonic()
    with _JENKINS_CACHE_LOCK:
        cached = _JENKINS_CACHE.get("data")
        age = now - _JENKINS_CACHE.get("created_at", 0.0)
        if cached is not None and not force_refresh and age < REFRESH_SECONDS:
            return cached

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(JENKINS_URLS))) as pool:
        futures = {pool.submit(_jenkins_fetch_server, url): url for url in JENKINS_URLS}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "server": urlparse(futures[future]).hostname,
                    "server_url": futures[future],
                    "success": False,
                    "jobs": [],
                    "job_count": 0,
                    "build_count": 0,
                    "queue": [],
                    "queue_count": 0,
                    "error": str(exc),
                })

    results.sort(key=lambda server: server.get("server_url", ""))
    response = {
        "success": bool(results) and any(server["success"] for server in results),
        "total_servers": len(results),
        "successful_servers": sum(1 for server in results if server["success"]),
        "failed_servers": sum(1 for server in results if not server["success"]),
        "total_jobs": sum(server["job_count"] for server in results),
        "total_builds": sum(server["build_count"] for server in results),
        "total_queued": sum(server["queue_count"] for server in results),
        "refreshed_at": datetime.now(JENKINS_TIMEZONE).isoformat(),
        "servers": results,
    }
    with _JENKINS_CACHE_LOCK:
        _JENKINS_CACHE["created_at"] = time_module.monotonic()
        _JENKINS_CACHE["data"] = response
    return response


@app.route("/api/jenkins/all", methods=["GET"])
def api_jenkins_all():
    """Combined jobs, build history, build metadata and queue from all servers."""
    if not JENKINS_USER or not JENKINS_TOKEN:
        return jsonify({
            "success": False,
            "message": "Configure JENKINS_USER and JENKINS_TOKEN.",
        }), 500
    force_refresh = request.args.get("refresh", "false").lower() in ("1", "true", "yes")
    try:
        data = fetch_all_jenkins_data(force_refresh=force_refresh)
        return jsonify(data), (200 if data["success"] else 502)
    except Exception:
        app.logger.exception("Unable to fetch combined Jenkins data")
        return jsonify({
            "success": False,
            "message": "Unable to fetch Jenkins data. Check application logs.",
        }), 500


def _jenkins_builds_for_cr(jenkins_data, cr_number):
    normalized_cr = _jenkins_normalize_cr(cr_number)
    if not normalized_cr:
        return []
    matches = []
    for server in (jenkins_data or {}).get("servers") or []:
        for job in server.get("jobs") or []:
            for build in job.get("builds") or []:
                if normalized_cr in (build.get("cr_numbers") or []):
                    matches.append(build)
    matches.sort(key=lambda item: item.get("timestamp") or 0, reverse=True)
    return matches


@app.route("/api/jenkins/by-cr/<path:cr_number>", methods=["GET"])
def api_jenkins_by_cr(cr_number):
    """Return saved Jenkins history for a CR, refreshing it when Jenkins is available."""
    normalized_cr = _jenkins_normalize_cr(cr_number)
    if not normalized_cr:
        return jsonify({"success": False, "message": "Enter a valid CR number."}), 400
    try:
        # Ensure the history table exists and load the last successfully saved snapshot.
        _persist_jenkins_builds([])
        saved_builds = _load_persisted_jenkins_builds(normalized_cr)

        if not JENKINS_USER or not JENKINS_TOKEN:
            return jsonify({
                "success": True,
                "cr_number": f"CR-{normalized_cr}",
                "builds": saved_builds,
                "count": len(saved_builds),
                "jenkins_available": False,
                "message": "Showing saved Jenkins history; configure Jenkins credentials to refresh." if saved_builds else "Jenkins credentials are not configured and no saved history exists for this CR.",
            })

        try:
            data = fetch_all_jenkins_data()
            live_builds = _jenkins_builds_for_cr(data, normalized_cr)
            _persist_jenkins_builds(live_builds)
            saved_builds = _load_persisted_jenkins_builds(normalized_cr)
            return jsonify({
                "success": True,
                "cr_number": f"CR-{normalized_cr}",
                "builds": saved_builds,
                "count": len(saved_builds),
                "jenkins_available": bool(data.get("success")),
                "refreshed_at": data.get("refreshed_at"),
                "message": "Matching Jenkins build/deploy records loaded and saved." if saved_builds else "No Jenkins build with this CR number was found in the available build history.",
            })
        except Exception:
            app.logger.exception("Jenkins CR refresh failed for %s; returning saved history", normalized_cr)
            return jsonify({
                "success": True,
                "cr_number": f"CR-{normalized_cr}",
                "builds": saved_builds,
                "count": len(saved_builds),
                "jenkins_available": False,
                "message": "Jenkins is currently unavailable. Showing previously saved history." if saved_builds else "Jenkins is unavailable and no saved history exists for this CR.",
            })
    except Exception:
        app.logger.exception("Jenkins CR history storage failed for %s", normalized_cr)
        return jsonify({"success": False, "message": "Unable to read or save Jenkins history. Check the database connection and application logs."}), 500


AUDIT_PASSCODE_PEPPER = os.getenv("AUDIT_PASSCODE_PEPPER") or app.secret_key

LOG_DIR = BASE_DIR / "logs"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_BACKUP_COUNT = 90

if os.getenv("VERCEL"):
    # Vercel uses an ephemeral filesystem; send logs to stdout so they
    # remain available in Vercel Function Logs.
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler()],
        force=True,
    )
else:
    # Local/server deployment: write to logs/app.log and rotate at midnight.
    # Archived files are named app.log.YYYY-MM-DD and 90 archives are retained.
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    daily_file_handler = TimedRotatingFileHandler(
        filename=str(LOG_DIR / "app.log"),
        when="midnight",
        interval=1,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
        utc=False,
    )
    daily_file_handler.suffix = "%Y-%m-%d"
    daily_file_handler.setLevel(logging.INFO)
    daily_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    logging.basicConfig(
        level=logging.INFO,
        handlers=[daily_file_handler, console_handler],
        force=True,
    )


CHANGE_TYPES = [
    "Normal CR",
    "Emergency CR",
    "Standard CR",
    "Forward Fix CR",
    "Bug Fix CR",
]

DEPLOYMENT_TYPES = ["Automation", "Manual"]

VERIFIED_BY_OPTIONS = ["DBA TEAM", "MW TEAM", "DevOps Team"]

# ---------------------------------------------------------------------------
# Deployment statuses (these are the ONLY statuses offered to users).
# "Scheduled Deployment" replaces the old "Pending" status and is the default
# status of every newly submitted request.
# ---------------------------------------------------------------------------
PENDING_STATUS = "Scheduled Deployment"
STATUSES = [
    "Scheduled Deployment",
    "Deployment Completed",
    "Deployment Failed",
    "On Hold",
    "Postponed",
    "Rolled",
    "Cancelled",
]
# Written only by the Approve / Reject buttons on the approval screen.
# They are not shown in the status dropdown, but are accepted when a record
# that already carries one of them is saved again.
APPROVAL_STATUSES = ["Approved", "Rejected"]
STATUS_EMAIL_STATUSES = {
    "Approved",
    "Rejected",
    "Cancelled",
    "Deployment Completed",
    "Deployment Failed",
}

SLOT_STARTS = ["18:30","19:00","19:30","20:00","20:30","21:00","21:30","22:00","22:30","23:00"]


def get_slot_starts(change_type=None):
    """Emergency CRs may use any half-hour start time across the full day."""
    if (change_type or "").strip().casefold() == "emergency cr":
        return [
            (datetime.combine(date.today(), time.min) + timedelta(minutes=30 * index)).strftime("%H:%M")
            for index in range(48)
        ]
    return SLOT_STARTS


SLOT_CONSTRAINT = "no_overlapping_deployment_slots"
def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=15,
        application_name="Flask Deployment Tracker",
    )

# def get_connection():
#     return psycopg2.connect(
#         host=os.getenv("DB_HOST"),
#         port=int(os.getenv("DB_PORT", "5432")),
#         dbname=os.getenv("DB_NAME"),
#         user=os.getenv("DB_USER"),
#         password=os.getenv("DB_PASSWORD"),
#         sslmode="disable",
#         connect_timeout=10
#     )

def normalize_audit_email(value):
    """Normalize and validate an email used as a Modified By identity."""
    email = (value or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address for Modified By.")
    return email

def validate_git_link(url):
    """Accept only HTTPS clone URLs from approved Git hosts ending in .git."""
    url = (url or "").strip()
    if not url:
        return False

    try:
        parsed = urlparse(url)
        allowed_hosts = {
            "github.com",
            "gitlab.com",
            "bitbucket.org",
            "apnagit.rblbank.com",
        }
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() in allowed_hosts
            and bool(parsed.path.strip("/"))
            and parsed.path.rstrip("/").lower().endswith(".git")
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except (ValueError, AttributeError):
        return False

def audit_passcode_digest(passcode):
    """Return a deterministic keyed digest so passcodes are not stored as text."""
    return hmac.new(
        AUDIT_PASSCODE_PEPPER.encode("utf-8"),
        ("deployment-tracker-audit-v1:" + passcode).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def get_audit_user_emails():
    """Fetch registered audit identities for the Modified By dropdown."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM public.audit_users ORDER BY email")
            return [row[0] for row in cur.fetchall()]
    except Exception:
        app.logger.exception("Could not load audit user registry; run audit_user_registry.sql")
        return []
    finally:
        if conn is not None:
            conn.close()


def verify_audit_user_credentials(email_value, passcode):
    """Verify email + its unique 4-digit code and apply a five-failure lockout."""
    try:
        email = normalize_audit_email(email_value)
    except ValueError as exc:
        return False, str(exc), 400

    if not re.fullmatch(r"\d{4}", str(passcode or "")):
        return False, "Enter the 4-digit passcode for this email address.", 400

    conn = None
    try:
        conn = get_connection()
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT passcode_digest, failed_attempts, locked_until
                FROM public.audit_users
                WHERE email = %s
                FOR UPDATE
                """,
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False, "This email is not registered. Select Add Email to register it.", 401

            stored_digest, failed_attempts, locked_until = row
            now = datetime.now()
            if locked_until and locked_until > now:
                remaining = max(1, int((locked_until - now).total_seconds() // 60) + 1)
                conn.commit()
                return False, f"Too many failed attempts. Try again in about {remaining} minute(s).", 429

            if locked_until and locked_until <= now:
                failed_attempts = 0
                cur.execute(
                    "UPDATE public.audit_users SET failed_attempts = 0, locked_until = NULL WHERE email = %s",
                    (email,),
                )

            candidate_digest = audit_passcode_digest(str(passcode))
            if hmac.compare_digest(str(stored_digest).strip(), candidate_digest):
                cur.execute(
                    """
                    UPDATE public.audit_users
                    SET failed_attempts = 0,
                        locked_until = NULL,
                        last_used_at = CURRENT_TIMESTAMP
                    WHERE email = %s
                    """,
                    (email,),
                )
                conn.commit()
                return True, email, 200

            new_failures = int(failed_attempts or 0) + 1
            if new_failures >= 5:
                cur.execute(
                    """
                    UPDATE public.audit_users
                    SET failed_attempts = %s,
                        locked_until = %s
                    WHERE email = %s
                    """,
                    (new_failures, now + timedelta(minutes=15), email),
                )
                conn.commit()
                return False, "Five incorrect attempts reached. This email is locked for 15 minutes.", 429

            cur.execute(
                "UPDATE public.audit_users SET failed_attempts = %s WHERE email = %s",
                (new_failures, email),
            )
            conn.commit()
            return False, f"Incorrect passcode. {5 - new_failures} attempt(s) remaining before a 15-minute lock.", 401

    except Exception:
        if conn is not None:
            conn.rollback()
        app.logger.exception("Audit user passcode verification failed")
        return False, "Audit identity storage is unavailable. Run audit_user_registry.sql and retry.", 500
    finally:
        if conn is not None:
            conn.close()

def parse_cab_date(value):
    """Convert the ITSM CAB date display value to a Python date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("The ITSM API did not return a CAB date for this CR.")

    # Prefer ISO and Indian display formats; include common ITSM date formats.
    formats = (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%m/%d/%Y",

       
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(
            f"Unable to understand CAB date returned by ITSM: {raw}"
        ) from exc



@app.route("/api/change-dashboard", methods=["GET"])
def change_dashboard_data():
    """Return deployment records using the actual deployment_tracker columns."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                id, cr_number, rfid, deployment_date, slot_start, slot_end,
                slots, application_name, deployment_count, deployment_type,
                change_type, released_by, verified_and_deployed_by,
                change_requestor, business_unit, environment, git_links,
                incident_number, roll_back, status, comments, team_allocations
            FROM public.deployment_tracker
            ORDER BY deployment_date DESC NULLS LAST,
                     slot_start DESC NULLS LAST,
                     id DESC
        """)

        columns = [description[0] for description in cursor.description]
        deployments = []

        for row in cursor.fetchall():
            record = dict(zip(columns, row))

            for key, value in record.items():
                if isinstance(value, (datetime, date, time)):
                    record[key] = value.isoformat()

            # Handle JSON/JSONB whether psycopg2 returns decoded values or text.
            for json_field in ("git_links", "team_allocations"):
                value = record.get(json_field)
                if isinstance(value, str):
                    try:
                        record[json_field] = json.loads(value)
                    except (TypeError, ValueError):
                        record[json_field] = []

            deployments.append(record)

        return jsonify({
            "success": True,
            "count": len(deployments),
            "deployments": deployments
        })

    except Exception:
        app.logger.exception("Unable to load Change Dashboard records")
        return jsonify({
            "success": False,
            "message": "Unable to load deployment records. Check the Flask logs."
        }), 500

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()



@app.route("/api/validate-cr", methods=["GET"])
def validate_cr():

    cr_number = (request.args.get("cr_number") or "").strip()

    if not cr_number:
        return jsonify({
            "success": False,
            "message": "CR Number is required."
        }), 400

    try:
        # Validate CR from ITSM
        cr_details = validate_cr_number(cr_number)

        # Check RFID in database
        conn = get_connection()

        with conn.cursor() as cur:
            cur.execute("""
                SELECT rfid
                FROM public.deployment_tracker
                WHERE LOWER(TRIM(cr_number)) = LOWER(TRIM(%s))
                LIMIT 1
            """, (cr_number,))

            row = cur.fetchone()

        conn.close()

        allowed_dates = [
            parse_cab_date(d).isoformat()
            for d in cr_details.get("cab_dates", [])
        ]

        return jsonify({
            "success": True,
            "approved": True,
            "change_requestor": cr_details.get("change_requestor"),
            "application_name": cr_details.get("application_name"),
            "business_unit": cr_details.get("business_unit"),
            "environment": cr_details.get("environment"),
            "cab_dates": allowed_dates,
            "cab_date_iso": allowed_dates[0] if allowed_dates else None,
            "change_type": cr_details.get("change_type"),
            "exists": bool(row),
            "rfid": row[0] if row else None
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

@app.route("/api/audit-users/register", methods=["POST"])
def register_audit_user():
    """Register a new email and return its unique passcode only once."""
    payload = request.get_json(silent=True) or {}
    try:
        email = normalize_audit_email(payload.get("email"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    conn = None
    try:
        conn = get_connection()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.audit_users WHERE email = %s", (email,))
            if cur.fetchone():
                return jsonify({
                    "success": False,
                    "message": "This email is already registered. Select it and enter its passcode.",
                }), 409

            for _ in range(500):
                passcode = f"{secrets.randbelow(10000):04d}"
                digest = audit_passcode_digest(passcode)
                cur.execute(
                    """
                    INSERT INTO public.audit_users (email, passcode_digest)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING email
                    """,
                    (email, digest),
                )
                if cur.fetchone():
                    app.logger.info("Audit identity registered: %s", email)
                    return jsonify({
                        "success": True,
                        "email": email,
                        "passcode": passcode,
                        "message": "Email registered. Save this 4-digit passcode now; it will not be shown again.",
                    }), 201
                cur.execute("SELECT 1 FROM public.audit_users WHERE email = %s", (email,))
                if cur.fetchone():
                    return jsonify({
                        "success": False,
                        "message": "This email is already registered. Select it and enter its passcode.",
                    }), 409

            return jsonify({
                "success": False,
                "message": "Could not find an unused 4-digit passcode. Contact the application administrator.",
            }), 503
    except Exception:
        app.logger.exception("Audit user registration failed")
        return jsonify({
            "success": False,
            "message": "Could not register the email. Ensure audit_user_registry.sql has been applied.",
        }), 500
    finally:
        if conn is not None:
            conn.close()


def is_manager_user():
    return str(session.get("role", "")).strip().lower() == "manager"


def is_super_manager_user():
    """Super-manager access is separate from ordinary manager access."""
    return str(session.get("role", "")).strip().lower().replace(" ", "_") == "super_manager"


def require_super_manager():
    if not is_super_manager_user():
        return jsonify({"success": False, "message": "Super Manager access is required."}), 403
    return None


def get_release_settings(cur=None):
    """Return global slot capacity and blocked-date settings."""
    own_conn = None
    try:
        if cur is None:
            own_conn = get_connection()
            cur = own_conn.cursor()
        cur.execute("SELECT max_cr_per_slot FROM public.release_calendar_config WHERE id = 1")
        row = cur.fetchone()
        capacity = int(row[0]) if row and row[0] else 1
        return max(1, capacity)
    finally:
        if own_conn is not None:
            cur.close()
            own_conn.close()


def get_blocked_date(cur, deployment_date):
    cur.execute(
        "SELECT message FROM public.release_calendar_blocks WHERE blocked_date = %s",
        (deployment_date,),
    )
    row = cur.fetchone()
    return row[0] if row else None

def validate_deployment_date(
    deployment_date,
    is_manager=False,
    change_type=None,
):
    if is_manager:
        return

    if deployment_date < date.today():
        raise ValueError(
            "Past deployment dates are not allowed."
        )

    # All change types may now be scheduled on weekdays and weekends.
    # Existing slot-time and slot-capacity validation remains unchanged.
    return

def write_audit_log(level, message,cr_number=None,rfid=None):
    """Write to the application log and, when available, the DB log."""
    app.logger.log(
        getattr(logging, level.upper(), logging.INFO),
        message,
    )
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.deployment_tracker_logs
                        (
                            event_time,
                            log_level,
                            message,
                            cr_number,
                            rfid
                        )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        datetime.now(),
                        level.upper(),
                        message[:2000],
                        cr_number,
                        rfid,
                    ),
                )
    except Exception:
        app.logger.exception(
            "Could not write to deployment_tracker_logs"
        )
    finally:
        if conn is not None:
            conn.close()

def insert_change_history(cur, deployment_id, cr_number, rfid, modified_by, action, changes):
    """Persist field-level audit history in the same transaction as the update."""
    cur.execute(
        """
        INSERT INTO public.deployment_change_history
            (deployment_id, cr_number, rfid, modified_by, action, changes)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            deployment_id,
            cr_number,
            rfid,
            modified_by,
            action,
            json.dumps(changes, default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value)),
        ),
    )


def audit_value(value):
    """Convert database values to JSON-friendly values for old/new snapshots."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def generate_rfid():
    return f"{secrets.randbelow(100_000_000):08d}"

def get_existing_rfid(cur, cr_number):
    """Find the RFID associated with a CR, ignoring case and spaces."""
    cur.execute(
        """
        SELECT rfid
        FROM public.deployment_tracker
        WHERE LOWER(TRIM(cr_number)) = LOWER(TRIM(%s))
          AND rfid IS NOT NULL
        ORDER BY id
        LIMIT 1
        """,
        (cr_number,),
    )
    row = cur.fetchone()
    return row[0] if row else None
def validate_cr_number(cr_number):

    change_id = re.sub(
        r"^CR-",
        "",
        str(cr_number).strip(),
        flags=re.IGNORECASE
    )

    url = f"https://itsupportuat.rblbank.com/api/v3/changes/{change_id}"


    headers = {
        "TECHNICIAN_KEY": "1A112622-14BF-4A6C-A897-8888870B2526",
        "Accept": "application/json"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )


        response.raise_for_status()

        data = response.json()

        if data.get("response_status", {}).get("status") != "success":
            raise ValueError(
                f"CR-{change_id} not found."
            )

        change = data.get("change", {})

        change_requestor = (
            change.get("change_requester", {})
            .get("email_id", "")
        )

        change_type = (
            change.get("change_type", {})
            .get("name", "")
        )

        change_type_mapping = {
            "Normal Change": "Normal CR",
            "Emergency Change": "Emergency CR",
            "Standard Change": "Standard CR"
        }

        change_type = change_type_mapping.get(
            change_type,
            change_type
        )

        application_name = (
            change.get("subcategory", {})
            .get("name", "")
        )

        udf = change.get("udf_fields") or {}

        udf = change.get("udf_fields") or {}

        business_unit = str(
            udf.get("udf_sline_39302") or ""
        ).strip()
        
        environment = str(
            udf.get("udf_sline_10801") or ""
        ).strip()

        cr_status = (
            (change.get("status") or {})
            .get("name", "")
            .strip()
        )

        approval_status = (
            (change.get("approval_status") or {})
            .get("name", "")
            .strip()
            )

        stage_name = (
            (change.get("stage") or {})
            .get("name", "")
            .strip()
        )

        if cr_status in ["Rejected","Completed", "Closed", "Cancelled"]:
            raise ValueError(
                f"CR-{change_id} cannot be used. Current status: {cr_status}"
            )

        if "review" in stage_name.lower():
            raise ValueError(
                f"CR-{change_id} is in Review stage and cannot be used."
            )

        if approval_status != "Approved":
            raise ValueError(
                f"CR-{change_id} is not approved. Current status: {approval_status}"
            )
                   
        udf = change.get("udf_fields") or {}

        cab_dates = []

        for field in (
           
            # "udf_date_33001",
            "udf_date_35717",
            "udf_date_35719",
            "udf_date_35720",
            "udf_date_35721",
            "udf_date_35722",
            "udf_date_35723",
            "udf_date_35724",
            "udf_date_35725",
            "udf_date_35726",
            "udf_date_35727",

            
        ):
            value = (udf.get(field) or {}).get("display_value")
            if value:
                cab_dates.append(value)
        
        cab_dates = list(dict.fromkeys(cab_dates))
        
        if not cab_dates:
            raise ValueError(
                f"CR-{change_id} does not have any deployment dates configured in ITSM."
            )
        
        return {
            "change_id": str(change.get("id")),
            "approval_status": approval_status,
            "cr_status": cr_status,
            "change_type": change_type,
            "change_requestor": change_requestor,
            "application_name": application_name,
            "business_unit": business_unit,
            "environment": environment,
            "cab_dates": cab_dates
        }


    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise ValueError(f"CR-{change_id} not found.")

        raise ValueError(f"Unable to validate CR-{change_id}.")

    except requests.RequestException:
        raise ValueError(
            "Unable to connect to ITSM service."
        )
def save_deployment(data):

    cr_number = (data.get("cr_number") or "").strip()

    cr_number = re.sub(
        r"^CR-",
        "",
        cr_number,
        flags=re.IGNORECASE
    ).strip()

    if not cr_number.isdigit():
        raise ValueError(
            "CR Number must contain digits after CR-"
        )

    cr_number = f"CR-{cr_number}"

    cr_details = validate_cr_number(cr_number)

    app.logger.info(
        "CR Validated | CR=%s | CAB Dates=%s",
        cr_number,
        cr_details.get("cab_dates")
    )    
    
    business_unit = (data.get("business_unit") or "").strip()
    environment = (data.get("environment") or "").strip()

    git_links = request.form.getlist("git_links[]")
    if not git_links:
        raise ValueError("At least one Git link is required.")

    for link in git_links:

        if not validate_git_link(link):
            raise ValueError(
                f"Invalid Git link: {link}"
            )
    application_name = (
        data.get("application_name") or ""
    ).strip()
    change_requestor = (data.get("change_requestor") or "").strip()
    if not change_requestor:
        raise ValueError("Change Requestor is required.")
    if len(change_requestor) > 150:
        raise ValueError("Change Requestor cannot exceed 150 characters.")

    released_by = None
    verified_and_deployed_by = None

    if not cr_number:
        raise ValueError("CR Number is required.")
    if not application_name:
        raise ValueError("Application Name is required.")
    try:
        deployment_date = datetime.strptime(
            data["deployment_date"],
            "%Y-%m-%d",
        ).date()
        start_time = datetime.strptime(
            data["slot_start"],
            "%H:%M",
        ).time()
    except (ValueError, KeyError) as exc:
        raise ValueError(
            "Enter a valid deployment date and time slot."
        ) from exc
    allowed_dates = [
        parse_cab_date(d)
        for d in cr_details.get("cab_dates", [])
    ]

    if deployment_date not in allowed_dates:
        raise ValueError(
            "Selected deployment date is not present in the CR."
        )

    change_type = (data.get("change_type") or "").strip()

    if change_type not in CHANGE_TYPES:
        raise ValueError("Invalid change type.")

    validate_deployment_date(
        deployment_date,
        is_manager=is_manager_user(),
        change_type=change_type,
    )
        
    if start_time.strftime("%H:%M") not in get_slot_starts(change_type):
        if change_type == "Emergency CR":
            raise ValueError("Emergency CR slots must start on a half-hour boundary between 12:00 AM and 11:30 PM.")
        raise ValueError("Choose a 30-minute slot between 6:30 PM and 11:00 PM.")
    if data.get("deployment_type") not in DEPLOYMENT_TYPES:
        raise ValueError("Invalid deployment type.")
    data["status"] = PENDING_STATUS  # "Scheduled Deployment" (formerly "Pending")

    roll_back = (data.get("roll_back") or "No").strip()
    if roll_back not in ("Yes", "No"):
        raise ValueError("Invalid rollback selection.")
    data["roll_back"] = roll_back

    slot_start = datetime.combine(
        deployment_date,
        start_time,
    )
    slot_end = slot_start + timedelta(minutes=30)
    try:
        deployment_count = int(data.get("deployment_count", "1"))
        if deployment_count < 1 or deployment_count > 5:
            raise ValueError(
                "Deployment Count must be between 1 and 5."
            )
    except ValueError:
        raise ValueError("Deployment Count must be numeric.")

    if deployment_count < 1 or deployment_count > 5:
        raise ValueError(
            "Deployment Count must be between 1 and 5."
        )
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                normalized_cr = cr_number.casefold()
                cur.execute(
                    """
                    SELECT pg_advisory_xact_lock(hashtext(%s))
                    """,
                    (normalized_cr,),
                )
                rfid = get_existing_rfid(cur, cr_number)
                if not rfid:
                    for _ in range(20):
                        candidate = generate_rfid()
                        cur.execute(
                            """
                            SELECT 1
                            FROM public.deployment_tracker
                            WHERE rfid = %s
                            LIMIT 1
                            """,
                            (candidate,),
                        )
                        if cur.fetchone() is None:
                            rfid = candidate
                            break
                    if not rfid:
                        raise RuntimeError(
                            "Could not generate an available RFID. "
                            "Please retry."
                        )
                # Serialize all bookings for this exact half-hour slot before checking capacity.
                # The legacy exclusion constraint must be dropped by the companion SQL migration
                # because it forbids all overlapping rows even when capacity is greater than one.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"release-slot:{slot_start.isoformat()}",),
                )
                blocked_message = get_blocked_date(cur, deployment_date)
                if blocked_message is not None:
                    raise ValueError(
                        f"This deployment date is blocked by the Super Manager. "
                        f"Message: {blocked_message or 'No bookings are allowed on this date.'}"
                    )

                max_cr_per_slot = get_release_settings(cur)
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.deployment_tracker
                    WHERE slot_start < %s
                      AND slot_end > %s
                      AND LOWER(TRIM(COALESCE(status, '')))
                          NOT IN ('cancelled', 'rejected')
                    """,
                    (slot_end, slot_start),
                )
                occupied_count = int(cur.fetchone()[0] or 0)
                if occupied_count >= max_cr_per_slot:
                    raise ValueError(
                        f"This slot has reached its limit of {max_cr_per_slot} CR request(s). "
                        "Please choose another slot."
                    )
                cur.execute(
                    """
                    INSERT INTO public.deployment_tracker (
                        cr_number,
                        rfid,
                        deployment_date,
                        slots,
                        slot_start,
                        slot_end,
                        application_name,
                        deployment_count,
                        deployment_type,
                        change_type,
                        released_by,
                        verified_and_deployed_by,
                        change_requestor,
                        business_unit,
                        environment,
                        git_links,
                        incident_number,
                        roll_back,
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,%s,
                        %s, %s, %s, %s,%s,
                        %s, %s, %s, %s,%s
                    )
                    RETURNING id
                    """,
                    (
                        cr_number,
                        rfid,
                        deployment_date,
                        (
                            f"{slot_start:%H:%M} - "
                            f"{slot_end:%H:%M}"
                        ),
                        slot_start,
                        slot_end,
                        application_name,
                        deployment_count, 
                        data["deployment_type"],
                        data["change_type"],
                        released_by,
                        verified_and_deployed_by,
                        change_requestor,
                        business_unit,
                        environment,
                        json.dumps(git_links),
                        data.get("incident_number"),
                        data["roll_back"],
                        data["status"],
                    ),
                )
                record_id = cur.fetchone()[0]
        write_audit_log(
            "INFO",
            (
                f"Request submitted successfully. "
                f"Record ID={record_id}; slot={slot_start}"
            ),
            cr_number,
            rfid,
        )
        return rfid
    except errors.ExclusionViolation as exc:
        app.logger.warning(
            "Deployment slot conflict for CR %s",
            cr_number,
        )
        raise ValueError(
            "This slot was just booked by another request. "
            "Please refresh and choose another green slot."
        ) from exc
    except errors.UniqueViolation as exc:
        constraint = (
            getattr(exc.diag, "constraint_name", None) or ""
        )
        app.logger.exception(
            "Unique constraint violation: %s",
            constraint,
        )
        raise RuntimeError(
            "A database uniqueness constraint rejected this request. "
            "Check your database constraints and server logs."
        ) from exc
    except psycopg2.Error as exc:
        app.logger.exception(
            "Database save failed; SQLSTATE=%s",
            exc.pgcode,
        )
        raise RuntimeError(
            "Could not save the request to the database. "
            "Check the server logs."
        ) from exc
    finally:
        if conn is not None:
            conn.close()

def get_released_by_users():
    """Fetch existing Released By names from PostgreSQL."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT TRIM(released_by)
                FROM public.deployment_tracker
                WHERE NULLIF(TRIM(released_by), '') IS NOT NULL
                ORDER BY 1
            """)
            return [row[0] for row in cur.fetchall()]
    except Exception:
        app.logger.exception(
            "Could not load Released By users"
        )
        return []
    finally:
        if conn is not None:
            conn.close()

def resolve_verified_deployed_by(data):
    """Validate the team selection or required custom name."""
    choice = (data.get("verified_and_deployed_by") or "").strip()

    if choice == "__others__":
        custom_name = (
            data.get("verified_and_deployed_by_other") or ""
        ).strip()
        if not custom_name:
            raise ValueError(
                "Please enter a name for Verified and Deployed By."
            )
        if len(custom_name) > 100:
            raise ValueError(
                "Verified and Deployed By name cannot exceed 100 characters."
            )
        return custom_name

    if choice in VERIFIED_BY_OPTIONS:
        return choice

    raise ValueError(
        "Please select DBA TEAM, MW TEAM, DevOps Team, or Others."
    )


def form_context(data=None):
    return {
        "data": data or {},
        "change_types": CHANGE_TYPES,
        "deployment_types": DEPLOYMENT_TYPES,
        "statuses": STATUSES,
        "slot_starts": SLOT_STARTS,
        "emergency_slot_starts": get_slot_starts("Emergency CR"),
    }

@app.route("/governance_tracker", methods=["GET"])

def devops_page():
    return render_template(
        "governance_tracker.html",
        deployment_types=DEPLOYMENT_TYPES,
        change_types=CHANGE_TYPES,
        statuses=STATUSES,
        released_by_users=get_released_by_users(),
        verified_by_options=VERIFIED_BY_OPTIONS,
        audit_user_emails=get_audit_user_emails(),
    )

@app.route("/api/deployment-by-cr", methods=["GET"])
def get_deployment_by_cr():
    """Return every deployment record associated with the supplied CR number."""
    cr_number = (request.args.get("cr_number") or "").strip()
    cr_number = re.sub(r"^CR-", "", cr_number, flags=re.IGNORECASE).strip()
    if not cr_number.isdigit():
        return jsonify({
            "success": False,
            "message": "Enter a valid CR Number, for example CR-12345."
        }), 400

    normalized_cr = f"CR-{cr_number}"
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, cr_number, rfid, deployment_date, slot_start, slot_end,
                       slots, application_name, deployment_count, deployment_type, change_type,
                       released_by, verified_and_deployed_by, change_requestor,
                       git_links, comments, incident_number, roll_back, status, team_allocations
                FROM public.deployment_tracker
                WHERE UPPER(REGEXP_REPLACE(TRIM(cr_number), '^CR-', '', 'i')) = %s
                ORDER BY id DESC
            """, (cr_number.upper(),))
            rows = cur.fetchall()
            columns = [description[0] for description in cur.description]

        if not rows:
            return jsonify({
                "success": False,
                "message": f"No deployment found for {normalized_cr}."
            }), 404

        deployments = []
        for row in rows:
            deployment = dict(zip(columns, row))
            for key, value in deployment.items():
                if isinstance(value, (datetime, date, time)):
                    deployment[key] = value.isoformat()
                elif value is not None and not isinstance(value, (str, int, float, bool)):
                    deployment[key] = str(value)
            deployments.append(deployment)

        return jsonify({
            "success": True,
            "message": f"{len(deployments)} deployment request(s) found for {normalized_cr}.",
            "deployments": deployments
        })
    except Exception:
        app.logger.exception("DevOps CR lookup failed")
        return jsonify({
            "success": False,
            "message": "Could not retrieve deployment details. Check the server logs."
        }), 500
    finally:
        if conn is not None:
            conn.close()


def ensure_running_timer_table(cur):
    """Create the timer table on first use; one accumulated timer per deployment."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.deployment_running_timers (
            deployment_id BIGINT PRIMARY KEY
                REFERENCES public.deployment_tracker(id) ON DELETE CASCADE,
            started_at TIMESTAMPTZ,
            stopped_at TIMESTAMPTZ,
            accumulated_seconds BIGINT NOT NULL DEFAULT 0,
            is_running BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def serialize_running_timer(row):
    if not row:
        return {
            "started_at": None,
            "stopped_at": None,
            "accumulated_seconds": 0,
            "is_running": False
        }
    started_at, stopped_at, accumulated_seconds, is_running = row
    return {
        "started_at": started_at.isoformat() if started_at else None,
        "stopped_at": stopped_at.isoformat() if stopped_at else None,
        "accumulated_seconds": int(accumulated_seconds or 0),
        "is_running": bool(is_running)
    }


@app.route("/api/deployment-running-timer/<int:deployment_id>", methods=["GET"])
def get_deployment_running_timer(deployment_id):
    conn = None
    try:
        conn = get_connection()

        with conn:
            with conn.cursor() as cur:
                ensure_running_timer_table(cur)

                cur.execute("""
                    SELECT started_at,
                           stopped_at,
                           accumulated_seconds,
                           is_running
                    FROM public.deployment_running_timers
                    WHERE deployment_id = %s
                """, (deployment_id,))
                row = cur.fetchone()

                cur.execute("""
                    SELECT 1
                    FROM public.deployment_tracker
                    WHERE id = %s
                """, (deployment_id,))

                if not cur.fetchone():
                    return jsonify({
                        "success": False,
                        "message": "Deployment record not found."
                    }), 404

        return jsonify({
            "success": True,
            "timer": serialize_running_timer(row)
        })

    except Exception as e:
        app.logger.exception(
            "Could not load deployment running timer: %s",
            deployment_id
        )

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        if conn is not None:
            conn.close()

@app.route("/api/deployment-running-timer/<int:deployment_id>", methods=["POST"])
def update_deployment_running_timer(deployment_id):
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in ("start", "stop"):
        return jsonify({"success": False, "message": "Timer action must be start or stop."}), 400

    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_running_timer_table(cur)
                cur.execute("SELECT 1 FROM public.deployment_tracker WHERE id = %s", (deployment_id,))
                if not cur.fetchone():
                    return jsonify({"success": False, "message": "Deployment record not found."}), 404

                cur.execute(
                    """
                    INSERT INTO public.deployment_running_timers (deployment_id)
                    VALUES (%s)
                    ON CONFLICT (deployment_id) DO NOTHING
                    """,
                    (deployment_id,)
                )
                cur.execute(
                    """
                    SELECT started_at, stopped_at, accumulated_seconds, is_running
                    FROM public.deployment_running_timers
                    WHERE deployment_id = %s
                    FOR UPDATE
                    """,
                    (deployment_id,)
                )
                row = cur.fetchone()
                started_at, stopped_at, accumulated_seconds, is_running = row
                now = datetime.now().astimezone()

                if action == "start":
                    if is_running:
                        return jsonify({
                            "success": True,
                            "message": "Timer is already running.",
                            "timer": serialize_running_timer(row)
                        })
                    cur.execute(
                        """
                        UPDATE public.deployment_running_timers
                        SET started_at = %s, stopped_at = NULL, is_running = TRUE,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE deployment_id = %s
                        RETURNING started_at, stopped_at, accumulated_seconds, is_running
                        """,
                        (now, deployment_id)
                    )
                    message = "Timer started. Start date and time recorded."
                else:
                    if not is_running or not started_at:
                        return jsonify({"success": False, "message": "The timer is not currently running."}), 409
                    elapsed = max(0, int((now - started_at).total_seconds()))
                    cur.execute(
                        """
                        UPDATE public.deployment_running_timers
                        SET stopped_at = %s,
                            accumulated_seconds = accumulated_seconds + %s,
                            is_running = FALSE,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE deployment_id = %s
                        RETURNING started_at, stopped_at, accumulated_seconds, is_running
                        """,
                        (now, elapsed, deployment_id)
                    )
                    message = "Timer stopped. Stop date and time recorded."

                updated = cur.fetchone()
        return jsonify({
            "success": True,
            "message": message,
            "timer": serialize_running_timer(updated)
        })
    except Exception as e:
        app.logger.exception(
            "Could not update deployment running timer: %s",
            deployment_id
        )
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/deployment-by-id/<int:deployment_id>", methods=["PUT"])

def update_devops_deployment(deployment_id):
    """Update an existing deployment row from the DevOps page."""
    data = request.get_json(silent=True) or {}
    git_links = data.get("git_links", [])

    if isinstance(git_links, str):
        git_links = [git_links]

    if not isinstance(git_links, list) or not git_links:
        return jsonify({
            "success": False,
            "message": "At least one Git link is required."
        }), 400

    git_links = [str(link).strip() for link in git_links]
    for link in git_links:
        if not validate_git_link(link):
            return jsonify({
                "success": False,
                "message": f"Invalid Git repository URL: {link}"
            }), 400
    
    try:
        modified_by = normalize_audit_email(data.get("modified_by"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    modified_by_passcode = str(data.get("modified_by_passcode") or "").strip()
    if not re.fullmatch(r"\d{4}", modified_by_passcode):
        return jsonify({"success": False, "message": "Enter the 4-digit passcode for the selected email."}), 400

    required_fields = [
        "deployment_date", "slot_start", "application_name", "deployment_count",
        "deployment_type", "change_type", "roll_back", "status",
        "released_by", "verified_and_deployed_by", "change_requestor",
    ]
    for field in required_fields:
        if not str(data.get(field) or "").strip():
            return jsonify({
                "success": False,
                "message": f"{field.replace('_', ' ').title()} is required.",
            }), 400
    try:
        deployment_count = int(data.get("deployment_count"))
        if deployment_count < 1 or deployment_count > 5:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "Deployment Count must be a whole number between 1 and 5."
        }), 400

    team_allocations = data.get("team_allocations")
    if not isinstance(team_allocations, list) or not team_allocations:
        return jsonify({"success": False, "message": "Add at least one team allocation."}), 400
    normalized_allocations = []
    seen_teams = set()
    allocation_sum = 0
    for item in team_allocations:
        if not isinstance(item, dict):
            return jsonify({"success": False, "message": "Invalid team allocation row."}), 400
        team_name = str(item.get("team") or "").strip()
        try:
            team_count = int(item.get("count"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Each team must have a whole-number deployment count."}), 400
        team_key = team_name.casefold()
        if not team_name or len(team_name) > 100 or team_count < 1:
            return jsonify({"success": False, "message": "Enter a valid team name and a deployment count greater than zero."}), 400
        if team_key in seen_teams:
            return jsonify({"success": False, "message": f"Team '{team_name}' is listed more than once. Combine its count into one row."}), 400
        seen_teams.add(team_key)
        allocation_sum += team_count
        normalized_allocations.append({"team": team_name, "count": team_count})
    if allocation_sum != deployment_count:
        return jsonify({
            "success": False,
            "message": f"Team allocation total ({allocation_sum}) must equal Deployment Count ({deployment_count})."
        }), 400

    if data["deployment_type"] not in DEPLOYMENT_TYPES:
        return jsonify({"success": False, "message": "Invalid deployment type."}), 400
    if data["change_type"] not in CHANGE_TYPES:
        return jsonify({"success": False, "message": "Invalid change type."}), 400
    if data["status"] not in STATUSES and data["status"] not in APPROVAL_STATUSES:
        return jsonify({"success": False, "message": "Invalid status."}), 400
    if data["roll_back"] not in ("Yes", "No"):
        return jsonify({"success": False, "message": "Invalid rollback selection."}), 400

    released_by = (data.get("released_by") or "").strip()
    if not released_by or released_by == "__add__":
        return jsonify({
            "success": False,
            "message": "Select a Released By user or enter a new name.",
        }), 400
    if len(released_by) > 100:
        return jsonify({
            "success": False,
            "message": "Released By name cannot exceed 100 characters.",
        }), 400

    try:
        verified_and_deployed_by = resolve_verified_deployed_by(data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    change_requestor = (data.get("change_requestor") or "").strip()
    if not change_requestor:
        return jsonify({
            "success": False,
            "message": "Change Requestor is required.",
        }), 400
    if len(change_requestor) > 150:
        return jsonify({
            "success": False,
            "message": "Change Requestor cannot exceed 150 characters.",
        }), 400

    try:
        deployment_date = datetime.strptime(data["deployment_date"], "%Y-%m-%d").date()
        start_time = datetime.strptime(data["slot_start"], "%H:%M").time()
    except ValueError:
        return jsonify({"success": False, "message": "Invalid deployment date or slot."}), 400
    
    # Governance can move a deployment to any valid time of day.
    # Keep the deployment duration at 30 minutes; do not apply the
    # standard booking window (6:30 PM-11:00 PM) on this update route.
    if not data["application_name"].strip():
        return jsonify({"success": False, "message": "Application Name is required."}), 400

    credentials_ok, credential_message, credential_status = verify_audit_user_credentials(
        modified_by, modified_by_passcode
    )
    if not credentials_ok:
        return jsonify({"success": False, "message": credential_message}), credential_status

    slot_start = datetime.combine(deployment_date, start_time)
    slot_end = slot_start + timedelta(minutes=30)
    conn = None
    cursor = None
    try:
        conn = get_connection()
        conn.autocommit = False
        cursor = conn.cursor()
        # Serialize updates to the target slot, then enforce the current Super Manager capacity.
        target_slot_key = f"release-slot:{slot_start.isoformat()}"
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (target_slot_key,))
        cursor.execute(
            """
            SELECT id, cr_number, rfid, deployment_date, slot_start, slot_end,
                   application_name, deployment_count, deployment_type, change_type, released_by,
                   verified_and_deployed_by, change_requestor, git_links,
                   comments, incident_number, roll_back, status, team_allocations
            FROM public.deployment_tracker
            WHERE id = %s
            FOR UPDATE
            """,
            (deployment_id,),
        )
        old_row = cursor.fetchone()
        if old_row is None:
            conn.rollback()
            return jsonify({"success": False, "message": "Deployment record not found."}), 404
        old_columns = [description[0] for description in cursor.description]
        old_record = dict(zip(old_columns, old_row))
        blocked_message = get_blocked_date(cursor, deployment_date)
        if blocked_message is not None:
            conn.rollback()
            return jsonify({
                "success": False,
                "message": f"This deployment date is blocked by the Super Manager. Message: {blocked_message}",
            }), 409

        current_capacity = get_release_settings(cursor)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM public.deployment_tracker
            WHERE id <> %s
              AND slot_start < %s
              AND slot_end > %s
              AND LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cancelled', 'rejected')
            """,
            (deployment_id, slot_end, slot_start),
        )
        target_occupied = int(cursor.fetchone()[0] or 0)
        if target_occupied >= current_capacity:
            conn.rollback()
            return jsonify({
                "success": False,
                "message": f"This slot has reached the Super Manager limit of {current_capacity} CR request(s).",
            }), 409

        cursor.execute(
            """
            UPDATE public.deployment_tracker
            SET deployment_date = %s,
                slots = %s,
                slot_start = %s,
                slot_end = %s,
                application_name = %s,
                deployment_count = %s,
                deployment_type = %s,
                change_type = %s,
                released_by = %s,
                verified_and_deployed_by = %s,
                change_requestor = %s,
                git_links = %s,
                comments = %s,
                incident_number = %s,
                roll_back = %s,
                status = %s,
                team_allocations = %s::jsonb
            WHERE id = %s
            """,
            (
                deployment_date,
                f"{slot_start:%H:%M} - {slot_end:%H:%M}",
                slot_start,
                slot_end,
                data["application_name"].strip(),
                deployment_count,
                data["deployment_type"],
                data["change_type"],
                released_by,
                verified_and_deployed_by,
                change_requestor,
                json.dumps(git_links),
                (data.get("comments") or "").strip() or None,
                (data.get("incident_number") or "").strip() or None,
                data["roll_back"],
                data["status"],
                json.dumps(normalized_allocations),
                deployment_id,
            ),
        )
        new_record = {
            "deployment_date": deployment_date,
            "slot_start": slot_start,
            "slot_end": slot_end,
            "application_name": data["application_name"].strip(),
            "deployment_count": deployment_count,
            "deployment_type": data["deployment_type"],
            "change_type": data["change_type"],
            "released_by": released_by,
            "verified_and_deployed_by": verified_and_deployed_by,
            "change_requestor": change_requestor,
            "git_links": json.dumps(git_links),
            "team_allocations": json.dumps(normalized_allocations),
            "comments": (data.get("comments") or "").strip() or None,
            "incident_number": (data.get("incident_number") or "").strip() or None,
            "roll_back": data["roll_back"],
            "status": data["status"],
        }
        old_status = str(old_record.get("status") or "").strip()
        new_status = str(new_record.get("status") or "").strip()
        status_email_snapshot = None
        status_email_transition = None
        if old_status != new_status and new_status in STATUS_EMAIL_STATUSES:
            status_email_snapshot = dict(old_record)
            status_email_snapshot.update(new_record)
            status_email_snapshot["id"] = deployment_id
            status_email_transition = (old_status or "Not set", new_status)

        changes = {}
        for field, new_value in new_record.items():
            old_value = old_record.get(field)
            if audit_value(old_value) != audit_value(new_value):
                changes[field] = {
                    "old": audit_value(old_value),
                    "new": audit_value(new_value),
                }
        if changes:
            insert_change_history(
                cursor,
                deployment_id,
                old_record.get("cr_number"),
                old_record.get("rfid"),
                modified_by,
                "Updated",
                changes,
            )
        conn.commit()
        write_audit_log(
            "INFO",
            f"DevOps updated deployment record ID={deployment_id}; changed_by={modified_by}; fields={','.join(changes.keys()) or 'no changes'}",
            old_record.get("cr_number"),
            old_record.get("rfid"),
        )
        response_message = "Deployment details updated successfully."
        if status_email_snapshot is not None and status_email_transition is not None:
            email_sent, _email_reason = send_status_change_notification(
                status_email_snapshot,
                status_email_transition[0],
                status_email_transition[1],
            )
            if email_sent:
                response_message += " Status notification email sent."
            else:
                response_message += " Status was saved, but the notification email failed; check the application logs and SMTP settings."
        return jsonify({"success": True, "message": response_message})
    except errors.ExclusionViolation:
        if conn:
            conn.rollback()
        return jsonify({
            "success": False,
            "message": "This slot is already occupied. Choose another available slot.",
        }), 409
    except psycopg2.Error:
        if conn:
            conn.rollback()
        app.logger.exception("DevOps deployment update failed")
        return jsonify({"success": False, "message": "Database update failed. Check server logs."}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/pending-deployments", methods=["GET"])

def pending_deployments():
    """List requests still in the default 'Scheduled Deployment' status."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                id,
                cr_number,
                rfid,
                deployment_date,
                slot_start,
                slot_end,
                slots,
                application_name,
                deployment_type,
                change_type,
                released_by,
                verified_and_deployed_by,
                change_requestor,
                git_links,
                comments,
                incident_number,
                roll_back,
                status, team_allocations
            FROM public.deployment_tracker
            WHERE LOWER(TRIM(COALESCE(status, %s))) = LOWER(%s)
            ORDER BY id DESC
        """, (PENDING_STATUS, PENDING_STATUS))
        columns = [column[0] for column in cursor.description]
        requests_list = []
        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            for key, value in record.items():
                if isinstance(value, (datetime, date, time)):
                    record[key] = value.isoformat()
                elif value is not None and not isinstance(
                    value, (str, int, float, bool)
                ):
                    record[key] = str(value)
            requests_list.append(record)
        return jsonify({
            "success": True,
            "requests": requests_list,
            "count": len(requests_list)
        })
    except Exception:
        app.logger.exception("Unable to load pending requests")
        return jsonify({
            "success": False,
            "message": "Unable to load pending requests."
        }), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route("/api/deployments-by-date", methods=["GET"])
def deployments_by_date():
    """Return deployment records for the selected local calendar date."""
    date_text = (request.args.get("date") or "").strip()
    try:
        selected_date = date.fromisoformat(date_text)
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Choose a valid date in YYYY-MM-DD format.",
        }), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                id,
                cr_number,
                rfid,
                deployment_date,
                slot_start,
                slot_end,
                slots,
                application_name,
                deployment_type,
                change_type,
                released_by,
                verified_and_deployed_by,
                change_requestor,
                git_links,
                comments,
                incident_number,
                roll_back,
                status
            FROM public.deployment_tracker
            WHERE deployment_date = %s
            ORDER BY slot_start NULLS LAST, id ASC
        """, (selected_date,))

        columns = [column[0] for column in cursor.description]
        deployments = []
        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            for key, value in record.items():
                if isinstance(value, (datetime, date, time)):
                    record[key] = value.isoformat()
                elif value is not None and not isinstance(value, (str, int, float, bool)):
                    record[key] = str(value)
            deployments.append(record)

        return jsonify({
            "success": True,
            "date": selected_date.isoformat(),
            "deployments": deployments,
            "count": len(deployments),
        })
    except Exception:
        app.logger.exception("Unable to load deployments for selected date %s", date_text)
        return jsonify({
            "success": False,
            "message": "Unable to load the deployment schedule. Check server logs.",
        }), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/deployment-approval/<int:deployment_id>", methods=["POST"])

def decide_deployment_request(deployment_id):
    payload = request.get_json(silent=True) or {}
    try:
        modified_by = normalize_audit_email(payload.get("modified_by"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    modified_by_passcode = str(payload.get("modified_by_passcode") or "").strip()
    if not re.fullmatch(r"\d{4}", modified_by_passcode):
        return jsonify({"success": False, "message": "Enter the 4-digit passcode for the selected email."}), 400
    decision = (payload.get("decision") or "").strip().lower()
    status_map = {
        "approve": "Approved",
        "reject": "Rejected"
    }
    if decision not in status_map:
        return jsonify({
            "success": False,
            "message": "Decision must be approve or reject."
        }), 400
    credentials_ok, credential_message, credential_status = verify_audit_user_credentials(
        modified_by, modified_by_passcode
    )
    if not credentials_ok:
        return jsonify({"success": False, "message": credential_message}), credential_status
    conn = None
    cursor = None
    new_status = status_map[decision]
    try:
        conn = get_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                id, cr_number, rfid, deployment_date, slot_start, slot_end,
                application_name, deployment_type, change_type, released_by,
                verified_and_deployed_by, change_requestor, git_links,
                comments, incident_number, roll_back, status
            FROM public.deployment_tracker
            WHERE id = %s
            FOR UPDATE
        """, (deployment_id,))
        old_row = cursor.fetchone()
        if old_row is None:
            conn.rollback()
            return jsonify({
                "success": False,
                "message": "Deployment record not found. Refresh the pending list."
            }), 409

        columns = [desc[0] for desc in cursor.description]
        deployment_snapshot = dict(zip(columns, old_row))
        old_status = str(deployment_snapshot.get("status") or PENDING_STATUS).strip()
        if old_status.casefold() != PENDING_STATUS.casefold():
            conn.rollback()
            return jsonify({
                "success": False,
                "message": "Request is no longer pending. Refresh the pending list."
            }), 409

        cursor.execute("""
            UPDATE public.deployment_tracker
            SET status = %s
            WHERE id = %s
              AND LOWER(TRIM(COALESCE(status, %s))) = LOWER(%s)
            RETURNING cr_number, rfid
        """, (new_status, deployment_id, PENDING_STATUS, PENDING_STATUS))
        updated_row = cursor.fetchone()
        if updated_row is None:
            conn.rollback()
            return jsonify({
                "success": False,
                "message": (
                    "Request not found or it is no longer pending. "
                    "Refresh the pending list."
                )
            }), 409

        deployment_snapshot["status"] = new_status
        insert_change_history(
            cursor,
            deployment_id,
            updated_row[0],
            updated_row[1],
            modified_by,
            decision.title(),
            {"status": {"old": old_status, "new": new_status}},
        )
        conn.commit()
        write_audit_log(
            "INFO",
            f"Deployment request {decision}d: "
            f"ID={deployment_id}, CR={updated_row[0]}, status={new_status}, changed_by={modified_by}",
            updated_row[0],
            updated_row[1]
        )

        response_message = f"Request {new_status.lower()} successfully."
        if old_status != new_status and new_status in STATUS_EMAIL_STATUSES:
            email_sent, _email_reason = send_status_change_notification(
                deployment_snapshot, old_status, new_status
            )
            if email_sent:
                response_message += " Status notification email sent."
            else:
                response_message += " Status was saved, but the notification email failed; check the application logs and SMTP settings."

        return jsonify({
            "success": True,
            "message": response_message,
            "status": new_status
        })
    except Exception:
        if conn:
            conn.rollback()
        app.logger.exception("Approval/rejection failed")
        return jsonify({
            "success": False,
            "message": "Could not save the decision."
        }), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/slot-status", methods=["GET"])

def slot_status():
    date_text = (request.args.get("date") or "").strip()
    try:
        selected_date = datetime.strptime(
            date_text,
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return jsonify(
            error="Choose a valid deployment date."
        ), 400
    
    change_type = (request.args.get("change_type") or "").strip()
    cr_number = (request.args.get("cr_number") or "").strip()
    if not cr_number:
        return jsonify(error="Enter and validate the CR number to load its CAB date and slots."), 400
    
    try:
        cr_details = validate_cr_number(cr_number)
    
        allowed_dates = {
            parse_cab_date(d)
            for d in cr_details.get("cab_dates", [])
            if d
        }
    
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    
    if selected_date not in allowed_dates:
        return jsonify({
            "error": (
                "Selected date is not present in the CR. "
                f"Allowed dates: {', '.join(sorted(d.isoformat() for d in allowed_dates))}"
            ),
            "allowed_dates": [
                d.isoformat() for d in sorted(allowed_dates)
            ],
            "slots": []
        }), 400

    if change_type and change_type not in CHANGE_TYPES:
        return jsonify({
            "error": "Invalid change type."
    }), 400

    is_manager = is_manager_user()
    is_emergency = change_type.casefold() == "emergency cr"

    if not is_manager:
        if selected_date < date.today():
            return jsonify({
                "error": (
                    "Past deployment dates are not available for booking."
                )
        }), 400

        # if selected_date.weekday() in (5, 6) and not is_emergency:
        #     return jsonify({
        #         "week_off": True,
        #         "message": (
        #             "Week Off — Only Emergency CR deployments are "
        #             "allowed on Saturday and Sunday."
        #     ),
        #     "slots": []
        # }), 200

    slot_starts = get_slot_starts(change_type)


    slots = []
    blocked_message = None
    max_cr_per_slot = 1
    for start_text in slot_starts:
        start_time = datetime.strptime(
            start_text,
            "%H:%M",
        ).time()
        start_dt = datetime.combine(
            selected_date,
            start_time,
        )
        end_dt = start_dt + timedelta(minutes=30)
        slots.append({
            "start": start_text,
            "end": end_dt.strftime("%H:%M"),
            "state": "available",
            "status": "Available",
        })
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            blocked_message = get_blocked_date(cur, selected_date)
            max_cr_per_slot = get_release_settings(cur)
            cur.execute(
                 """
                    SELECT slot_start, slot_end, status
                 FROM public.deployment_tracker
                  WHERE deployment_date = %s
                   AND slot_start IS NOT NULL
                   AND slot_end IS NOT NULL
                  AND LOWER(TRIM(COALESCE(status, '')))
                  NOT IN ('cancelled', 'rejected')
             """,
                (selected_date,),
            )
            rows = cur.fetchall()
        for slot in slots:
            slot_start = datetime.combine(
                selected_date, datetime.strptime(slot["start"], "%H:%M").time()
            )
            slot_end = slot_start + timedelta(minutes=30)
            overlapping = [
                (start_dt, end_dt, status) for start_dt, end_dt, status in rows
                if start_dt < slot_end and end_dt > slot_start
            ]
            active = [
                item for item in overlapping
                if (item[2] or "").strip().casefold() not in ("cancelled", "rejected")
            ]
            slot["booked_count"] = len(active)
            slot["capacity"] = max_cr_per_slot
            if blocked_message is not None:
                slot["state"] = "blocked"
                slot["status"] = "Blocked for the day"
            elif len(active) >= max_cr_per_slot:
                slot["state"] = "booked"
                slot["status"] = f"Full ({len(active)}/{max_cr_per_slot})"
            elif active:
                slot["state"] = "available"
                slot["status"] = f"{len(active)}/{max_cr_per_slot} booked; {max_cr_per_slot - len(active)} available"
            else:
                slot["state"] = "available"
                slot["status"] = f"Available (0/{max_cr_per_slot})"
        return jsonify({
            "date": date_text,
            "change_type": change_type or None,
            "blocked": blocked_message is not None,
            "block_message": blocked_message,
            "max_cr_per_slot": max_cr_per_slot,
            "slots": slots,
        })
    except psycopg2.Error:
        app.logger.exception(
            "Could not load slot availability"
        )
        return jsonify(
            error="Unable to load slot availability. Please retry."
        ), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/lookup-cr", methods=["GET"])

def lookup_cr():
    cr_number = (request.args.get("cr_number") or "").strip()
    if not cr_number:
        return jsonify(
            exists=False,
            rfid=None,
        ), 200
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rfid
                FROM public.deployment_tracker
                WHERE LOWER(TRIM(cr_number)) = LOWER(TRIM(%s))
                  AND rfid IS NOT NULL
                ORDER BY id
                LIMIT 1
                """,
                (cr_number,),
            )
            row = cur.fetchone()
        if row:
            return jsonify(
                exists=True,
                rfid=str(row[0]),
            ), 200
        return jsonify(
            exists=False,
            rfid=None,
        ), 200
    except psycopg2.Error:
        app.logger.exception("CR lookup failed")
        return jsonify(
            error="Database lookup failed. Please retry."
        ), 500
    finally:
        if conn is not None:
            conn.close()

def send_deployment_notification(data, rfid):
    """Email all configured recipients after a deployment is saved successfully.

    Returns (sent, reason). Mail failure never rolls back a saved deployment.
    Configure recipients and SMTP credentials in .env, never in source code.
    """
    git_links = data.get("git_links", [])


    requestor_email = (data.get("change_requestor") or "").strip()
    # Read application_name from the submitted deployment data before building
    # the email subject. This keeps it defined throughout this function.
    application_name = (data.get("application_name") or "").strip()
    if not application_name:
        application_name = "Not provided"

    recipients_raw = os.getenv("DEPLOYMENT_EMAIL_RECIPIENTS", "")

    recipients = [
        address.strip()
        for address in re.split(r"[,;\n]+", recipients_raw)
        if address.strip()
    ]

    if requestor_email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", requestor_email):
        recipients.append(requestor_email)

    recipients = list(set(recipients))
    
    if not recipients:
        app.logger.warning(
            "Deployment saved, but email was not sent: "
            "DEPLOYMENT_EMAIL_RECIPIENTS is not configured."
        )
        return False, "Email recipients are not configured in .env."

    host = (os.getenv("MAIL_SERVER") or "").strip()
    username = (os.getenv("MAIL_USERNAME") or "").strip()
    password = os.getenv("MAIL_PASSWORD", "")
    from_email = (os.getenv("MAIL_FROM_EMAIL") or username).strip()
    from_name = (os.getenv("MAIL_FROM_NAME") or "Deployment Tracker").strip()
    if not host or not from_email:
        app.logger.warning(
            "Deployment saved, but email was not sent: SMTP configuration is incomplete."
        )
        return False, "SMTP settings are incomplete in .env."

    try:
        port = int(os.getenv("MAIL_PORT", "587"))
        use_ssl = os.getenv("MAIL_USE_SSL", "false").strip().lower() in ("1", "true", "yes", "on")
        use_tls = os.getenv("MAIL_USE_TLS", "true").strip().lower() in ("1", "true", "yes", "on")
        cr_number = (data.get("cr_number") or "").strip()
        cr_suffix = re.sub(r"^CR-", "", cr_number, flags=re.IGNORECASE).strip()
        cr_number = f"CR-{cr_suffix}" if cr_suffix else cr_number
        requestor = (data.get("change_requestor") or "").strip() or "Unknown user"
        deployment_date = datetime.strptime(data["deployment_date"], "%Y-%m-%d").date()
        start_time = datetime.strptime(data["slot_start"], "%H:%M").time()
        slot_start = datetime.combine(deployment_date, start_time)
        slot_end = slot_start + timedelta(minutes=30)

        def display(value):
            value = (value or "").strip()
            return value if value else "Not provided"

        subject = f"{application_name}-{requestor} scheduled the deployment - {cr_number}"
        body = "\n".join([
            f"{requestor} scheduled the deployment.",
            "",
            "DEPLOYMENT DETAILS",
            "------------------",
            f"Scheduled By: {requestor}",
            f"CR Number: {cr_number}",
            f"RFID: {rfid}",
            f"Deployment Date: {deployment_date.strftime('%d-%m-%Y')}",
            f"Slot Timing: {slot_start.strftime('%I:%M %p')} - {slot_end.strftime('%I:%M %p')}",
            f"Application Name: {display(data.get('application_name'))}",
            f"Deployment Type: {display(data.get('deployment_type'))}",
            f"Change Type: {display(data.get('change_type'))}",
            f"Change Requestor: {requestor}",
            f"Incident Number: {display(data.get('incident_number'))}",
            f"Git Links: {', '.join(git_links)}",
            f"Status: {PENDING_STATUS}",
            "",
            "This is an automated notification from the Deployment Tracker.",
        ])

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((from_name, from_email))
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        timeout = int(os.getenv("MAIL_TIMEOUT_SECONDS", "20"))
        if use_ssl:
            with smtplib.SMTP_SSL(
                host, port, timeout=timeout, context=ssl.create_default_context()
            ) as smtp:
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)

        app.logger.info(
            "Deployment notification sent to %d recipient(s) for CR %s, RFID %s",
            len(recipients), cr_number, rfid,
        )
        return True, None
    except Exception:
        app.logger.exception(
            "Deployment saved, but notification email failed for CR %s",
            data.get("cr_number", ""),
        )
        return False, "The deployment was saved, but the notification email could not be sent. Check the application logs and SMTP settings."


def send_status_change_notification(record, old_status, new_status):
    """Notify configured recipients and registered audit users of a status change.

    The message intentionally excludes Modified By email/passcode fields. SMTP
    errors are logged and never undo a deployment status already committed.
    """
    configured_recipients = [
        address.strip().lower()
        for address in re.split(r"[,;\n]+", os.getenv("DEPLOYMENT_EMAIL_RECIPIENTS", ""))
        if address.strip()
    ]
    try:
        registered_recipients = [
            address.strip().lower()
            for address in get_audit_user_emails()
            if address and address.strip()
        ]
    except Exception:
        app.logger.exception("Could not load registered email recipients for status notification")
        registered_recipients = []
    recipients = sorted(set(configured_recipients + registered_recipients))
    if not recipients:
        app.logger.warning("Status changed to %s, but no email recipients are configured", new_status)
        return False, "No email recipients are configured."

    host = (os.getenv("MAIL_SERVER") or "").strip()
    username = (os.getenv("MAIL_USERNAME") or "").strip()
    password = os.getenv("MAIL_PASSWORD", "")
    from_email = (os.getenv("MAIL_FROM_EMAIL") or username).strip()
    from_name = (os.getenv("MAIL_FROM_NAME") or "Deployment Tracker").strip()
    if not host or not from_email:
        app.logger.warning("Status notification email skipped because SMTP settings are incomplete")
        return False, "SMTP settings are incomplete."

    def display(value):
        if value is None:
            return "Not provided"
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        value = str(value).strip()
        return value if value else "Not provided"

    try:
        deployment_id = record.get("id", "Not provided")
        cr_number = display(record.get("cr_number"))
        rfid = display(record.get("rfid"))
        deployment_date = record.get("deployment_date")
        slot_start = record.get("slot_start")
        slot_end = record.get("slot_end")
        if isinstance(deployment_date, (datetime, date)):
            date_text = deployment_date.strftime("%d-%m-%Y")
        else:
            try:
                date_text = date.fromisoformat(str(deployment_date)[:10]).strftime("%d-%m-%Y")
            except (TypeError, ValueError):
                date_text = display(deployment_date)

        def format_clock(value):
            if isinstance(value, datetime):
                value = value.time()
            if isinstance(value, time):
                return value.strftime("%I:%M %p")
            if value is not None:
                raw = str(value)
                try:
                    return datetime.fromisoformat(raw).strftime("%I:%M %p")
                except ValueError:
                    try:
                        return time.fromisoformat(raw).strftime("%I:%M %p")
                    except ValueError:
                        return raw
            return "Not provided"

        changed_at = datetime.now().astimezone().strftime("%d-%m-%Y %I:%M:%S %p %Z")
        slot_text = f"{format_clock(slot_start)} - {format_clock(slot_end)}"
        application_name = display(record.get("application_name"))
        subject = f"Deployment Status Update: {application_name} - {cr_number} - {new_status}"

        # git_links is stored as JSON in PostgreSQL; normalize it for email display.
        raw_git_links = record.get("git_links")
        if isinstance(raw_git_links, str):
            try:
                parsed_git_links = json.loads(raw_git_links)
                git_links = parsed_git_links if isinstance(parsed_git_links, list) else [raw_git_links]
            except (TypeError, ValueError):
                git_links = [raw_git_links]
        elif isinstance(raw_git_links, (list, tuple)):
            git_links = list(raw_git_links)
        else:
            git_links = []
        git_links = [str(link).strip() for link in git_links if str(link).strip()]
        git_links_text = "\n".join(git_links) if git_links else "Not provided"

        details = [
            f"Status changed from {old_status} to {new_status}.",
            f"Status change timestamp: {changed_at}",
            "",
            "DEPLOYMENT DETAILS",
            "------------------",
            f"CR Number: {cr_number}",
            f"RFID: {rfid}",
            f"Deployment Date: {date_text}",
            f"Slot Timing: {slot_text}",
            f"Application Name: {display(record.get('application_name'))}",
            f"Deployment Type: {display(record.get('deployment_type'))}",
            f"Change Type: {display(record.get('change_type'))}",
            f"Released By: {display(record.get('released_by'))}",
            f"Verified and Deployed By: {display(record.get('verified_and_deployed_by'))}",
            f"Change Requestor: {display(record.get('change_requestor'))}",
            f"Incident Number: {display(record.get('incident_number'))}",
            f"Roll Back: {display(record.get('roll_back'))}",
            f"Status: {new_status}",
            "Git Repository Links:",
            *(git_links if git_links else ["Not provided"]),
            f"Comments: {display(record.get('comments'))}",
            "",
            "This is an automated notification from the Deployment Tracker.",
        ]
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((from_name, from_email))
        message["To"] = "Undisclosed recipients:;"
        message.set_content("\n".join(details))

        # Add an HTML alternative so repository URLs are explicitly clickable.
        import html
        git_links_html = (
            "<br>".join(
                f'<a href="{html.escape(link, quote=True)}">{html.escape(link)}</a>'
                for link in git_links
            )
            if git_links else "Not provided"
        )
        html_details = [
            f"<p>Status changed from <strong>{html.escape(str(old_status))}</strong> "
            f"to <strong>{html.escape(str(new_status))}</strong>.</p>",
            "<h3>DEPLOYMENT DETAILS</h3><ul>",
            f"<li><strong>CR Number:</strong> {html.escape(cr_number)}</li>",
            f"<li><strong>RFID:</strong> {html.escape(rfid)}</li>",
            f"<li><strong>Deployment Date:</strong> {html.escape(date_text)}</li>",
            f"<li><strong>Slot Timing:</strong> {html.escape(slot_text)}</li>",
            f"<li><strong>Application Name:</strong> {html.escape(display(record.get('application_name')))}</li>",
            f"<li><strong>Deployment Type:</strong> {html.escape(display(record.get('deployment_type')))}</li>",
            f"<li><strong>Change Type:</strong> {html.escape(display(record.get('change_type')))}</li>",
            f"<li><strong>Released By:</strong> {html.escape(display(record.get('released_by')))}</li>",
            f"<li><strong>Verified and Deployed By:</strong> {html.escape(display(record.get('verified_and_deployed_by')))}</li>",
            f"<li><strong>Change Requestor:</strong> {html.escape(display(record.get('change_requestor')))}</li>",
            f"<li><strong>Incident Number:</strong> {html.escape(display(record.get('incident_number')))}</li>",
            f"<li><strong>Roll Back:</strong> {html.escape(display(record.get('roll_back')))}</li>",
            f"<li><strong>Status:</strong> {html.escape(str(new_status))}</li>",
            f"<li><strong>Git Repository Links:</strong><br>{git_links_html}</li>",
            f"<li><strong>Comments:</strong> {html.escape(display(record.get('comments')))}</li>",
            "</ul>",
            "<p>This is an automated notification from the Deployment Tracker.</p>",
        ]
        message.add_alternative("\n".join(html_details), subtype="html")

        port = int(os.getenv("MAIL_PORT", "587"))
        use_ssl = os.getenv("MAIL_USE_SSL", "false").strip().lower() in ("1", "true", "yes", "on")
        use_tls = os.getenv("MAIL_USE_TLS", "true").strip().lower() in ("1", "true", "yes", "on")
        timeout = int(os.getenv("MAIL_TIMEOUT_SECONDS", "20"))
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as smtp:
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message, to_addrs=recipients)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message, to_addrs=recipients)

        app.logger.info(
            "Status notification sent for %s: %s -> %s to %d recipient(s)",
            cr_number, old_status, new_status, len(recipients),
        )
        return True, None
    except Exception:
        app.logger.exception(
            "Deployment status saved, but notification email failed for CR %s", cr_number
        )
        return False, "Email delivery failed."



@app.route("/", methods=["GET", "POST"])

def Release_Calender():
    if request.method == "GET":
        return render_template(
            "Release_Calender.html",
            **form_context(),
        )
    data = request.form.to_dict()

    # request.form.to_dict() keeps only one value for repeated field names.
    # Git links are dynamically added inputs with the same name, so collect
    # every submitted value separately and pass them to the email function.
    data["git_links"] = [
        link.strip()
        for link in request.form.getlist("git_links[]")
        if link and link.strip()
    ]

    app.logger.info(
        "Git links received from deployment form: %s",
        data["git_links"],
    )

    required_fields = [
        "cr_number",
        "deployment_date",
        "slot_start",
        "application_name",
        "deployment_type",
        "change_type",
        "change_requestor",
    ]
    for field in required_fields:
        if not (data.get(field) or "").strip():
            flash(
                f"{field.replace('_', ' ').title()} is required.",
                "error",
            )
            return render_template(
                "Release_Calender.html",
                **form_context(data),
            ), 400
    try:
        rfid = save_deployment(data)
        cr_number = (data.get("cr_number") or "").strip()
        email_sent, email_message = send_deployment_notification(data, rfid)
        success_message = (
            "Request submitted successfully! "
            f"CR Number: {cr_number} | RFID: {rfid}"
        )
        if email_sent:
            success_message += " | Email notification sent."
            flash(success_message, "success")
        else:
            flash(success_message, "success")
            flash(email_message or "Email notification was not sent.", "warning")
        return redirect(url_for("Release_Calender"))
    except ValueError as exc:
        flash(str(exc), "error")
        data["git_links"] = request.form.getlist("git_links[]")
        return render_template(
            "Release_Calender.html",
            **form_context(data),
        ), 400
    except (RuntimeError, KeyError) as exc:
        app.logger.exception("Request submission failed")
        flash(str(exc), "error")
        data["git_links"] = request.form.getlist("git_links[]")
        return render_template(
            "Release_Calender.html",
            **form_context(data),
        ), 500

@app.route("/manager", methods=["GET"])
def manager_dashboard():
    """Manager dashboard with a week-selectable deployment activity chart."""
    conn = None
    cursor = None
    try:
        week_start_text = (request.args.get("week_start") or "").strip()
        team_from_date_text = (request.args.get("from_date") or "").strip()
        team_to_date_text = (request.args.get("to_date") or "").strip()
        team_filter_error = ""
        team_from_date = None
        team_to_date = None
        try:
            selected_day = date.fromisoformat(week_start_text) if week_start_text else date.today()
        except ValueError:
            selected_day = date.today()

        # Team summary is date-filtered independently of the dashboard's
        # all-time record table and headline metrics.
        if team_from_date_text or team_to_date_text:
            try:
                if not team_from_date_text or not team_to_date_text:
                    raise ValueError("Select both From Date and To Date to view a date range.")
                team_from_date = date.fromisoformat(team_from_date_text)
                team_to_date = date.fromisoformat(team_to_date_text)
                if team_to_date < team_from_date:
                    raise ValueError("To Date must be on or after From Date.")
            except ValueError as exc:
                team_filter_error = str(exc)
                team_from_date = None
                team_to_date = None

        activity_week_start = selected_day - timedelta(days=selected_day.weekday())
        activity_week_end = activity_week_start + timedelta(days=6)
        activity_week_end_exclusive = activity_week_start + timedelta(days=7)
        previous_week_start = activity_week_start - timedelta(days=7)
        next_week_start = activity_week_start + timedelta(days=7)

        day_counts = {activity_week_start + timedelta(days=i): 0 for i in range(7)}

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COALESCE(SUM(deployment_count),0) AS total,

                COALESCE(SUM(
                    CASE
                        WHEN deployment_date = CURRENT_DATE
                        THEN deployment_count
                        ELSE 0
                    END
                ),0) AS daily,

                COALESCE(SUM(
                    CASE
                        WHEN deployment_date >= date_trunc('week', CURRENT_DATE)::date
                        AND deployment_date <
                            (date_trunc('week', CURRENT_DATE)
                            + INTERVAL '1 week')::date
                        THEN deployment_count
                        ELSE 0
                    END
                ),0) AS weekly,

                COALESCE(SUM(
                    CASE
                        WHEN deployment_date >= date_trunc('month', CURRENT_DATE)::date
                        AND deployment_date <
                            (date_trunc('month', CURRENT_DATE)
                            + INTERVAL '1 month')::date
                        THEN deployment_count
                        ELSE 0
                    END
                ),0) AS monthly,

                COALESCE(SUM(
                    CASE
                        WHEN LOWER(TRIM(roll_back)) = 'yes'
                        THEN deployment_count
                        ELSE 0
                    END
                ),0) AS rollbacks
            FROM public.deployment_tracker
        """)
        metric_row = cursor.fetchone()
        metrics = {
            "total": metric_row[0] or 0,
            "daily": metric_row[1] or 0,
            "weekly": metric_row[2] or 0,
            "monthly": metric_row[3] or 0,
            "rollbacks": metric_row[4] or 0,
        }

        cursor.execute("""
            SELECT
                deployment_date,
                COALESCE(SUM(deployment_count),0) AS deployment_total
            FROM public.deployment_tracker
            WHERE deployment_date >= %s
                AND deployment_date < %s
            GROUP BY deployment_date
            ORDER BY deployment_date
        """, (activity_week_start, activity_week_end_exclusive))
        for deployment_day, count in cursor.fetchall():
            if deployment_day in day_counts:
                day_counts[deployment_day] = count

        chart_scale_max = max(4, max(day_counts.values(), default=0))
        activity_chart = []
        for chart_day, count in day_counts.items():
            activity_chart.append({
                "date": chart_day.isoformat(),
                "label": chart_day.strftime("%a"),
                "count": count,
                "height_percent": round((count / chart_scale_max) * 100, 1),
            })
        chart_y_ticks = sorted({
            chart_scale_max,
            round(chart_scale_max * 0.75),
            round(chart_scale_max * 0.5),
            round(chart_scale_max * 0.25),
            0,
        }, reverse=True)

        cursor.execute("""
            SELECT id, cr_number, rfid, deployment_date, slot_start, slot_end,
                   application_name, deployment_count, team_allocations,
                   deployment_type, change_type, released_by,
                   verified_and_deployed_by, change_requestor, incident_number,
                   roll_back, status
            FROM public.deployment_tracker
            ORDER BY deployment_date DESC, slot_start DESC, id DESC
        """)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        records = [dict(zip(columns, row)) for row in rows]

        # Attach persisted Jenkins build/deploy history to each deployment by CR.
        # A refresh updates saved rows; previously saved rows remain visible offline.
        try:
            if JENKINS_USER and JENKINS_TOKEN:
                jenkins_snapshot = fetch_all_jenkins_data()
                _persist_jenkins_builds(_all_jenkins_builds(jenkins_snapshot))
                for record in records:
                    record["jenkins_builds"] = _load_persisted_jenkins_builds(
                        record.get("cr_number")
                    )
            else:
                _persist_jenkins_builds([])
                for record in records:
                    record["jenkins_builds"] = _load_persisted_jenkins_builds(
                        record.get("cr_number")
                    )
        except Exception:
            app.logger.exception("Could not refresh Jenkins history; loading saved records")
            for record in records:
                record["jenkins_builds"] = _load_persisted_jenkins_builds(
                    record.get("cr_number")
                )

        # Prepare readable per-record team allocations for the All Deployment Records table.
        # JSONB may be returned by psycopg2 as a Python list or as serialized JSON text.
        for record in records:
            allocations = record.get("team_allocations") or []
            if isinstance(allocations, str):
                try:
                    allocations = json.loads(allocations)
                except (TypeError, ValueError):
                    allocations = []
            display_allocations = []
            if isinstance(allocations, list):
                for allocation in allocations:
                    if not isinstance(allocation, dict):
                        continue
                    team_name = str(allocation.get("team") or "").strip()
                    try:
                        team_count = int(allocation.get("count") or 0)
                    except (TypeError, ValueError):
                        continue
                    if team_name and team_count > 0:
                        display_allocations.append({"team": team_name, "count": team_count})
            record["team_allocations_display"] = display_allocations

        team_totals = {}
        team_records = records
        if team_from_date and team_to_date:
            team_records = [
                record for record in records
                if record.get("deployment_date")
                and team_from_date <= record["deployment_date"] <= team_to_date
            ]
        for record in team_records:
            allocations = record.get("team_allocations") or []
            if isinstance(allocations, str):
                try:
                    allocations = json.loads(allocations)
                except (TypeError, ValueError):
                    allocations = []
            if not isinstance(allocations, list):
                continue
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    continue
                team_name = str(allocation.get("team") or "").strip()
                try:
                    team_count = int(allocation.get("count") or 0)
                except (TypeError, ValueError):
                    continue
                if team_name and team_count > 0:
                    team_totals[team_name] = team_totals.get(team_name, 0) + team_count

        team_deployment_counts = [
            {"team": team, "count": count}
            for team, count in sorted(team_totals.items(), key=lambda item: (-item[1], item[0].casefold()))
        ]
        team_deployment_total = sum(item["count"] for item in team_deployment_counts)
        team_chart_max = max((item["count"] for item in team_deployment_counts), default=1)

        cursor.execute("""
            SELECT id, deployment_id, cr_number, rfid, modified_at,
                   modified_by, action, changes
            FROM public.deployment_change_history
            ORDER BY modified_at DESC, id DESC
            LIMIT 200
        """)
        history_rows = cursor.fetchall()
        history_columns = [desc[0] for desc in cursor.description]
        change_history = [dict(zip(history_columns, row)) for row in history_rows]

        return render_template(
            "manager.html",
            metrics=metrics,
            records=records,
            team_deployment_counts=team_deployment_counts,
            team_deployment_total=team_deployment_total,
            team_chart_max=team_chart_max,
            team_from_date=team_from_date_text,
            team_to_date=team_to_date_text,
            team_filter_error=team_filter_error,
            team_date_filtered=bool(team_from_date and team_to_date),
            activity_chart=activity_chart,
            chart_y_ticks=chart_y_ticks,
            chart_scale_max=chart_scale_max,
            activity_week_start=activity_week_start,
            activity_week_end=activity_week_end,
            previous_week_start=previous_week_start,
            next_week_start=next_week_start,
            change_history=change_history,
        )
    except Exception:
        app.logger.exception("Unable to load manager dashboard")
        return "Unable to load manager dashboard. Check the Flask logs and database connection.", 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/manager/export-team-deployments", methods=["GET"])
def export_manager_team_deployments():
    """Export team-wise allocated deployment counts for an inclusive date range."""
    start_text = (request.args.get("from_date") or "").strip()
    end_text = (request.args.get("to_date") or "").strip()
    export_format = (request.args.get("format") or "csv").strip().lower()

    try:
        start_date = date.fromisoformat(start_text)
        end_date = date.fromisoformat(end_text)
    except ValueError:
        return "Please provide valid From and To dates.", 400

    if end_date < start_date:
        return "The To date must be on or after the From date.", 400
    if export_format not in {"csv", "xlsx", "excel"}:
        return "Unsupported export format. Choose CSV or Excel.", 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT deployment_date, cr_number, rfid, application_name,
                   deployment_count, team_allocations
            FROM public.deployment_tracker
            WHERE deployment_date >= %s AND deployment_date <= %s
            ORDER BY deployment_date, cr_number, id
        """, (start_date, end_date))

        detail_rows = []
        team_totals = {}
        for deployment_date, cr_number, rfid, application_name, deployment_count, allocations in cursor.fetchall():
            if isinstance(allocations, str):
                try:
                    allocations = json.loads(allocations)
                except (TypeError, ValueError):
                    allocations = []
            if not isinstance(allocations, list):
                allocations = []
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    continue
                team_name = str(allocation.get("team") or "").strip()
                try:
                    team_count = int(allocation.get("count") or 0)
                except (TypeError, ValueError):
                    continue
                if not team_name or team_count <= 0:
                    continue
                team_totals[team_name] = team_totals.get(team_name, 0) + team_count
                detail_rows.append([
                    deployment_date.isoformat() if deployment_date else "",
                    team_name,
                    team_count,
                    cr_number or "",
                    rfid or "",
                    application_name or "",
                ])

        team_summary = sorted(team_totals.items(), key=lambda item: (-item[1], item[0].casefold()))
        filename_range = f"{start_date.isoformat()}_to_{end_date.isoformat()}"

        if export_format == "csv":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["Deployment Date", "Team", "Allocated Deployments", "CR Number", "RFID", "Application"])
            writer.writerows(detail_rows)
            payload = io.BytesIO(output.getvalue().encode("utf-8-sig"))
            return send_file(payload, mimetype="text/csv", as_attachment=True,
                             download_name=f"team_deployment_report_{filename_range}.csv")

        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        workbook = Workbook()
        summary_sheet = workbook.active
        summary_sheet.title = "Team Summary"
        summary_sheet.append(["Team", "Total Deployments"])
        for team_name, total in team_summary:
            summary_sheet.append([team_name, total])
        summary_sheet.append(["Grand Total", sum(team_totals.values())])

        details_sheet = workbook.create_sheet("Deployment Details")
        details_sheet.append(["Deployment Date", "Team", "Allocated Deployments", "CR Number", "RFID", "Application"])
        for row in detail_rows:
            details_sheet.append(row)

        for sheet in workbook.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="263B73")
                cell.alignment = Alignment(horizontal="center")
            for column_cells in sheet.columns:
                max_length = max((len(str(cell.value or "")) for cell in column_cells), default=10)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 3, 14), 42)

        payload = io.BytesIO()
        workbook.save(payload)
        payload.seek(0)
        return send_file(payload,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True,
                         download_name=f"team_deployment_report_{filename_range}.xlsx")
    except Exception:
        app.logger.exception("Unable to export team deployment report")
        return "Unable to export the team deployment report. Check the Flask logs.", 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/manager/export-deployment-records.xlsx", methods=["POST"])
def export_manager_deployment_records_excel():
    """Create an Excel export from the manager table's currently filtered rows."""
    payload = request.get_json(silent=True) or {}
    headers = payload.get("headers") or []
    rows = payload.get("rows") or []
    from_text = (payload.get("from_date") or "").strip()
    to_text = (payload.get("to_date") or "").strip()

    if not isinstance(headers, list) or not isinstance(rows, list):
        return "Invalid export data.", 400
    if len(headers) > 40 or len(rows) > 10000:
        return "Export is too large. Apply filters and try again.", 413
    if any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
        return "Invalid deployment record data.", 400
    try:
        from_date = date.fromisoformat(from_text) if from_text else None
        to_date = date.fromisoformat(to_text) if to_text else None
    except ValueError:
        return "Please provide valid dates.", 400
    if from_date and to_date and to_date < from_date:
        return "To Date must be on or after From Date.", 400

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Deployment Records"
        sheet.append(headers)
        for row in rows:
            sheet.append([str(value) if value is not None else "" for value in row])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="263B73")
            cell.alignment = Alignment(horizontal="center")
        for column_cells in sheet.columns:
            max_length = max((len(str(cell.value or "")) for cell in column_cells), default=10)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 3, 14), 42)

        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        range_label = f"{from_text or 'all'}_to_{to_text or 'dates'}"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"deployment_records_{range_label}.xlsx",
        )
    except Exception:
        app.logger.exception("Unable to export manager deployment records to Excel")
        return "Unable to create Excel report. Check the Flask logs.", 500

#super  manager
@app.route("/super-manager", methods=["GET"])
def super_manager_page():
    denied = require_super_manager()
    if denied:
        return denied
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT max_cr_per_slot FROM public.release_calendar_config WHERE id = 1")
            row = cur.fetchone()
            capacity = int(row[0]) if row else 1
            cur.execute("""
                SELECT blocked_date, message, updated_at
                FROM public.release_calendar_blocks
                ORDER BY blocked_date DESC
            """)
            blocks = cur.fetchall()
            cur.execute("""
                SELECT id, cr_number, rfid, deployment_date, slots, application_name,
                       status, change_requestor
                FROM public.deployment_tracker
                ORDER BY deployment_date DESC, slot_start DESC, id DESC
                LIMIT 1000
            """)
            deployments = cur.fetchall()
            cur.execute("SELECT email FROM public.audit_users ORDER BY email")
            users = [r[0] for r in cur.fetchall()]
        return render_template(
            "super_manager.html",
            capacity=capacity,
            blocks=blocks,
            deployments=deployments,
            users=users,
        )
    except Exception:
        app.logger.exception("Could not load Super Manager page")
        return "Could not load Super Manager page. Check server logs and run super_manager_setup.sql.", 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/super-manager/settings", methods=["POST"])
def super_manager_update_capacity():
    denied = require_super_manager()
    if denied:
        return denied
    try:
        capacity = int(request.form.get("max_cr_per_slot", ""))
        if capacity < 1 or capacity > 100:
            raise ValueError
    except (TypeError, ValueError):
        flash("CRs per slot must be a whole number from 1 to 100.", "error")
        return redirect(url_for("super_manager_page"))
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.release_calendar_config (id, max_cr_per_slot, updated_at)
                    VALUES (1, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (id) DO UPDATE
                    SET max_cr_per_slot = EXCLUDED.max_cr_per_slot,
                        updated_at = CURRENT_TIMESTAMP
                """, (capacity,))
        flash(f"Release Calendar updated: up to {capacity} CR request(s) per slot.", "success")
    except Exception:
        app.logger.exception("Could not update slot capacity")
        flash("Could not update slot capacity. Check server logs.", "error")
    finally:
        if conn is not None:
            conn.close()
    return redirect(url_for("super_manager_page"))


@app.route("/super-manager/block-date", methods=["POST"])
def super_manager_block_date():
    denied = require_super_manager()
    if denied:
        return denied

    # Supports both the existing single-date controls and the new multi-weekend picker.
    bulk_weekend_selection = bool(request.form.getlist("blocked_dates"))
    date_texts = [value.strip() for value in request.form.getlist("blocked_dates") if value.strip()]
    if not date_texts:
        single_date = (request.form.get("blocked_date") or "").strip()
        if single_date:
            date_texts = [single_date]

    message = (request.form.get("message") or "").strip()
    action = (request.form.get("action") or "block").strip().lower()

    try:
        if action not in ("block", "unblock"):
            raise ValueError("Invalid date action.")
        if not date_texts:
            raise ValueError("Select at least one date.")
        if action == "block" and not message:
            raise ValueError("Enter a message to display to users.")
        if len(message) > 1000:
            raise ValueError("The message cannot exceed 1000 characters.")

        selected_dates = sorted({date.fromisoformat(value) for value in date_texts})
        # The weekend picker accepts Saturdays and Sundays only, even for one selected date.
        if bulk_weekend_selection and any(day.weekday() not in (5, 6) for day in selected_dates):
            raise ValueError("The weekend picker can contain Saturdays and Sundays only.")
        if len(selected_dates) > 200:
            raise ValueError("You can update a maximum of 200 dates at one time.")
    except ValueError as exc:
        flash(str(exc) or "Choose valid dates.", "error")
        return redirect(url_for("super_manager_page"))

    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                for selected_date in selected_dates:
                    if action == "unblock":
                        cur.execute(
                            "DELETE FROM public.release_calendar_blocks WHERE blocked_date = %s",
                            (selected_date,),
                        )
                    else:
                        cur.execute("""
                            INSERT INTO public.release_calendar_blocks
                                (blocked_date, message, updated_at)
                            VALUES (%s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (blocked_date) DO UPDATE
                            SET message = EXCLUDED.message, updated_at = CURRENT_TIMESTAMP
                        """, (selected_date, message))
        if action == "unblock":
            flash(f"{len(selected_dates)} selected date(s) reopened for booking.", "success")
        else:
            flash(f"{len(selected_dates)} selected date(s) blocked. New bookings are disabled on those dates.", "success")
    except Exception:
        app.logger.exception("Could not change blocked date(s)")
        flash("Could not update blocked dates. Check server logs.", "error")
    finally:
        if conn is not None:
            conn.close()
    return redirect(url_for("super_manager_page"))


@app.route("/super-manager/delete-requests", methods=["POST"])
def super_manager_delete_requests():
    denied = require_super_manager()
    if denied:
        return denied
    raw_ids = request.form.getlist("deployment_ids")
    ids = []
    for item in raw_ids:
        try:
            value = int(item)
            if value > 0:
                ids.append(value)
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))
    if not ids:
        flash("Select at least one deployment request to delete.", "error")
        return redirect(url_for("super_manager_page"))
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, cr_number, rfid
                    FROM public.deployment_tracker WHERE id = ANY(%s)
                """, (ids,))
                rows = cur.fetchall()
                cur.execute("DELETE FROM public.deployment_tracker WHERE id = ANY(%s)", (ids,))
                deleted = cur.rowcount
                for record_id, cr_number, rfid in rows:
                    cur.execute("""
                        INSERT INTO public.deployment_tracker_logs
                            (event_time, log_level, message, cr_number, rfid)
                        VALUES (CURRENT_TIMESTAMP, 'WARNING', %s, %s, %s)
                    """, (f"Super Manager deleted deployment request id={record_id}", cr_number, rfid))
        flash(f"Deleted {deleted} deployment request(s). Existing change-history rows were not deleted.", "success")
    except Exception:
        app.logger.exception("Bulk deployment deletion failed")
        flash("Could not delete selected requests. Check logs.", "error")
    finally:
        if conn is not None:
            conn.close()
    return redirect(url_for("super_manager_page"))


@app.route("/super-manager/delete-user", methods=["POST"])
def super_manager_delete_user():
    denied = require_super_manager()
    if denied:
        return denied
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Select a registered user to remove.", "error")
        return redirect(url_for("super_manager_page"))
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.audit_users WHERE LOWER(email) = %s", (email,))
                deleted = cur.rowcount
        flash(f"Removed {email} from the audit-user registry." if deleted else "User was not found.", "success" if deleted else "error")
    except Exception:
        app.logger.exception("Could not delete audit user")
        flash("Could not remove the user. Check server logs.", "error")
    finally:
        if conn is not None:
            conn.close()
    return redirect(url_for("super_manager_page"))

@app.route("/super-manager/unlock", methods=["GET", "POST"])
def super_manager_unlock():
    if request.method == "POST":
        submitted_key = request.form.get("access_key", "")
        expected_key = os.getenv("SUPER_MANAGER_ACCESS_KEY", "")

        if not expected_key:
            return (
                "Super Manager is not configured. Add SUPER_MANAGER_ACCESS_KEY "
                "to your .env file and restart the Flask app.",
                500,
            )

        if not hmac.compare_digest(submitted_key, expected_key):
            app.logger.warning("Failed Super Manager access attempt")
            return (
                "<!doctype html><html><head><title>Access denied</title></head>"
                "<body><h2>Invalid access key</h2>"
                "<p><a href='/super-manager/unlock'>Try again</a></p></body></html>",
                403,
            )

        session.clear()
        session["role"] = "super_manager"
        session.permanent = False
        return redirect(url_for("super_manager_page"))

    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Super Manager Access</title>
  <style>
    body{font-family:Arial,sans-serif;background:#f3f5f9;display:grid;place-items:center;
         min-height:100vh;margin:0;color:#172033}
    form{background:#fff;padding:28px;border-radius:12px;box-shadow:0 8px 30px #0001;
         width:min(360px,calc(100% - 48px))}
    h2{margin-top:0}input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;
         border-radius:7px;font-size:16px}input{border:1px solid #ccd2df}
    button{border:0;background:#174ea6;color:#fff;font-weight:bold;cursor:pointer}
  </style>
</head>
<body>
  <form method="post">
    <h2>Super Manager</h2>
    <p>Enter your Super Manager access key.</p>
    <input type="password" name="access_key" placeholder="Access key" required autofocus>
    <button type="submit">Unlock</button>
  </form>
</body>
</html>"""


@app.route("/super-manager/logout", methods=["POST"])
def super_manager_logout():
    session.clear()
    return redirect(url_for("super_manager_unlock"))


if __name__ == "__main__":
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=PORT,
        debug=False,
    )