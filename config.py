"""Application settings. Each value can be overridden by an environment variable (or .env) of the same name."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    """Default configuration, loaded into app.config by create_app()."""

    # Downloaded tender documents are stored in TARGET_DIRECTORY/<tcno>/
    TARGET_DIRECTORY = Path(os.getenv('TARGET_DIRECTORY', BASE_DIR / 'downloads')).resolve()
    # Seconds to wait for the document server to connect, and between received chunks
    DOWNLOAD_TIMEOUT = int(os.getenv('DOWNLOAD_TIMEOUT', '120'))
    # Bigger downloads are aborted, so a bad link cannot fill the disk
    MAX_DOWNLOAD_MB = int(os.getenv('MAX_DOWNLOAD_MB', '500'))
    # A tender file whose text is longer is not sent to the LLM (300,000 characters is about 100k tokens) and is
    # listed as not checked
    MAX_FILE_CHARS = int(os.getenv('MAX_FILE_CHARS', '300000'))
    # The team's shortlisted / dropped decisions; None means TARGET_DIRECTORY/decisions.json
    DECISIONS_FILE = os.getenv('DECISIONS_FILE')
    # The company profile (markdown) every tender is checked against; edited on the page
    COMPANY_DETAILS_FILE = Path(os.getenv('COMPANY_DETAILS_FILE', BASE_DIR / 'company_details.md')).resolve()

    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    LOG_DIRECTORY = Path(os.getenv('LOG_DIRECTORY', BASE_DIR / 'logs')).resolve()
