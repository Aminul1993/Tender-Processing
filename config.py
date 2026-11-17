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
    # The PostgreSQL database the processing job, every tender's last result and deep analysis, the company profile,
    # the team's shortlisted / dropped decisions, the watchlist's tenders and the cache of file texts, page OCR and
    # chunk vectors are kept in
    # (services/database.py), shared with worker processes: postgresql://<user>:<password>@<host>:<port>/<database>.
    # The database must exist; the tables are made on first use, in DATABASE_SCHEMA (so one database can serve
    # several installs).
    DATABASE_URL = os.getenv('DATABASE_URL') or None
    DATABASE_SCHEMA = os.getenv('DATABASE_SCHEMA', 'public')

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
    # Changing either needs a new PINECONE_INDEX_NAME: each tender is indexed into it when next checked.
    PINECONE_EMBED_MODEL = os.getenv('PINECONE_EMBED_MODEL', 'llama-text-embed-v2')
    PINECONE_EMBED_DIMENSION = int(os.getenv('PINECONE_EMBED_DIMENSION', '1024'))
    # The metric of an index made by the app. dotproduct scores the dense vectors as cosine does and also takes
    # sparse values, for hybrid retrieval (RETRIEVAL_HYBRID_ALPHA); an index made with cosine is searched dense only.
    PINECONE_METRIC = os.getenv('PINECONE_METRIC', 'dotproduct')
    # Vectors per upsert request; 50 x 1024 floats with their text stays well under Pinecone's 2 MB request limit
    PINECONE_UPSERT_BATCH = int(os.getenv('PINECONE_UPSERT_BATCH', '50'))
    # Characters per chunk, and how many of them repeat the end of the chunk before
    CHUNK_SIZE = int(os.getenv('CHUNK_SIZE', '1600'))
    CHUNK_OVERLAP = int(os.getenv('CHUNK_OVERLAP', '200'))
    # Chunks each retrieval query returns, and the most text (characters) sent to the LLM for one check
    RETRIEVAL_TOP_K = int(os.getenv('RETRIEVAL_TOP_K', '8'))
    RETRIEVAL_MAX_CHARS = int(os.getenv('RETRIEVAL_MAX_CHARS', '60000'))
    # Adaptive retrieval: a query keeps its matches only down to this share of its best match's score, and none
    # scoring under RETRIEVAL_MIN_SCORE (cosine), two at least whatever they score; 0 turns either off. On 35 indexed
    # tenders, 0.85 and 0.2 sent 24% less text than the top RETRIEVAL_TOP_K of every query (8 and 60,000 characters).
    RETRIEVAL_SCORE_FLOOR = float(os.getenv('RETRIEVAL_SCORE_FLOOR', '0.85'))
    RETRIEVAL_MIN_SCORE = float(os.getenv('RETRIEVAL_MIN_SCORE', '0.2'))
    # Hybrid retrieval: the weight of the dense score, the sparse one (exact words, clause numbers, rupee figures)
    # weighing the rest; 1 is dense alone. Needs an index with the dotproduct metric (PINECONE_METRIC).
    RETRIEVAL_HYBRID_ALPHA = float(os.getenv('RETRIEVAL_HYBRID_ALPHA', '0.75'))
    # Hierarchical retrieval: the most of RETRIEVAL_MAX_CHARS the qualification section, taken whole, may fill; 0
    # takes no section whole
    RETRIEVAL_SECTION_SHARE = float(os.getenv('RETRIEVAL_SECTION_SHARE', '0.5'))
    # Files read, and chunk batches embedded and upserted, at the same time
    INGEST_WORKERS = int(os.getenv('INGEST_WORKERS', '4'))

    # Tenders the web app processes at the same time itself; 0 leaves them all to worker processes (worker.py)
    JOB_WORKERS = int(os.getenv('JOB_WORKERS', '1'))

    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    LOG_DIRECTORY = Path(os.getenv('LOG_DIRECTORY', BASE_DIR / 'logs')).resolve()
