"""
Testovi za coursera-inventory-collector.
Pokreni: pytest -v
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Dodaj root folder u path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coursera_collector import (
    Config,
    FetchStatus,
    extract_slug,
    fetch_course,
    validate_input_file,
    read_urls,
    load_checkpoint,
    save_checkpoint_safe,
    export_results,
    create_session,
)


# ============================================================
# TESTIRANJE Config
# ============================================================

class TestConfig:
    """Testovi za Config klasu."""

    def test_http_timeout(self):
        assert Config.HTTP_TIMEOUT == 15

    def test_default_delay(self):
        assert Config.DEFAULT_DELAY == 0.5

    def test_max_urls(self):
        assert Config.MAX_URLS == 10_000

    def test_api_url(self):
        assert "coursera.org" in Config.API_URL

    def test_user_agent(self):
        assert "Coursera" in Config.USER_AGENT


# ============================================================
# TESTIRANJE FetchStatus
# ============================================================

class TestFetchStatus:
    """Testovi za FetchStatus enum."""

    def test_status_values(self):
        assert FetchStatus.OK.value == "OK"
        assert FetchStatus.TIMEOUT.value == "TIMEOUT"
        assert FetchStatus.HTTP_404.value == "HTTP_404"
        assert FetchStatus.HTTP_429_RATE_LIMIT.value == "HTTP_429_RATE_LIMIT"

    def test_status_str(self):
        assert str(FetchStatus.OK) == "OK"
        assert str(FetchStatus.TIMEOUT) == "TIMEOUT"

    def test_all_statuses(self):
        assert len(list(FetchStatus)) == 13


# ============================================================
# TESTIRANJE extract_slug
# ============================================================

class TestExtractSlug:
    """Testovi za extract_slug funkciju."""

    def test_full_url(self):
        url = "https://www.coursera.org/learn/machine-learning"
        assert extract_slug(url) == "machine-learning"

    def test_url_with_query_slug(self):
        url = "https://www.coursera.org/learn?slug=python"
        assert extract_slug(url) == "python"

    def test_url_with_trailing_slash(self):
        url = "https://www.coursera.org/learn/data-science/"
        assert extract_slug(url) == "data-science"

    def test_empty_url(self):
        assert extract_slug("") == ""

    def test_only_slug(self):
        assert extract_slug("machine-learning") == "machine-learning"

    def test_whitespace_trimmed(self):
        url = "  https://www.coursera.org/learn/python  "
        assert extract_slug(url) == "python"

    def test_invalid_url(self):
        # Nevalidan URL — ne bi trebalo da pukne
        result = extract_slug("not a valid url !!!")
        assert isinstance(result, str)


# ============================================================
# TESTIRANJE validate_input_file
# ============================================================

class TestValidateInputFile:
    """Testovi za validate_input_file funkciju."""

    def test_existing_file(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("https://example.com\n")
        assert validate_input_file(f) is True

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nema.txt"
        assert validate_input_file(f) is False

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert validate_input_file(f) is False

    def test_too_large_file(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * (Config.MAX_FILE_SIZE + 1))
        assert validate_input_file(f) is False


# ============================================================
# TESTIRANJE read_urls
# ============================================================

class TestReadUrls:
    """Testovi za read_urls funkciju."""

    def test_basic_read(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text(
            "https://coursera.org/learn/a\n"
            "https://coursera.org/learn/b\n"
        )
        urls = read_urls(f)
        assert len(urls) == 2

    def test_deduplication(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text(
            "https://coursera.org/learn/a\n"
            "https://coursera.org/learn/a\n"
            "https://coursera.org/learn/b\n"
        )
        urls = read_urls(f)
        assert len(urls) == 2

    def test_skip_comments_and_empty(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text(
            "# komentar\n"
            "\n"
            "https://coursera.org/learn/a\n"
            "   \n"
            "# još komentar\n"
        )
        urls = read_urls(f)
        assert len(urls) == 1

    def test_strip_whitespace(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("  https://coursera.org/learn/a  \n")
        urls = read_urls(f)
        assert urls[0] == "https://coursera.org/learn/a"


# ============================================================
# TESTIRANJE load_checkpoint
# ============================================================

class TestLoadCheckpoint:
    """Testovi za load_checkpoint funkciju."""

    def test_nonexistent_file(self, tmp_path):
        result = load_checkpoint(tmp_path / "nema.csv")
        assert result == {}

    def test_valid_csv(self, tmp_path):
        f = tmp_path / "cp.csv"
        f.write_text(
            "url,status,slug,name\n"
            "https://coursera.org/learn/a,OK,a,Course A\n"
        )
        result = load_checkpoint(f)
        assert "https://coursera.org/learn/a" in result
        assert result["https://coursera.org/learn/a"]["status"] == "OK"


# ============================================================
# TESTIRANJE save_checkpoint_safe
# ============================================================

class TestSaveCheckpointSafe:
    """Testovi za save_checkpoint_safe funkciju."""

    def test_basic_save(self, tmp_path):
        f = tmp_path / "cp.csv"
        records = [
            {"url": "https://coursera.org/learn/a", "status": "OK", "slug": "a"}
        ]
        assert save_checkpoint_safe(f, records) is True
        assert f.exists()

    def test_empty_records(self, tmp_path):
        f = tmp_path / "cp.csv"
        assert save_checkpoint_safe(f, []) is True


# ============================================================
# TESTIRANJE create_session
# ============================================================

class TestCreateSession:
    """Testovi za create_session funkciju."""

    def test_session_created(self):
        session = create_session()
        assert session is not None
        session.close()

    def test_user_agent_set(self):
        session = create_session()
        assert session.headers["User-Agent"] == Config.USER_AGENT
        session.close()

    def test_retries_set(self):
        session = create_session(retries=5)
        # Proveri da je adapter montiran
        assert "https://" in session.adapters
        session.close()


# ============================================================
# TESTIRANJE export_results
# ============================================================

class TestExportResults:
    """Testovi za export_results funkciju."""

    def test_empty_records(self, tmp_path):
        result = export_results([], "test")
        assert result is None

    def test_csv_export(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        records = [
            {"url": "https://coursera.org/learn/a", "status": "OK", "name": "Course A"}
        ]
        result = export_results(records, "test", "csv")
        assert result is not None
        assert result.suffix == ".csv"
        assert result.exists()

    def test_excel_export(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        records = [
            {"url": "https://coursera.org/learn/a", "status": "OK", "name": "Course A"},
            {"url": "https://coursera.org/learn/b", "status": "TIMEOUT", "name": ""},
        ]
        result = export_results(records, "test", "excel")
        assert result is not None
        assert result.suffix == ".xlsx"
        assert result.exists()


# ============================================================
# TESTIRANJE fetch_course (mock)
# ============================================================

class TestFetchCourse:
    """Testovi za fetch_course funkciju."""

    def test_empty_slug(self):
        course, status = fetch_course(MagicMock(), "")
        assert course is None
        assert status == FetchStatus.EMPTY_SLUG

    def test_successful_response(self):
        session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "elements": [
                {
                    "name": "Test Course",
                    "slug": "test-course",
                    "description": "Test desc",
                    "workload": "4 hours",
                    "primaryLanguages": ["en"],
                }
            ]
        }
        session.get.return_value = mock_response

        course, status = fetch_course(session, "test-course")
        assert status == FetchStatus.OK
        assert course["name"] == "Test Course"
        assert course["slug"] == "test-course"

    def test_timeout(self):
        import requests
        session = MagicMock()
        session.get.side_effect = requests.Timeout()
        course, status = fetch_course(session, "slug")
        assert course is None
        assert status == FetchStatus.TIMEOUT

    def test_connection_error(self):
        import requests
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError()
        course, status = fetch_course(session, "slug")
        assert course is None
        assert status == FetchStatus.CONNECTION_ERROR

    def test_course_not_found(self):
        session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"elements": []}
        session.get.return_value = mock_response

        course, status = fetch_course(session, "nonexistent")
        assert course is None
        assert status == FetchStatus.COURSE_NOT_FOUND
