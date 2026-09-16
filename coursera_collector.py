import argparse
import logging
import signal
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse, parse_qs

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


class Config:
    HTTP_TIMEOUT: int = 15
    CHECKPOINT_TIMEOUT: int = 5
    DEFAULT_DELAY: float = 0.5
    DEFAULT_RETRIES: int = 3
    MAX_FILE_SIZE: int = 10_000_000
    MAX_URLS: int = 10_000
    API_URL: str = "https://api.coursera.org/api/courses.v1"
    USER_AGENT: str = "CourseraInventoryCollector/3.0"


class FetchStatus(Enum):
    OK = "OK"
    EMPTY_SLUG = "EMPTY_SLUG"
    TIMEOUT = "TIMEOUT"
    HTTP_404 = "HTTP_404"
    HTTP_429_RATE_LIMIT = "HTTP_429_RATE_LIMIT"
    HTTP_500 = "HTTP_500"
    HTTP_502 = "HTTP_502"
    HTTP_503 = "HTTP_503"
    HTTP_504 = "HTTP_504"
    COURSE_NOT_FOUND = "COURSE_NOT_FOUND"
    REQUEST_ERROR = "REQUEST_ERROR"
    INVALID_JSON = "INVALID_JSON"
    CONNECTION_ERROR = "CONNECTION_ERROR"

    def __str__(self) -> str:
        return self.value


