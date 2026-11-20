"""Flask app, environment, logging, DB connection and role helpers."""

import logging
import os
import psycopg2
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, jsonify, session
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


load_dotenv(BASE_DIR / ".env")


app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder =str(BASE_DIR / "static"),
    static_url_path = "/static"
)


PORT = int(os.getenv("PORT", "5050"))


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


def is_manager_user():
    return str(session.get("role", "")).strip().lower() == "manager"


def is_super_manager_user():
    """Super-manager access is separate from ordinary manager access."""
    return str(session.get("role", "")).strip().lower().replace(" ", "_") == "super_manager"


def require_super_manager():
    if not is_super_manager_user():
        return jsonify({"success": False, "message": "Super Manager access is required."}), 403
    return None


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
