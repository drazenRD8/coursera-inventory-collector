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


# ============================================================
# KONSTANTE
# ============================================================

class Config:
    """Konfiguracija konstanti za aplikaciju."""
    HTTP_TIMEOUT: int = 15
    CHECKPOINT_TIMEOUT: int = 5
    DEFAULT_DELAY: float = 0.5
    DEFAULT_RETRIES: int = 3
    MAX_FILE_SIZE: int = 10_000_000  # 10MB
    MAX_URLS: int = 10_000
    API_URL: str = "https://api.coursera.org/api/courses.v1"
    USER_AGENT: str = "CourseraInventoryCollector/3.0"


# ============================================================
# ENUM - STATUS KODOVI
# ============================================================

class FetchStatus(Enum):
    """Enumeracija svih mogućih status kodova."""
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


# ============================================================
# LOGGING
# ============================================================

def setup_logging(output_dir: Path) -> logging.Logger:
    """Postavlja logging sa konsoli i fajlovima."""
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


# ============================================================
# API
# ============================================================

def create_session(retries: int = Config.DEFAULT_RETRIES) -> requests.Session:
    """Kreira HTTP Session sa automatskim retry mehanizmom."""
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

    session.headers.update({
        "User-Agent": Config.USER_AGENT
    })

    return session


# ============================================================
# SLUG
# ============================================================

def extract_slug(url: str) -> str:
    """Robustno izvlači slug iz Coursera URL-a."""
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


# ============================================================
# FETCH COURSE
# ============================================================

def fetch_course(
    session: requests.Session,
    slug: str,
    timeout: int = Config.HTTP_TIMEOUT,
    attempt: int = 0
) -> Tuple[Optional[Dict[str, Any]], FetchStatus]:
    """Preuzima jedan kurs sa Coursera API-ja sa adaptive backoff-om za 429."""
    if not slug:
        return None, FetchStatus.EMPTY_SLUG

    params = {
        "q": "slug",
        "slug": slug
    }

    try:
        response = session.get(
            Config.API_URL,
            params=params,
            timeout=timeout
        )

        response.raise_for_status()
        data = response.json()

    except requests.Timeout:
        logger.debug("Timeout pri preuzimanju %s", slug)
        return None, FetchStatus.TIMEOUT

    except requests.ConnectionError as e:
        logger.debug("Greška konekcije za %s: %s", slug, e)
        return None, FetchStatus.CONNECTION_ERROR

    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response else None

        if status_code == 404:
            return None, FetchStatus.HTTP_404

        if status_code == 429:
            wait_time = min(60, 2 ** attempt)
            logger.warning(
                "Rate limit za %s. Čekam %d sekundi... (pokušaj %d)",
                slug, wait_time, attempt + 1
            )
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

        logger.debug("HTTP greška %s za %s: %s", status_code, slug, e)
        return None, FetchStatus.REQUEST_ERROR

    except requests.RequestException as e:
        logger.debug("Request greška za %s: %s", slug, e)
        return None, FetchStatus.REQUEST_ERROR

    except ValueError:
        logger.debug("Invalidan JSON odgovor za %s", slug)
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
        logger.debug("Greška pri parsiranju odgovora za %s: %s", slug, e)
        return None, FetchStatus.INVALID_JSON


# ============================================================
# INPUT
# ============================================================

def validate_input_file(path: Path) -> bool:
    """Validira ulazni fajl prije obrade."""
    if not path.exists():
        logger.error("Fajl '%s' ne postoji.", path)
        return False

    size = path.stat().st_size

    if size == 0:
        logger.warning("Ulazni fajl je prazan.")
        return False

    if size > Config.MAX_FILE_SIZE:
        logger.error(
            "Fajl je prevelik (%d bajta). Maksimalno: %d bajta",
            size, Config.MAX_FILE_SIZE
        )
        return False

    return True


