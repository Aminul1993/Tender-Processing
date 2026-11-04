"""File and folder name helpers."""
import mimetypes
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

_UNSAFE_CHARACTERS = re.compile(r'[^A-Za-z0-9._-]+')


def safe_name(value, default='file'):
    """
    Turns any text into a single, safe path component: no separators, no '..', no characters
    Windows rejects. Used for TCNO folder names and downloaded file names.
    """
    name = _UNSAFE_CHARACTERS.sub('_', str(value)).strip('._')
    return name or default


def filename_from_response(response, default='document'):
    """
    Picks a file name for a download: Content-Disposition first, then the last part of the URL.
    Adds an extension from the Content-Type when the name has none, so file readers can tell the type.
    """
    disposition = response.headers.get('Content-Disposition', '')
    # RFC 5987 form first (filename*=UTF-8''name.pdf), then the plain one (filename="name.pdf")
    match = (re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.IGNORECASE)
             or re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE))
    name = unquote(match.group(1).strip()) if match else unquote(urlparse(response.url).path)
    # Keep only the last component, whichever separator the server used
    name = safe_name(PureWindowsPath(PurePosixPath(name).name).name, default)

    if not PurePosixPath(name).suffix:
        content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
        name += mimetypes.guess_extension(content_type) or ''
    return name
