"""Downloads a tender's documents into TARGET_DIRECTORY/<tcno>/ and unpacks zip files, including nested ones."""
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests

from ext import TENDERTIGER_URL, extract_zip_recursive, tendertiger_checked, tendertiger_session
from utils.files import filename_from_response

log = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024


class DownloadError(Exception):
    """The document could not be downloaded or unpacked."""


def download_tender_documents(tender, tcno_folder, timeout=120, max_bytes=500 * 1024 * 1024,
                              on_progress=None):
    """
    Downloads all documents of tender (a raw tender from the tender list) from TenderTiger and leaves
    them in tcno_folder, which is emptied first so a re-run never mixes old and new documents. A zip
    (recognised by its content, not its name) is extracted with extract_zip_recursive, so nested zips
    are unpacked as well.

    Returns the paths of all files in tcno_folder. Raises DownloadError on any failure, and RuntimeError
    when the TenderTiger login fails.
    on_progress, if given, is called with a short status message before each step.
    """
    report = on_progress or (lambda message: None)
    tcno_folder = Path(tcno_folder)
    if tcno_folder.exists():
        shutil.rmtree(tcno_folder)
    tcno_folder.mkdir(parents=True)

    # Download beside the tcno folder, not into it: a zip holding a file with its own name
    # would otherwise be overwritten while it is still being read
    with tempfile.TemporaryDirectory(dir=tcno_folder.parent, ignore_cleanup_errors=True) as staging:
        report('Downloading documents')
        downloaded = _fetch(tender, Path(staging), timeout, max_bytes)
        if zipfile.is_zipfile(downloaded):
            report('Extracting ZIP files')
            try:
                extract_zip_recursive(downloaded, tcno_folder)
            except Exception as exc:  # BadZipFile, encrypted entries, unsupported compression, disk errors
                raise DownloadError(f'Could not extract {downloaded.name}: {exc}') from exc
        else:
            shutil.move(downloaded, tcno_folder / downloaded.name)

    files = sorted(path for path in tcno_folder.rglob('*') if path.is_file())
    log.info('%d file(s) ready in %s', len(files), tcno_folder)
    return files


def _open_documents(tender, timeout):
    """
    Asks TenderTiger for the tender's documents as one zip, the way the tender page's Download All button does,
    and returns the streaming response. Raises DownloadError when TenderTiger has none, and requests errors.
    """
    url = f'{TENDERTIGER_URL}/TenderDetail/DownloadAllFiles'
    params = {'hdntenderprocId': tender['tenderprocid'], 'hdnfreetender': '0', 'hdnwhatsapp': '0'}
    for refresh in (False, True):
        session = tendertiger_session(refresh)
        # DownloadAllFiles only finds a tender's documents once its detail page has been opened in the session
        tendertiger_checked(session.get(f'{TENDERTIGER_URL}/TenderDetail/Tenderinformation', timeout=timeout, params={
            'tenderNo': tender['enctenderprocid'], 'tcNo': tender['enctcno'], 'tendertype': 'Live',
        })).raise_for_status()
        check = tendertiger_checked(
            session.get(url, params={**params, 'hdnTcNo': tender['tcno'], 'mode': ''}, timeout=timeout))
        check.raise_for_status()
        status = check.json()
        if status.get('isLogin') != 0:
            break
        log.info('The TenderTiger session has ended: logging in again')
    else:
        raise DownloadError('TenderTiger did not accept the login for the download')
    if status.get('successCode') != 1:
        raise DownloadError(f'TenderTiger has no documents for this tender: {check.text[:200]}')
    # The page then fetches the zip with the TCNO left as "undefined", so this does the same
    return session.get(url, params={**params, 'hdnTcNo': 'undefined', 'mode': 'download'},
                       stream=True, timeout=timeout)


def _fetch(tender, folder, timeout, max_bytes):
    """Streams the tender's documents into folder and returns the saved file's path. Raises DownloadError."""
    try:
        with _open_documents(tender, timeout) as response:
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            # Expired or invalid links come back as a 200 page/message rather than an error status
            if content_type in ('text/html', 'application/json'):
                raise DownloadError(f'Expected a document but got {content_type}: {response.text[:200]}')

            path = folder / filename_from_response(response)
            size = 0
            with open(path, 'wb') as handle:
                for chunk in response.iter_content(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise DownloadError(f'Download is larger than {max_bytes // (1024 * 1024)} MB')
                    handle.write(chunk)
    except requests.RequestException as exc:
        raise DownloadError(f'Download failed: {exc}') from exc

    if size == 0:
        raise DownloadError('The server returned an empty file')
    log.info('Downloaded %s (%d bytes)', path.name, size)
    return path