def read_urls(path: Path) -> list[str]:
    """Čita URL-ove i uklanja duplikate."""
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
                logger.warning(
                    "Dostignut limit od %d URL-ova.",
                    Config.MAX_URLS
                )
                break

    return urls


# ============================================================
# CHECKPOINT
# ============================================================

def load_checkpoint(path: Path) -> Dict[str, Dict[str, Any]]:
    """Učitava prethodno obrađene URL-ove."""
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

        logger.info("Učitano %d prethodno obrađenih zapisa.", len(checkpoint))
        return checkpoint

    except Exception as e:
        logger.warning("Ne mogu učitati checkpoint: %s", e)
        return {}


def save_checkpoint_safe(
    path: Path,
    records: list[Dict[str, Any]],
    retries: int = 3
) -> bool:
    """Čuva checkpoint sa retry logikom u slučaju da je fajl otvoren."""
    for attempt in range(retries):
        try:
            df = pd.DataFrame(records)
            df.to_csv(path, index=False, encoding="utf-8")
            return True

        except PermissionError:
            if attempt < retries - 1:
                logger.warning(
                    "Checkpoint fajl je otvoren. Pokušaj %d/%d za %ds...",
                    attempt + 1, retries, Config.CHECKPOINT_TIMEOUT
                )
                time.sleep(Config.CHECKPOINT_TIMEOUT)
            else:
                logger.error(
                    "Ne mogu sačuvati checkpoint nakon %d pokušaja. "
                    "Molim zatvori fajl ako je otvoren.",
                    retries
                )
                return False

        except Exception as e:
            logger.error("Greška pri čuvanju checkpoint-a: %s", e)
            return False

    return False


# ============================================================
# GRACEFUL SHUTDOWN
# ============================================================

def create_signal_handler(
    checkpoint_path: Path,
    records: list[Dict[str, Any]]
) -> None:
    """Kreira handler za SIGINT (Ctrl+C) koji čuva progress prije izlaza."""
    def signal_handler(sig, frame):
        logger.info("\n" + "=" * 60)
        logger.info("🛑 Prekid programa (Ctrl+C)...")
        logger.info("Čuvam checkpoint prije izlaza...")

        if save_checkpoint_safe(checkpoint_path, records):
            logger.info("✓ Checkpoint uspješno sačuvan.")
            logger.info("Možeš nastaviti sa --resume flagom.")
        else:
            logger.error("✗ Checkpoint nije sačuvan!")

        logger.info("=" * 60)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)


# ============================================================
# EXPORT
# ============================================================

def export_results(
    records: list[Dict[str, Any]],
    output_name: str,
    format_type: str = "excel"
) -> Optional[Path]:
    """Izvozi rezultate u Excel ili CSV sa automatskim brojanjem fajlova."""
    if not records:
        logger.warning("Nema rezultata za izvoz.")
        return None

    df = pd.DataFrame(records)

    if format_type == "excel":
        return _export_excel(df, output_name)
    else:
        return _export_csv(df, output_name)


def _export_excel(df: pd.DataFrame, output_name: str) -> Optional[Path]:
    """Izvozi u Excel sa tri lista."""
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

        logger.info("✓ Excel sačuvan: %s", output_file)
        return output_file

    except PermissionError:
        logger.error("Excel fajl je otvoren. Zatvori ga i pokušaj ponovo.")
        return None

    except Exception as e:
        logger.error("Greška pri izvozenju u Excel: %s", e)
        return None


