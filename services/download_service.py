"""Downloads a tender's documents into TARGET_DIRECTORY/<tcno>/ and unpacks zip files, including nested ones."""
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests

from utils.files import ZipTooLarge, extract_zip_recursive, filename_from_response
from utils.tendertiger import TENDERTIGER_URL, tendertiger_checked, tendertiger_session

log = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
# How many times the download limit (MAX_DOWNLOAD_MB) its zips may unpack to; more is a zip bomb, not documents
UNPACKED_FACTOR = 4


class DownloadError(Exception):
    """The document could not be downloaded or unpacked."""


def download_tender_documents(tender, tcno_folder, timeout=120, max_bytes=500 * 1024 * 1024,
                              on_progress=None):
    """
    Downloads all documents of tender (a raw tender from the tender list) from TenderTiger and puts them
    in tcno_folder in place of what it held, so a re-run never mixes old and new documents. The folder is
    replaced only once every document is downloaded and extracted: a download that fails leaves the last
    one's documents as they were. A zip (recognised by its content, not its name) is extracted with
    extract_zip_recursive, so nested zips are unpacked as well.

    Returns the paths of all files in tcno_folder. Raises DownloadError on any failure (also when a file
    in tcno_folder is open in another program, so it cannot be replaced), and RuntimeError when the
    TenderTiger login fails.
    on_progress, if given, is called with a short status message before each step.
    """
    report = on_progress or (lambda message: None)
    tcno_folder = Path(tcno_folder)
    tcno_folder.parent.mkdir(parents=True, exist_ok=True)

    # Downloaded and extracted in a staging folder beside the tcno folder (so on the same disk, to be moved in its
    # place), each in a folder of its own: a zip holding a file with its own name would otherwise be overwritten
    # while it is still being read
    with tempfile.TemporaryDirectory(dir=tcno_folder.parent, ignore_cleanup_errors=True) as staging:
        download, documents = Path(staging) / 'download', Path(staging) / 'documents'
        download.mkdir()
        documents.mkdir()
        report('Downloading documents')
        downloaded = _fetch(tender, download, timeout, max_bytes)
        if zipfile.is_zipfile(downloaded):
            report('Extracting ZIP files')
            try:
                # Documents compress little, so more than UNPACKED_FACTOR times the download is not documents
                extract_zip_recursive(downloaded, documents, max_bytes=UNPACKED_FACTOR * max_bytes)
            except ZipTooLarge as exc:
                raise DownloadError(str(exc)) from exc
            except Exception as exc:  # BadZipFile, encrypted entries, unsupported compression, disk errors
                raise DownloadError(f'Could not extract {downloaded.name}: {exc}') from exc
        else:
            shutil.move(downloaded, documents / downloaded.name)
        _replace(tcno_folder, documents, Path(staging) / 'replaced')

    files = sorted(path for path in tcno_folder.rglob('*') if path.is_file())
    log.info('%d file(s) ready in %s', len(files), tcno_folder)
    return files


def _replace(folder, documents, aside):
    """
    Puts the documents folder in folder's place. The old folder is moved aside first (into the staging folder, deleted
    with it), which on Windows fails, changing nothing, while one of its files is open in another program.
    """
    if folder.exists():
        try:
            folder.rename(aside)
        except OSError as exc:
            raise DownloadError(f'The documents in {folder} could not be replaced, as one of them is open in another '
                                f'program: {exc}') from exc
    documents.rename(folder)


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
