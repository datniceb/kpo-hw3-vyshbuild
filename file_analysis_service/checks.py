"""Эвристическая «проверка на собираемость».

Код студентов НЕ запускается и НЕ компилируется по-настоящему. Проверяем:
1. файл не пустой;
2. архив (если это zip) целый и не подозрительно огромный;
3. внутри есть исходники, а не только README;
4. есть точка входа / файл сборки (main.py, pom.xml, CMakeLists.txt, ...);
5. Python-файлы без синтаксических ошибок (compile() только разбирает код, не выполняя его).

Итог: success, если все обязательные проверки прошли. Предупреждения (например, нет README) на итог не влияют.
"""

import io
import os
import zipfile

SOURCE_EXTENSIONS = {
    ".py",
    ".java",
    ".kt",
    ".cs",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".hpp",
    ".go",
    ".rs",
    ".js",
    ".ts",
}

# по каким файлам понимаем, что это проект и на каком он языке
BUILD_MARKERS = {
    "Python": ["main.py", "__main__.py", "pyproject.toml", "requirements.txt", "setup.py"],
    "Java/Kotlin": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "C#": [".csproj", ".sln"],
    "C/C++": ["CMakeLists.txt", "Makefile"],
    "Go": ["go.mod"],
    "Rust": ["Cargo.toml"],
    "JavaScript/TypeScript": ["package.json"],
}

MAX_FILES_IN_ARCHIVE = 2000
MAX_UNPACKED_MB = 100
MAX_PY_FILE_KB = 1024
MAX_ERRORS_SHOWN = 5


def make_check(name: str, passed: bool, message: str, required: bool = True) -> dict:
    return {"name": name, "passed": passed, "required": required, "message": message}


def finish(checks: list, language: str | None = None) -> dict:
    is_success = all(c["passed"] for c in checks if c["required"])
    return {
        "status": "success" if is_success else "failed",
        "is_success": is_success,
        "language": language,
        "checks": checks,
    }


def extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def detect_language(names: list[str]) -> str | None:
    basenames = [os.path.basename(n) for n in names]
    for language, markers in BUILD_MARKERS.items():
        for marker in markers:
            if marker.startswith("."):  # маркер-расширение, например .csproj
                if any(b.lower().endswith(marker) for b in basenames):
                    return language
            elif marker in basenames:
                return language
    return None


def python_syntax_errors(files: list[tuple[str, bytes]]) -> list[str]:
    """Возвращает список ошибок вида 'main.py, строка 3: invalid syntax'."""
    errors = []
    for name, source in files:
        if len(source) > MAX_PY_FILE_KB * 1024:
            errors.append(f"{name}: файл больше {MAX_PY_FILE_KB} КБ, не проверяли")
            continue
        try:
            # compile только разбирает код в байткод — ничего не выполняется
            compile(source, name, "exec", dont_inherit=True)
        except SyntaxError as e:
            errors.append(f"{name}, строка {e.lineno}: {e.msg}")
        except Exception as e:  # например, нулевые байты в файле
            errors.append(f"{name}: {e}")
    return errors


def add_python_check(checks: list, py_files: list[tuple[str, bytes]]) -> None:
    if not py_files:
        return
    errors = python_syntax_errors(py_files)
    if errors:
        shown = "; ".join(errors[:MAX_ERRORS_SHOWN])
        more = f" (и ещё {len(errors) - MAX_ERRORS_SHOWN})" if len(errors) > MAX_ERRORS_SHOWN else ""
        checks.append(make_check("python_syntax", False, f"Синтаксические ошибки: {shown}{more}"))
    else:
        checks.append(make_check("python_syntax", True, f"Python-файлов без синтаксических ошибок: {len(py_files)}"))


def analyze(filename: str, data: bytes) -> dict:
    checks = []

    if len(data) == 0:
        checks.append(make_check("not_empty", False, "Файл пустой (0 байт)"))
        return finish(checks)
    checks.append(make_check("not_empty", True, f"Размер файла: {len(data)} байт"))

    if extension(filename) == ".zip" or zipfile.is_zipfile(io.BytesIO(data)):
        return analyze_archive(data, checks)
    return analyze_single_file(filename, data, checks)


def analyze_single_file(filename: str, data: bytes, checks: list) -> dict:
    ext = extension(filename)
    if ext not in SOURCE_EXTENSIONS:
        checks.append(make_check("is_source_code", False, f"Файл «{filename}» не похож на исходный код"))
        return finish(checks)
    checks.append(make_check("is_source_code", True, f"Исходный файл {ext}"))

    language = "Python" if ext == ".py" else None
    if ext == ".py":
        add_python_check(checks, [(filename, data)])
    return finish(checks, language)


def analyze_archive(data: bytes, checks: list) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        broken = archive.testzip()  # проверяет контрольные суммы всех файлов
    except Exception as e:  # битый, зашифрованный архив и т.п.
        checks.append(make_check("valid_archive", False, f"Архив не открывается: {e}"))
        return finish(checks)
    if broken is not None:
        checks.append(make_check("valid_archive", False, f"Архив повреждён, битый файл: {broken}"))
        return finish(checks)

    # папки и мусор от macOS не считаем
    entries = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    names = [i.filename for i in entries]
    checks.append(make_check("valid_archive", True, f"Архив целый, файлов внутри: {len(entries)}"))

    unpacked_mb = sum(i.file_size for i in entries) / (1024 * 1024)
    if len(entries) > MAX_FILES_IN_ARCHIVE or unpacked_mb > MAX_UNPACKED_MB:
        checks.append(
            make_check(
                "archive_limits",
                False,
                f"Слишком большой архив: {len(entries)} файлов, {unpacked_mb:.1f} МБ в распакованном виде "
                f"(лимит {MAX_FILES_IN_ARCHIVE} файлов и {MAX_UNPACKED_MB} МБ)",
            )
        )
        return finish(checks)

    sources = [n for n in names if extension(n) in SOURCE_EXTENSIONS]
    if sources:
        checks.append(make_check("has_source_files", True, f"Исходных файлов: {len(sources)}"))
    else:
        checks.append(make_check("has_source_files", False, "В архиве нет ни одного файла с исходным кодом"))

    language = detect_language(names)
    if language:
        checks.append(make_check("has_entry_point", True, f"Найдена точка входа / файл сборки, язык: {language}"))
    else:
        checks.append(
            make_check("has_entry_point", False, "Не найдено точки входа или файла сборки (main.py, pom.xml, ...)")
        )

    has_readme = any(os.path.basename(n).lower().startswith("readme") for n in names)
    checks.append(make_check("has_readme", has_readme, "README есть" if has_readme else "README нет", required=False))

    py_files = [(n, archive.read(n)) for n in names if extension(n) == ".py"]
    add_python_check(checks, py_files)
    return finish(checks, language)
