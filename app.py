"""Tender processing web app. Run `python app.py` and open http://127.0.0.1:5000."""
import logging
import os

from dotenv import load_dotenv

# Load .env before config.py reads the environment
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from config import BASE_DIR, Config  # noqa: E402
from ext import company_details  # noqa: E402
from routes.api import EXCEPTION_HANDLERS, api  # noqa: E402
from routes.pages import pages  # noqa: E402
from services.decision_service import DecisionStore  # noqa: E402
from services.deep_analysis_service import DeepAnalysisRunner  # noqa: E402
from services.processing_service import TenderProcessor  # noqa: E402
from utils.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger(__name__)


def create_app(config_object=Config, vector_store=None):
    """
    Application factory: sets up logging, the tenders' Pinecone namespaces (vector_store, a PineconeService, or one
    made from config_object), the tender processor, the decision store and the routers.
    """
    configure_logging(config_object.LOG_LEVEL, config_object.LOG_DIRECTORY)
    app = FastAPI(title='Tender Processing', exception_handlers=EXCEPTION_HANDLERS)

    config_object.TARGET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    job_workers = config_object.JOB_WORKERS
    if job_workers < 1 and not config_object.REDIS_URL:
        log.warning('JOB_WORKERS is 0 but REDIS_URL is not set, so no worker process can reach the job: the app '
                     'processes one tender at a time itself')
        job_workers = 1
    app.state.tender_processor = TenderProcessor.from_config(config_object, index=vector_store,
                                                             job_workers=job_workers)
    app.state.company_file = config_object.COMPANY_DETAILS_FILE
    app.state.default_filters_file = config_object.DEFAULT_FILTERS_FILE
    app.state.decisions = DecisionStore(
        config_object.DECISIONS_FILE or config_object.TARGET_DIRECTORY / 'decisions.json')
    app.state.deep_analyses = DeepAnalysisRunner(app.state.tender_processor, app.state.decisions,
                                                 config_object.COMPANY_DETAILS_FILE)
    app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')
    app.include_router(pages)
    app.include_router(api)

    if not os.getenv('TENDERTIGER_EMAIL') or not os.getenv('TENDERTIGER_PASSWORD'):
        log.warning('TENDERTIGER_EMAIL / TENDERTIGER_PASSWORD are not set: the tender list cannot be loaded')
    if not app.state.tender_processor.index.configured:
        log.warning('PINECONE_API_KEY is not set: tenders can be listed but every analysis will fail')
    if not os.getenv('GROQ_API_KEY'):
        log.warning('GROQ_API_KEY is not set: tenders can be listed but every analysis will fail')
    if not company_details(config_object.COMPANY_DETAILS_FILE):
        log.warning('The company profile %s is empty: add it with Edit profile on the page, or every analysis will fail',
                    config_object.COMPANY_DETAILS_FILE)
    log.info('Tender documents are saved under %s', config_object.TARGET_DIRECTORY)
    return app


if __name__ == '__main__':
    import uvicorn

    # One web process: the tender list, the deep analyses and the default in-process Redis live in its memory. Scale
    # the processing with worker processes instead (worker.py, with REDIS_URL). log_config=None keeps the logging set
    # up by create_app().
    uvicorn.run(create_app(), host='127.0.0.1', port=int(os.getenv('PORT', '5000')), log_config=None)
