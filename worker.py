"""
A worker process: takes the tenders queued on the page from the job in Redis and processes them, beside the web app
and any other workers. Needs REDIS_URL, and the same .env and TARGET_DIRECTORY as the web app (the same machine, or
a folder shared with it): the documents and results are saved there.

    python worker.py               # one tender at a time
    python worker.py --threads 3   # three at a time

Set JOB_WORKERS=0 for the web app to leave all the processing to workers. Ctrl+C stops taking tenders; those in
progress finish first (a second Ctrl+C quits at once: their tenders are then failed after a minute or so). Logs go
to the console and to LOG_DIRECTORY/worker-<pid>.log.
"""
import argparse
import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()

from config import Config  # noqa: E402
from services.processing_service import TenderProcessor  # noqa: E402
from utils.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger('worker')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--threads', type=int, default=1, help='tenders processed at the same time (default 1)')
    args = parser.parse_args(argv)
    configure_logging(Config.LOG_LEVEL, Config.LOG_DIRECTORY, f'worker-{os.getpid()}.log')
    if not Config.REDIS_URL:
        parser.error('REDIS_URL is not set: a worker shares the job with the web app through Redis')
    Config.TARGET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    # The results are the web app's to load from the tender folders; a worker writes them as it finishes tenders
    processor = TenderProcessor.from_config(Config, load_results=False, job_workers=0)
    stop = threading.Event()
    threads = processor.work(max(1, args.threads), stop)
    log.info('Worker %s: processing queued tenders, %d at a time', processor.worker_name, len(threads))
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(1)
    except KeyboardInterrupt:
        stop.set()
        log.info('Stopping: the tenders in progress finish first (Ctrl+C again to quit now)')
        for thread in threads:
            thread.join()
    log.info('Worker %s stopped', processor.worker_name)


if __name__ == '__main__':
    main()
