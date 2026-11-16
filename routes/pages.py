"""HTML pages."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import BASE_DIR
from services.company_service import company_profile

pages = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=BASE_DIR / 'templates')


@pages.get('/', response_class=HTMLResponse)
def index(request: Request):
    """The tender list. The table is filled by static/js/app.js from /api/tenders."""
    company = company_profile(request.app.state.tender_processor.company_store.read())
    return templates.TemplateResponse(request, 'index.html', {'company': company})
