"""File Storing Service — хранит сданные файлы (артефакты) и записи о сдачах.

Ничего не анализирует и ни к кому не ходит: только сохранить и отдать.
"""

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_file

DATA_DIR = os.environ.get("DATA_DIR", "./data")
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "store.db")
PORT = int(os.environ.get("PORT", "8001"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "20"))
MAX_TEXT_LEN = 100

app = Flask(__name__)
app.json.ensure_ascii = False  # кириллица в ответах читается нормально, без \u0420...
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024


# ---------- база ----------


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(FILES_DIR, exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                student TEXT NOT NULL,
                assignment_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """
        )


def row_to_dict(row) -> dict:
    # путь на диске наружу не отдаём — это внутреннее дело сервиса
    return {
        "id": row["id"],
        "student": row["student"],
        "assignment_id": row["assignment_id"],
        "filename": row["original_filename"],
        "size": row["size"],
        "sha256": row["sha256"],
        "uploaded_at": row["uploaded_at"],
    }


def find_submission(submission_id: str):
    with get_db() as conn:
        return conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()


def error(message: str, status: int):
    return jsonify({"error": message}), status


# ---------- API ----------


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "file-storing"})


@app.post("/submissions")
def create_submission():
    student = request.form.get("student", "").strip()
    assignment_id = request.form.get("assignment_id", "").strip()
    file = request.files.get("file")

    if not student:
        return error("Не указан студент (поле student)", 400)
    if not assignment_id:
        return error("Не указано задание (поле assignment_id)", 400)
    if len(student) > MAX_TEXT_LEN or len(assignment_id) > MAX_TEXT_LEN:
        return error(f"student и assignment_id должны быть не длиннее {MAX_TEXT_LEN} символов", 400)
    if file is None or file.filename == "":
        return error("Не приложен файл (поле file)", 400)

    content = file.read()
    submission_id = uuid.uuid4().hex
    # файл сохраняем под id, а не под именем от пользователя — так нельзя подсунуть путь вроде ../../etc
    stored_path = os.path.join(FILES_DIR, submission_id)
    with open(stored_path, "wb") as f:
        f.write(content)

    uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sha256 = hashlib.sha256(content).hexdigest()
    filename = os.path.basename(file.filename)[:255]

    with get_db() as conn:
        conn.execute(
            "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (submission_id, student, assignment_id, filename, len(content), sha256, stored_path, uploaded_at),
        )

    return jsonify(row_to_dict(find_submission(submission_id))), 201


@app.get("/submissions/<submission_id>")
def get_submission(submission_id):
    row = find_submission(submission_id)
    if row is None:
        return error("Сдача не найдена", 404)
    return jsonify(row_to_dict(row))


@app.get("/submissions/<submission_id>/file")
def download_file(submission_id):
    row = find_submission(submission_id)
    if row is None:
        return error("Сдача не найдена", 404)
    if not os.path.exists(row["stored_path"]):
        return error("Запись о сдаче есть, но файл на диске пропал", 500)
    return send_file(row["stored_path"], as_attachment=True, download_name=row["original_filename"])


# ---------- ошибки всегда в JSON, а не HTML-страницей ----------


@app.errorhandler(404)
def not_found(e):
    return error("Такого адреса нет", 404)


@app.errorhandler(405)
def method_not_allowed(e):
    return error("Метод не поддерживается для этого адреса", 405)


@app.errorhandler(413)
def too_large(e):
    return error(f"Файл слишком большой: максимум {MAX_FILE_MB} МБ", 413)


@app.errorhandler(500)
def internal_error(e):
    return error("Внутренняя ошибка File Storing Service", 500)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