def _export_csv(df: pd.DataFrame, output_name: str) -> Optional[Path]:
    """Izvozi u CSV."""
    output_file = Path(f"{output_name}.csv")
    counter = 1

    while output_file.exists():
        output_file = Path(f"{output_name}_{counter}.csv")
        counter += 1

    try:
        df.to_csv(output_file, index=False, encoding="utf-8")
        logger.info("✓ CSV sačuvan: %s", output_file)
        return output_file

    except Exception as e:
        logger.error("Greška pri izvozenju u CSV: %s", e)
        return None


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Coursera Inventory Collector V3.0 "
            "- napredna verzija sa retry logikom i graceful shutdown-om"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Primjeri:
  # Osnovna obrada
  python script.py --input urls.txt --output data

  # Nastavi iz checkpointa
  python script.py --input urls.txt --output data --resume

  # Ponovo obradi sve i spremi kao CSV
  python script.py --input urls.txt --output data --format csv --force

  # Sporija obrada sa više retry-ja (za loše konekcije)
  python script.py --input urls.txt --delay 1.0 --retries 5
        """
    )

    parser.add_argument(
        "--input",
        default="urls.txt",
        help="Ulazni fajl sa URL-ovima (default: urls.txt)"
    )

    parser.add_argument(
        "--output",
        default="coursera_inventory",
        help="Naziv izlaznog fajla bez ekstenzije (default: coursera_inventory)"
    )

    parser.add_argument(
        "--format",
        choices=["excel", "csv"],
        default="excel",
        help="Izlazni format (default: excel)"
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=Config.DEFAULT_DELAY,
        help=f"Pauza između zahtjeva u sekundama (default: {Config.DEFAULT_DELAY})"
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=Config.DEFAULT_RETRIES,
        help=f"Broj automatskih pokušaja (default: {Config.DEFAULT_RETRIES})"
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=Config.HTTP_TIMEOUT,
        help=f"HTTP timeout u sekundama (default: {Config.HTTP_TIMEOUT})"
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Nastavi iz prethodnog checkpointa"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Ponovo obradi sve URL-ove, čak i već uspješne"
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # LOGGING
    # --------------------------------------------------------

    global logger
    output_dir = Path(args.output).parent if "/" in args.output else Path(".")
    logger = setup_logging(output_dir)

    logger.info("=" * 60)
    logger.info("🚀 Coursera Inventory Collector V3.0")
    logger.info("=" * 60)

    # --------------------------------------------------------
    # VALIDACIJA ULAZA
    # --------------------------------------------------------

    input_path = Path(args.input)

    if not validate_input_file(input_path):
        return

    urls = read_urls(input_path)

    if not urls:
        logger.warning("Nema URL-ova za obradu nakon filtriranja.")
        return

    logger.info("✓ Pronađeno %d jedinstvenih URL-ova.", len(urls))

    # --------------------------------------------------------
    # CHECKPOINT
    # --------------------------------------------------------

    checkpoint_path = Path(f"{args.output}_checkpoint.csv")

    if args.resume:
        checkpoint = load_checkpoint(checkpoint_path)
    else:
        checkpoint = {}

    # --------------------------------------------------------
    # GRACEFUL SHUTDOWN
    # --------------------------------------------------------

    records = []
    create_signal_handler(checkpoint_path, records)

    # --------------------------------------------------------
    # SESSION
    # --------------------------------------------------------

    session = None
    successful = 0
    failed = 0
    skipped = 0

    try:
        session = create_session(retries=args.retries)
        logger.info("✓ HTTP sesija kreirana.")

        total = len(urls)

        logger.info("=" * 60)
        logger.info("🔄 Počinjem obradu...")
        logger.info("=" * 60)

        for url in tqdm(
            urls,
            desc="Obrada URL-ova",
            unit="url",
            colour="green"
        ):

            slug = extract_slug(url)

            # -------- Resume logika --------

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

            # -------- Fetch --------

            course, status = fetch_course(
                session,
                slug,
                timeout=args.timeout
            )

            # -------- Success --------

            if course:
                records.append({
                    "url": url,
                    **course,
                    "status": status.value
                })
                successful += 1

            # -------- Error --------

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

            # -------- Checkpoint (posle svakog URL-a) --------

            if not save_checkpoint_safe(checkpoint_path, records):
                logger.warning("Nastavljam bez checkpoint-a...")

            # -------- Rate limiting --------

            if url != urls[-1]:
                time.sleep(max(0, args.delay))

    except KeyboardInter