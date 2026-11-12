"""Logging configuration shared by the whole application."""
import logging
from logging.handlers import RotatingFileHandler

LOG_FORMAT = '%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s'


class _SkipProgressPolling(logging.Filter):
    """The page polls /api/progress every second or two; logging each poll would bury everything else."""

    def filter(self, record):
        return '/api/progress' not in record.getMessage()


def configure_logging(level, log_directory, filename='app.log'):
    """
    Sends log records to the console and to log_directory/filename (rotated at 5 MB, 3 backups kept). Each process
    needs a file of its own: a file shared by processes cannot be rotated on Windows.
    """
    log_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_directory / filename, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace rather than add, so creating the app twice (e.g. in tests) does not print every line twice
    for handler in root.handlers:
        handler.close()
    root.handlers = [console, file_handler]

    # Send uvicorn's server and access logs through the handlers above, also when started with the uvicorn CLI
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, _SkipProgressPolling) for f in access.filters):
        access.addFilter(_SkipProgressPolling())
    # httpx logs every LLM and Pinecone request at INFO, and the Pinecone SDK every query and fetch: dozens of lines
    # per tender. services.pinecone_service logs what matters (indexes built, retrievals, retries)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('pinecone').setLevel(logging.WARNING)
