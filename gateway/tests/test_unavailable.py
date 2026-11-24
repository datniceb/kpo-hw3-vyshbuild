"""Gateway, когда сервисы недоступны: должен отвечать понятным JSON с кодом 503.

Адреса сервисов указывают на закрытый порт — соединение сразу отклоняется, как будто сервис упал.
"""

import io
import os

os.environ["STORE_URL"] = "http://127.0.0.1:1"
os.environ["ANALYSIS_URL"] = "http://127.0.0.1:1"

from app import app  # noqa: E402  (импорт после настройки переменных окружения)

client = app.test_client()


def test_submit_when_store_is_down():
    resp = client.post(
        "/submissions",
        data={"student": "Иванов", "assignment_id": "hw-1", "file": (io.BytesIO(b"print(1)"), "main.py")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["service"] == "file_storing"
    assert "недоступен" in body["error"]


def test_reports_when_analysis_is_down():
    resp = client.get("/assignments/hw-1/reports")
    assert resp.status_code == 503
    assert resp.get_json()["service"] == "file_analysis"


def test_health_shows_who_is_down():
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body == {"gateway": "ok", "file_storing": "unavailable", "file_analysis": "unavailable"}


def test_submit_without_file_is_400():
    resp = client.post("/submissions", data={"student": "Иванов", "assignment_id": "hw-1"})
    assert resp.status_code == 400


def test_unknown_route_is_json():
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert "error" in resp.get_json()
