import io
import zipfile

from checks import analyze


def make_zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buffer.getvalue()


def failed_checks(result: dict) -> list:
    return [c["name"] for c in result["checks"] if not c["passed"] and c["required"]]


def test_empty_file_fails():
    result = analyze("hw.zip", b"")
    assert result["status"] == "failed"
    assert failed_checks(result) == ["not_empty"]


def test_good_python_project():
    data = make_zip({"hw/main.py": "print(1)\n", "hw/README.md": "# hw"})
    result = analyze("hw.zip", data)
    assert result["is_success"] is True
    assert result["language"] == "Python"


def test_readme_only_archive_fails():
    result = analyze("hw.zip", make_zip({"README.md": "скоро будет"}))
    assert result["status"] == "failed"
    assert "has_source_files" in failed_checks(result)


def test_python_syntax_error_fails():
    result = analyze("hw.zip", make_zip({"main.py": "def f(:\n    pass\n"}))
    assert failed_checks(result) == ["python_syntax"]
    message = [c["message"] for c in result["checks"] if c["name"] == "python_syntax"][0]
    assert "main.py, строка 1" in message


def test_source_without_entry_point_fails():
    result = analyze("hw.zip", make_zip({"src/helper.py": "x = 1\n"}))
    assert failed_checks(result) == ["has_entry_point"]


def test_java_project_by_pom():
    data = make_zip({"pom.xml": "<project/>", "src/Main.java": "class Main {}"})
    result = analyze("hw.zip", data)
    assert result["is_success"] is True
    assert result["language"] == "Java/Kotlin"


def test_missing_readme_is_only_warning():
    result = analyze("hw.zip", make_zip({"main.py": "print(1)\n"}))
    readme = [c for c in result["checks"] if c["name"] == "has_readme"][0]
    assert readme["passed"] is False and readme["required"] is False
    assert result["is_success"] is True


def test_broken_archive_fails():
    result = analyze("hw.zip", b"PK\x03\x04 definitely not a zip")
    assert "valid_archive" in failed_checks(result)


def test_single_python_file():
    assert analyze("main.py", b"print('hi')\n")["is_success"] is True
    assert analyze("main.py", b"print('hi'\n")["is_success"] is False


def test_single_non_source_file_fails():
    result = analyze("notes.txt", b"just text")
    assert failed_checks(result) == ["is_source_code"]


def test_code_is_not_executed():
    # если бы код выполнялся, тест упал бы с SystemExit
    result = analyze("main.py", b"raise SystemExit('executed!')\n")
    assert result["is_success"] is True
