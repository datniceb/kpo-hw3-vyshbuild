"""API Gateway — единственная точка входа для клиентов.

Своих данных не хранит. Принимает запросы, синхронно вызывает File Storing и File Analysis
и собирает сценарий сдачи: сохранить файл -> проанализировать -> ответить клиенту.
Если сервис недоступен, клиент получает JSON с понятным текстом, а не «молчаливый» обрыв.
"""

import os

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

PORT = int(os.environ.get("PORT", "8000"))
STORE_URL = os.environ.get("STORE_URL", "http://localhost:8001")
ANALYSIS_URL = os.environ.get("ANALYSIS_URL", "http://localhost:8002")
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "30"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "20"))

SERVICE_NAMES = {
    "file_storing": "File Storing Service",
    "file_analysis": "File Analysis Service",
}

app = Flask(__name__)
app.json.ensure_ascii = False  # кириллица в ответах читается нормально, без \u0420...
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024


def error(message: str, status: int, **extra):
    body = {"error": message}
    body.update(extra)
    return jsonify(body), status


# ---------- вызов сервисов ----------


class ServiceUnavailable(Exception):
    """Сервис не ответил: упал, не запущен или завис дольше таймаута."""

    def __init__(self, service: str, reason: str):
        super().__init__(reason)
        self.service = service
        self.message = f"{SERVICE_NAMES[service]} {reason}"


def call(service: str, method: str, url: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.exceptions.Timeout:
        raise ServiceUnavailable(service, f"не ответил за {TIMEOUT:g} с")
    except requests.exceptions.ConnectionError:
        raise ServiceUnavailable(service, "недоступен (не запущен или упал)")


def read_json(service: str, resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return {"error": f"{SERVICE_NAMES[service]} вернул ответ не в формате JSON"}


def forward(service: str, resp: requests.Response):
    """Отдаёт клиенту ответ сервиса. Его 5xx превращаем в 502 с понятным текстом."""
    body = read_json(service, resp)
    if resp.status_code >= 500 and resp.status_code != 503:
        return error(f"{SERVICE_NAMES[service]} вернул внутреннюю ошибку", 502, details=body)
    return jsonify(body), resp.status_code


@app.errorhandler(ServiceUnavailable)
def service_unavailable(e):
    return error(e.message, 503, service=e.service)


# ---------- сценарий сдачи ----------


def run_analysis(submission: dict):
    """Синхронно запускает анализ. Если не вышло — объясняем, что сдача при этом сохранена."""
    retry_hint = f"POST /submissions/{submission['id']}/analyze"
    try:
        resp = call("file_analysis", "POST", f"{ANALYSIS_URL}/reports", json={"submission_id": submission["id"]})
    except ServiceUnavailable as e:
        return error(
            f"Работа сохранена, но проверка не выполнена: {e.message}",
            503,
            service=e.service,
            submission=submission,
            report=None,
            retry=retry_hint,
        )

    body = read_json("file_analysis", resp)
    if resp.status_code != 201:
        status = resp.status_code if resp.status_code in (404, 503) else 502
        return error(
            f"Работа сохранена, но проверка не выполнена: {body.get('error', 'неизвестная ошибка')}",
            status,
            service="file_analysis",
            submission=submission,
            report=None,
            retry=retry_hint,
        )
    return jsonify({"submission": submission, "report": body}), 201


@app.post("/submissions")
def submit():
    """Сценарий сдачи: Gateway -> Store (сохранить) -> Analysis (проверить) -> ответ клиенту."""
    file = request.files.get("file")
    if file is None or file.filename == "":
        return error("Не приложен файл (поле file)", 400)

    resp = call(
        "file_storing",
        "POST",
        f"{STORE_URL}/submissions",
        data={
            "student": request.form.get("student", ""),
            "assignment_id": request.form.get("assignment_id", ""),
        },
        files={"file": (file.filename, file.read(), file.mimetype or "application/octet-stream")},
    )
    if resp.status_code != 201:
        return forward("file_storing", resp)

    submission = read_json("file_storing", resp)
    if "id" not in submission:
        return error("File Storing Service вернул некорректный ответ", 502, details=submission)
    return run_analysis(submission)


@app.post("/submissions/<submission_id>/analyze")
def reanalyze(submission_id):
    """Повторный запуск проверки — например, если в момент сдачи анализ был недоступен."""
    resp = call("file_storing", "GET", f"{STORE_URL}/submissions/{submission_id}")
    if resp.status_code != 200:
        return forward("file_storing", resp)
    return run_analysis(read_json("file_storing", resp))


# ---------- просмотр ----------


@app.get("/submissions/<submission_id>")
def get_submission(submission_id):
    return forward("file_storing", call("file_storing", "GET", f"{STORE_URL}/submissions/{submission_id}"))


@app.get("/submissions/<submission_id>/file")
def download_file(submission_id):
    resp = call("file_storing", "GET", f"{STORE_URL}/submissions/{submission_id}/file")
    if resp.status_code != 200:
        return forward("file_storing", resp)
    headers = {}
    if "Content-Disposition" in resp.headers:
        headers["Content-Disposition"] = resp.headers["Content-Disposition"]
    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    return Response(resp.content, status=200, headers=headers, content_type=content_type)


@app.get("/submissions/<submission_id>/reports")
def submission_reports(submission_id):
    return forward("file_analysis", call("file_analysis", "GET", f"{ANALYSIS_URL}/submissions/{submission_id}/reports"))


@app.get("/reports/<report_id>")
def get_report(report_id):
    return forward("file_analysis", call("file_analysis", "GET", f"{ANALYSIS_URL}/reports/{report_id}"))


@app.get("/assignments/<assignment_id>/reports")
def assignment_reports(assignment_id):
    """Сводка для преподавателя по заданию."""
    return forward("file_analysis", call("file_analysis", "GET", f"{ANALYSIS_URL}/assignments/{assignment_id}/reports"))


@app.get("/health")
def health():
    """Состояние gateway и обоих сервисов — удобно смотреть, кто упал."""
    result = {"gateway": "ok"}
    for service, url in (("file_storing", STORE_URL), ("file_analysis", ANALYSIS_URL)):
        try:
            resp = requests.get(f"{url}/health", timeout=3)
            result[service] = "ok" if resp.status_code == 200 else f"error {resp.status_code}"
        except requests.exceptions.RequestException:
            result[service] = "unavailable"
    all_ok = all(v == "ok" for v in result.values())
    return jsonify(result), 200 if all_ok else 503


# ---------- Swagger ----------

SWAGGER_PAGE = """<!DOCTYPE html>
<html>
<head>
  <title>ВышBuild API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>SwaggerUIBundle({url: "/openapi.yaml", dom_id: "#swagger-ui"});</script>
</body>
</html>
"""


@app.get("/docs")
def docs():
    return Response(SWAGGER_PAGE, content_type="text/html; charset=utf-8")


@app.get("/openapi.yaml")
def openapi():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "openapi.yaml", mimetype="text/yaml")


# ---------- ошибки gateway — тоже JSON ----------


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
    return error("Внутренняя ошибка API Gateway", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