def setup_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler = logging.FileHandler(
        output_dir / "coursera_collector.log",
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    error_handler = logging.FileHandler(
        output_dir / "coursera_errors.log",
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    return logger


logger: Optional[logging.Logger] = None


def create_session(retries: int = Config.DEFAULT_RETRIES) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": Config.USER_AGENT})
    return session


def extract_slug(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if "/" not in url:
        return url
    try:
        parsed = urlparse(url)
        path_parts = [
            part.strip()
            for part in parsed.path.split("/")
            if part.strip()
        ]
        if not path_parts:
            return ""
        query = parse_qs(parsed.query)
        if "slug" in query and query["slug"]:
            return query["slug"][0].strip()
        return path_parts[-1]
    except Exception as e:
        logger.debug("Greška pri ekstrakciji sluga iz %s: %s", url, e)
        return ""


def fetch_course(
    session: requests.Session,
    slug: str,
    timeout: int = Config.HTTP_TIMEOUT,
    attempt: int = 0
) -> Tuple[Optional[Dict[str, Any]], FetchStatus]:
    if not slug:
        return None, FetchStatus.EMPTY_SLUG
    params = {"q": "slug", "slug": slug}
    try:
        response = session.get(
            Config.API_URL,
            params=params,
            timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        return None, FetchStatus.TIMEOUT
    except requests.ConnectionError as e:
        return None, FetchStatus.CONNECTION_ERROR
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response else None
        if status_code == 404:
            return None, FetchStatus.HTTP_404
        if status_code == 429:
            wait_time = min(60, 2 ** attempt)
            time.sleep(wait_time)
            if attempt < 3:
                return fetch_course(session, slug, timeout, attempt + 1)
            return None, FetchStatus.HTTP_429_RATE_LIMIT
        if status_code == 500:
            return None, FetchStatus.HTTP_500
        if status_code == 502:
            return None, FetchStatus.HTTP_502
        if status_code == 503:
            return None, FetchStatus.HTTP_503
        if status_code == 504:
            return None, FetchStatus.HTTP_504
        return None, FetchStatus.REQUEST_ERROR
    except requests.RequestException:
        return None, FetchStatus.REQUEST_ERROR
    except ValueError:
        return None, FetchStatus.INVALID_JSON
    try:
        elements = data.get("elements", [])
        if not elements:
            return None, FetchStatus.COURSE_NOT_FOUND
        course = elements[0]
        languages = course.get("primaryLanguages", [])
        if isinstance(languages, list):
            languages = ", ".join(str(lang) for lang in languages)
        result = {
            "name": course.get("name", ""),
            "slug": course.get("slug", slug),
            "description": course.get("description", ""),
            "workload": course.get("workload", ""),
            "languages": languages,
        }
        return result, FetchStatus.OK
    except Exception as e:
        return None, FetchStatus.INVALID_JSON


def validate_input_file(path: Path) -> bool:
    if not path.exists():
        logger.error("Fajl '%s' ne postoji.", path)
        return False
    size = path.stat().st_size
    if size == 0:
        logger.warning("Ulazni fajl je prazan.")
        return False
    if size > Config.MAX_FILE_SIZE:
        logger.error("Fajl je prevelik.")
        return False
    return True


def read_urls(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    urls = []
    seen = set()
    for line in lines:
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
            if len(urls) >= Config.MAX_URLS:
                break
    return urls


def load_checkpoint(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        df = pd.read_csv(path, encoding="utf-8")
        checkpoint = {}
        for _, row in df.iterrows():
            checkpoint[row["url"]] = {
                "status": row.get("status", ""),
                "slug": row.get("slug", ""),
                "name": row.get("name", ""),
                "description": row.get("description", ""),
                "workload": row.get("workload", ""),
                "languages": row.get("languages", ""),
            }
        return checkpoint
    except Exception as e:
        return {}


def save_checkpoint_safe(
    path: Path,
    records: list[Dict[str, Any]],
    retries: int = 3
) -> bool:
    for attempt in range(retries):
        try:
            df = pd.DataFrame(records)
            df.to_csv(path, index=False, encoding="utf-8")
            return True
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(Config.CHECKPOINT_TIMEOUT)
            else:
                return False
        except Exception:
            return False
    return False
def create_signal_handler(
    checkpoint_path: Path,
    records: list[Dict[str, Any]]
) -> None:
    def signal_handler(sig, frame):
        logger.info("🛑 Prekid programa...")
        save_checkpoint_safe(checkpoint_path, records)
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)


def export_results(
    records: list[Dict[str, Any]],
    output_name: str,
    format_type: str = "excel"
) -> Optional[Path]:
    if not records:
        return None
    df = pd.DataFrame(records)
    if format_type == "excel":
        return _export_excel(df, output_name)
    else:
        return _export_csv(df, output_name)


def _export_excel(df: pd.DataFrame, output_name: str) -> Optional[Path]:
    output_file = Path(f"{output_name}.xlsx")
    counter = 1
    while output_file.exists():
        output_file = Path(f"{output_name}_{counter}.xlsx")
        counter += 1
    try:
        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="All Results", index=False)
            df[df["status"] == FetchStatus.OK.value].to_excel(
                writer, sheet_name="Courses", index=False
            )
            df[df["status"] != FetchStatus.OK.value].to_excel(
                writer, sheet_name="Errors", index=False
            )
        return output_file
    except PermissionError:
        return None
    except Exception:
        return None


def _export_csv(df: pd.DataFrame, output_name: str) -> Optional[Path]:
    output_file = Path(f"{output_name}.csv")
    counter = 1
    while output_file.exists():
        output_file = Path(f"{output_name}_{counter}.csv")
        counter += 1
    try:
        df.to_csv(output_file, index=False, encoding="utf-8")
        return output_file
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Coursera Inventory Collector V3.0"
    )
    parser.add_argument("--input", default="urls.txt")
    parser.add_argument("--output", default="coursera_inventory")
    parser.add_argument("--format", choices=["excel", "csv"], default="excel")
    parser.add_argument("--delay", type=float, default=Config.DEFAULT_DELAY)
    parser.add_argument("--retries", type=int, default=Config.DEFAULT_RETRIES)
    parser.add_argument("--timeout", type=float, default=Config.HTTP_TIMEOUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    global logger
    output_dir = Path(args.output).parent if "/" in args.output else Path(".")
    logger = setup_logging(output_dir)

    input_path = Path(args.input)
    if not validate_input_file(input_path):
        return

    urls = read_urls(input_path)
    if not urls:
        return

    checkpoint_path = Path(f"{args.output}_checkpoint.csv")
    checkpoint = load_checkpoint(checkpoint_path) if args.resume else {}

    records = []
    create_signal_handler(checkpoint_path, records)

    session = None
    successful = 0
    failed = 0
    skipped = 0

    try:
        session = create_session(retries=args.retries)
        total = len(urls)

        for url in tqdm(urls, desc="Obrada", unit="url", colour="green"):
            slug = extract_slug(url)

            if (
                args.resume
                and not args.force
                and url in checkpoint
                and checkpoint[url].get("status") == FetchStatus.OK.value
            ):
                old = checkpoint[url]
                records.append({
                    "url": url,
                    "slug": old.get("slug", slug),
                    "name": old.get("name", ""),
                    "description": old.get("description", ""),
                    "workload": old.get("workload", ""),
                    "languages": old.get("languages", ""),
                    "status": FetchStatus.OK.value,
                })
                skipped += 1
                continue

            course, status = fetch_course(session, slug, timeout=args.timeout)

            if course:
                records.append({"url": url, **course, "status": status.value})
                successful += 1
            else:
                records.append({
                    "url": url,
                    "slug": slug,
                    "name": "",
                    "description": "",
                    "workload": "",
                    "languages": "",
                    "status": status.value
                })
                failed += 1

            save_checkpoint_safe(checkpoint_path, records)

            if url != urls[-1]:
                time.sleep(max(0, args.delay))

    except KeyboardInterrupt:
        save_checkpoint_safe(checkpoint_path, records)
    except Exception as e:
        logger.exception("Greška: %s", e)
        save_checkpoint_safe(checkpoint_path, records)
    finally:
        if session:
            session.close()

    if not records:
        return

    output_file = export_results(records, args.output, args.format)

    logger.info("Ukupno: %d | Uspješno: %d | Greške: %d | Preskočeno: %d",
                len(urls), successful, failed, skipped)


if __name__ == "__main__":
    main()