"""File Analysis Service (Build Service) — проверяет сдачи «на сборку» и хранит отчёты.

Сам файлы не хранит: за файлом синхронно ходит в File Storing Service.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

from checks import analyze

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "analysis.db")
PORT = int(os.environ.get("PORT", "8002"))
STORE_URL = os.environ.get("STORE_URL", "http://localhost:8001")
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "10"))

app = Flask(__name__)
app.json.ensure_ascii = False  # кириллица в ответах читается нормально, без \u0420...


# ---------- база ----------


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                student TEXT NOT NULL,
                assignment_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                is_success INTEGER NOT NULL,
                language TEXT,
                checks TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "submission_id": row["submission_id"],
        "student": row["student"],
        "assignment_id": row["assignment_id"],
        "filename": row["filename"],
        "status": row["status"],
        "is_success": bool(row["is_success"]),
        "language": row["language"],
        "checks": json.loads(row["checks"]),
        "created_at": row["created_at"],
    }


def error(message: str, status: int):
    return jsonify({"error": message}), status


# ---------- запросы в File Storing Service ----------


class StoreError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def store_get(path: str) -> requests.Response:
    try:
        resp = requests.get(f"{STORE_URL}{path}", timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        raise StoreError(f"File Storing Service не ответил за {TIMEOUT:g} с", 503)
    except requests.exceptions.ConnectionError:
        raise StoreError("File Storing Service недоступен — не удалось получить файл сдачи", 503)
    if resp.status_code == 404:
        raise StoreError("Сдача с таким id не найдена в File Storing Service", 404)
    if resp.status_code != 200:
        raise StoreError(f"File Storing Service вернул ошибку {resp.status_code}", 502)
    return resp


# ---------- API ----------


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "file-analysis"})


@app.post("/reports")
def create_report():
    body = request.get_json(silent=True) or {}
    submission_id = str(body.get("submission_id", "")).strip()
    if not submission_id:
        return error("Нужно передать submission_id в JSON", 400)

    try:
        submission = store_get(f"/submissions/{submission_id}").json()
        data = store_get(f"/submissions/{submission_id}/file").content
    except StoreError as e:
        return error(e.message, e.status)

    result = analyze(submission["filename"], data)

    report_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                submission_id,
                submission["student"],
                submission["assignment_id"],
                submission["filename"],
                result["status"],
                int(result["is_success"]),
                result["language"],
                json.dumps(result["checks"], ensure_ascii=False),
                created_at,
            ),
        )
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.get("/reports/<report_id>")
def get_report(report_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        return error("Отчёт не найден", 404)
    return jsonify(row_to_dict(row))


@app.get("/submissions/<submission_id>/reports")
def reports_for_submission(submission_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE submission_id = ? ORDER BY created_at DESC", (submission_id,)
        ).fetchall()
    return jsonify({"submission_id": submission_id, "reports": [row_to_dict(r) for r in rows]})


@app.get("/assignments/<assignment_id>/reports")
def reports_for_assignment(assignment_id):
    """Сводка для преподавателя: все отчёты по заданию и сколько собралось / не собралось."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE assignment_id = ? ORDER BY created_at", (assignment_id,)
        ).fetchall()
    reports = []
    for r in rows:
        reports.append(
            {
                "report_id": r["id"],
                "submission_id": r["submission_id"],
                "student": r["student"],
                "filename": r["filename"],
                "status": r["status"],
                "is_success": bool(r["is_success"]),
                "created_at": r["created_at"],
            }
        )
    success = sum(1 for r in reports if r["is_success"])
    return jsonify(
        {
            "assignment_id": assignment_id,
            "total": len(reports),
            "success": success,
            "failed": len(reports) - success,
            "reports": reports,
        }
    )


# ---------- ошибки всегда в JSON ----------


@app.errorhandler(404)
def not_found(e):
    return error("Такого адреса нет", 404)


@app.errorhandler(405)
def method_not_allowed(e):
    return error("Метод не поддерживается для этого адреса", 405)


@app.errorhandler(500)
def internal_error(e):
    return error("Внутренняя ошибка File Analysis Service", 500)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
