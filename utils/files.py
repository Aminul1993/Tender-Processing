"""File and folder name helpers, and the extraction of the zips a tender's documents come in."""
import logging
import mimetypes
import re
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

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


class ZipTooLarge(Exception):
    """A zip that unpacks to more than extract_zip_recursive() allows: a zip bomb, or not the tender's documents."""


# What an inner zip that cannot be extracted raises: damaged, encrypted, or packed in a way zipfile does not support
_UNEXTRACTABLE = (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error)


def extract_zip_recursive(zip_path, target_dir, max_bytes=2 * 1024 ** 3, max_files=10_000, max_depth=5):
    """
    Extracts a zip and every zip inside it, down to max_depth zips deep. Each inner zip goes into its own folder
    (a.zip -> a/). An inner zip that cannot be extracted (damaged or encrypted, say), or one deeper than max_depth, is
    kept as it is, so the other documents are still checked. Returns the paths of all files extracted.
    Raises ZipTooLarge, extracting no more, when the zips would unpack to more than max_bytes or max_files files in all
    (zipfile writes no more of a file than its stated size, so their stated sizes are what counts).
    """
    left = {'bytes': max_bytes, 'files': max_files}

    def extract(path, folder, depth):
        files = []
        with zipfile.ZipFile(path) as zfile:
            members = [member for member in zfile.infolist() if not member.is_dir()]
            left['bytes'] -= sum(member.file_size for member in members)
            left['files'] -= len(members)
            if left['bytes'] < 0 or left['files'] < 0:
                raise ZipTooLarge(f'Unpacked, the documents are larger than {max_bytes // 1024 ** 2:,} MB or more '
                                  f'than {max_files:,} files')
            for member in members:
                # extract() strips '..' and drive letters, so entries cannot escape folder
                extracted = Path(zfile.extract(member, folder))
                if extracted.suffix.lower() != '.zip' or depth >= max_depth:
                    files.append(extracted.resolve())
                    continue
                try:
                    files += extract(extracted, extracted.with_suffix(''), depth + 1)
                    extracted.unlink()
                except _UNEXTRACTABLE as error:
                    log.warning('Could not extract %s, so it is kept as it is: %s', extracted.name, error)
                    files.append(extracted.resolve())
        return files

    return extract(zip_path, target_dir, 1)
