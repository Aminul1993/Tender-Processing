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
    # The most OCR a tender's scans may need before the team is asked: pages and images to OCR (files read before are
    # cached and need none), and what they could cost at most, in US dollars, at OCR_PAGE_COST each. Over either, the
    # tender stops before any OCR and waits for approval. 0 is no limit.
    OCR_BUDGET_PAGES = int(os.getenv('OCR_BUDGET_PAGES', '50'))
    OCR_BUDGET_USD = float(os.getenv('OCR_BUDGET_USD', '0.5'))
    # The team's shortlisted / dropped decisions; None means TARGET_DIRECTORY/decisions.json
    DECISIONS_FILE = os.getenv('DECISIONS_FILE')
    # The texts read from tender files (by SHA-256) and the vectors of their chunks, so an unchanged file is never
    # read or embedded twice (services/cache_service.py); SQLite. Delete it to read and embed everything again.
    # None means TARGET_DIRECTORY/cache.sqlite3
    CACHE_FILE = os.getenv('CACHE_FILE')
    # The company profile (markdown) every tender is checked against; edited on the page
    COMPANY_DETAILS_FILE = Path(os.getenv('COMPANY_DETAILS_FILE', BASE_DIR / 'company_details.md')).resolve()
    # The TenderTiger filters that Default filters on the page applies (JSON, edited by hand)
    DEFAULT_FILTERS_FILE = Path(os.getenv('DEFAULT_FILTERS_FILE', BASE_DIR / 'default_filters.json')).resolve()

    # Pinecone: every tender's documents are chunked, embedded and stored in a namespace of its own,
    # PINECONE_NAMESPACE_PREFIX + TCNO (tcno-12345678), in one shared index, PINECONE_INDEX_NAME (made on first use).
    # The key is read from the environment (.env) only.
    PINECONE_API_KEY = os.getenv('PINECONE_API_KEY')
    PINECONE_INDEX_NAME = os.getenv('PINECONE_INDEX_NAME', 'tender-processing')
    PINECONE_NAMESPACE_PREFIX = os.getenv('PINECONE_NAMESPACE_PREFIX', 'tcno-')
    # Where the index is created (serverless); the Starter plan allows only aws / us-east-1
    PINECONE_CLOUD = os.getenv('PINECONE_CLOUD', 'aws')
    PINECONE_REGION = os.getenv('PINECONE_REGION', 'us-east-1')
    # Pinecone-hosted embedding model and its dimension (llama-text-embed-v2: 384, 512, 768, 1024 or 2048).
    # Changing either needs every index rebuilt (scripts/migrate_to_pinecone.py --force).
    PINECONE_EMBED_MODEL = os.getenv('PINECONE_EMBED_MODEL', 'llama-text-embed-v2')
    PINECONE_EMBED_DIMENSION = int(os.getenv('PINECONE_EMBED_DIMENSION', '1024'))
    # Vectors per upsert request; 50 x 1024 floats with their text stays well under Pinecone's 2 MB request limit
    PINECONE_UPSERT_BATCH = int(os.getenv('PINECONE_UPSERT_BATCH', '50'))
    # Characters per chunk, and how many of them repeat the end of the chunk before
    CHUNK_SIZE = int(os.getenv('CHUNK_SIZE', '1600'))
    CHUNK_OVERLAP = int(os.getenv('CHUNK_OVERLAP', '200'))
    # Chunks each retrieval query returns, and the most text (characters) sent to the LLM for one check
    RETRIEVAL_TOP_K = int(os.getenv('RETRIEVAL_TOP_K', '8'))
    RETRIEVAL_MAX_CHARS = int(os.getenv('RETRIEVAL_MAX_CHARS', '60000'))
    # Files read, and chunk batches embedded and upserted, at the same time
    INGEST_WORKERS = int(os.getenv('INGEST_WORKERS', '4'))

    # Where the processing job and the results live, so worker processes (worker.py) can share the work: a Redis URL
    # such as redis://default:<password>@<host>:<port> (rediss:// for TLS). Unset, an in-process Redis is used: one
    # process, and the job (not the results, saved in the tender folders) is lost on restart.
    REDIS_URL = os.getenv('REDIS_URL') or None
    # Put before every key, so one Redis can serve several installs
    REDIS_PREFIX = os.getenv('REDIS_PREFIX', 'tender-processing:')
    # Tenders the web app processes at the same time itself; 0 leaves them all to worker processes (REDIS_URL only)
    JOB_WORKERS = int(os.getenv('JOB_WORKERS', '1'))

    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    LOG_DIRECTORY = Path(os.getenv('LOG_DIRECTORY', BASE_DIR / 'logs')).resolve()
