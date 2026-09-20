"""Public OneDrive / SharePoint share links (URD 3.2, jalur 3).

Three ways this can work, in order of reliability — verified against the
real API on 2026-09-15, not just from documentation:

  1. **A folder synced by the OneDrive desktop client** — handled by
     ``hero.ingest.folders`` (Route 3), a plain local path with no network
     call at all. This is the recommended way to satisfy the URD's "folder
     OneDrive public yang telah dikonfigurasi" requirement, and the only one
     immune to Microsoft's API changes.
  2. **A single-file public share link** — rewritten into a direct-download
     URL and streamed like any other web PDF. No Microsoft API involved, so
     nothing here can be deprecated out from under it.
  3. **A folder share link, listed via the legacy api.onedrive.com "shares"
     endpoint** — kept as a best-effort path, but this API is unreliable in
     practice: a syntactically valid, well-formed token can get back a bare
     ``500 generalException``, and Microsoft's own forums report the same
     endpoint returning ``400``/``401`` for links that work fine in a
     browser. Treat a working folder listing as a bonus, not something to
     depend on — prefer option 1 whenever a listing (not a single file) is
     needed.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import requests

log = logging.getLogger(__name__)

GRAPH_SHARES = "https://api.onedrive.com/v1.0/shares/{token}/root"
ONEDRIVE_HOSTS = (
    "1drv.ms", "onedrive.live.com", "sharepoint.com", "my.sharepoint.com",
)
_FOLDER_HINT = (
    "folder shares are unreliable via the api.onedrive.com listing endpoint "
    "(Microsoft's own forums report intermittent 400/401/500 responses even "
    "for valid links); for a folder, sync it with the OneDrive desktop "
    "client instead and register the resulting local path under `folders:` "
    "in config/sources.yaml — that path needs no API at all"
)
_ACCESS_HINT = (
    "fix it one of two ways: (a) ask the folder owner to create a share link "
    "with 'Anyone with the link can view' (Share → Copy link → link settings) "
    "and pass that URL instead; or (b) sync the folder with the OneDrive "
    "desktop client using your own account and register the local path under "
    "`folders:` in config/sources.yaml, then run `hero folders`"
)


_AUTH_WALL_MARKERS = ("authenticate.aspx", "login.microsoftonline.com", "/_forms/default.aspx")


@dataclass
class LinkDiagnosis:
    """What a link actually is and whether HERO may fetch it anonymously."""

    accessible: bool
    kind: str          # "anonymous-share" | "browser-url" | "unknown"
    reason: str
    server_path: str | None = None   # e.g. /personal/x/Documents/Folder


def server_relative_path(url: str) -> str | None:
    """The folder path inside ``onedrive.aspx?id=...`` browser URLs, if present."""
    query = dict(parse_qsl(urlparse(url).query))
    return query.get("id") or None


def diagnose_link(
    share_url: str, session: requests.Session | None = None, timeout: int = 30,
) -> LinkDiagnosis:
    """Decide up front whether a link can be read without signing in.

    Two very different URLs both look like "an OneDrive folder link":

    * a **share link** (``.../:f:/g/personal/...?e=abc``) — created with
      "Anyone with the link"; readable anonymously;
    * a **browser URL** (``.../_layouts/15/onedrive.aspx?id=...``) — just the
      address bar of a signed-in user; it 302s to a login page for everyone
      else.

    Sending one probe without following redirects tells them apart. HERO does
    not attempt to get past a login page: the Project Charter limits access to
    public data, and credentials for an organisation's tenant are not
    something a scraper should hold.
    """
    parsed = urlparse(share_url)
    is_share_shape = "/:" in parsed.path      # /:f:/g/..., /:b:/g/..., /:u:/s/...
    server_path = server_relative_path(share_url)
    sess = session or requests.Session()
    try:
        resp = sess.get(share_url, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        return LinkDiagnosis(False, "unknown", f"could not reach the link: {exc}", server_path)

    location = (resp.headers.get("Location", "") if hasattr(resp, "headers") else "").lower()
    redirected_to_login = resp.status_code in (301, 302, 303, 307, 308) and any(
        m in location for m in _AUTH_WALL_MARKERS)
    # SharePoint answers the same anonymous request with either a redirect to
    # the login page or a bare 401/403, depending on client headers; both mean
    # "sign-in required", so both get the same specific explanation.
    if redirected_to_login or resp.status_code in (401, 403):
        kind = "anonymous-share" if is_share_shape else "browser-url"
        if is_share_shape:
            reason = ("this share link requires sign-in — its owner set it to "
                      "'People in your organization' instead of 'Anyone with the link'")
        else:
            reason = ("this is a browser address (onedrive.aspx?id=…), not a share "
                      "link, and it requires sign-in")
        return LinkDiagnosis(False, kind, f"{reason}; {_ACCESS_HINT}", server_path)
    return LinkDiagnosis(True, "anonymous-share" if is_share_shape else "unknown",
                         "no sign-in wall detected", server_path)


@dataclass
class ShareItem:
    name: str
    download_url: str
    size: int = 0
    is_folder: bool = False


def is_onedrive_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(h in host for h in ONEDRIVE_HOSTS)


def share_token(share_url: str) -> str:
    """Encode a share URL the way the legacy shares endpoint expects."""
    b64 = base64.urlsafe_b64encode(share_url.encode("utf-8")).decode("ascii")
    return "u!" + b64.rstrip("=")


def resolve_share_url(share_url: str, timeout: int = 45) -> str:
    """Follow 1drv.ms shorteners to the canonical share URL."""
    try:
        resp = requests.head(share_url, allow_redirects=True, timeout=timeout)
        return resp.url or share_url
    except requests.RequestException as exc:
        log.warning("could not resolve share URL %s: %s", share_url, exc)
        return share_url


def direct_download_url(resolved_url: str) -> str:
    """Rewrite a resolved share URL to force a raw-file response.

    Consumer OneDrive (``onedrive.live.com/redir?...``) and SharePoint/
    OneDrive-for-Business guest-access links (``.../guestaccess.aspx?...``)
    both honour a ``download=1`` query parameter that returns the file body
    directly instead of the HTML viewer page. This only makes sense for a
    single-file share; a folder link ignores the parameter and still
    returns HTML, which the caller's magic-byte check will correctly reject.
    """
    parsed = urlparse(resolved_url)
    query = dict(parse_qsl(parsed.query))
    query["download"] = "1"
    return urlunparse(parsed._replace(query=urlencode(query)))


def try_direct_download(
    share_url: str, session: requests.Session, timeout: int = 60
) -> tuple[bytes | None, str | None]:
    """Best-effort single-file fetch that never touches a Microsoft API.

    Returns (pdf_bytes, None) on success, or (None, reason) so the caller
    can fall back to the folder-listing API for the folder case.
    """
    resolved = resolve_share_url(share_url, timeout)
    url = direct_download_url(resolved)
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return None, f"direct-download attempt failed: {exc}"
    if b"%PDF-" not in resp.content[:1024]:
        return None, "direct-download response was not a PDF (likely a folder link)"
    return resp.content, None


def list_share_children(
    share_url: str, session: requests.Session | None = None, timeout: int = 45
) -> tuple[list[ShareItem], str | None]:
    """List files behind a public share link. Returns (items, error)."""
    sess = session or requests.Session()
    token = share_token(resolve_share_url(share_url, timeout))
    url = GRAPH_SHARES.format(token=token) + "?expand=children"
    try:
        resp = sess.get(url, timeout=timeout)
        if resp.status_code == 404:
            return [], "share not found or not public"
        if resp.status_code in (401, 403):
            return [], f"share requires authentication (not a public link); {_FOLDER_HINT}"
        if resp.status_code in (301, 302, 307, 308):
            return [], (
                "api.onedrive.com no longer serves this account "
                f"({resp.status_code}: 'User migrated'); {_FOLDER_HINT}")
        if resp.status_code >= 500:
            return [], (
                f"OneDrive's shares API returned {resp.status_code} "
                f"(known to be flaky for this endpoint); {_FOLDER_HINT}")
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        return [], f"share request failed: {exc}; {_FOLDER_HINT}"
    except ValueError:
        return [], f"share endpoint returned a non-JSON response; {_FOLDER_HINT}"

    if "error" in payload:
        # An error body must never be mistaken for "a link to a single file".
        return [], f"share endpoint error: {payload['error'].get('message', payload['error'])}"
    entries = payload.get("children")
    if entries is None:
        entries = [payload]  # the link points at a single file

    items: list[ShareItem] = []
    for entry in entries:
        dl = entry.get("@content.downloadUrl") or entry.get(
            "@microsoft.graph.downloadUrl")
        items.append(ShareItem(
            name=entry.get("name", "unnamed"),
            download_url=dl or "",
            size=int(entry.get("size") or 0),
            is_folder="folder" in entry,
        ))
    return items, None


# ---------------------------------------------------------------------------
# SharePoint / OneDrive-for-Business folder shares
# ---------------------------------------------------------------------------
# A ".../:f:/g/personal/..." link with "Anyone with the link" makes SharePoint
# hand a browser an anonymous guest cookie (FedAuth) scoped to that folder.
# The same cookie works for SharePoint's own REST API, which — unlike the
# legacy api.onedrive.com endpoint (discontinued: "User migrated") — lists and
# serves files reliably. Only what the link grants is read; nothing here
# touches credentials, and sibling folders are never opened unless asked for.

_ODATA_JSON = {"Accept": "application/json;odata=nometadata"}
DEFAULT_EXCLUDE = ("abstrak", "faq", "matriks")   # companion files, not the regulation


@dataclass
class SharePointFile:
    name: str
    server_relative_url: str
    size: int = 0


def is_sharepoint_folder_share(url: str) -> bool:
    parsed = urlparse(url)
    return "sharepoint.com" in (parsed.netloc or "").lower() and "/:f:/" in parsed.path


def _odata_path(path: str) -> str:
    """Quote a server-relative path for use inside ('...') of a REST call."""
    return quote(path.replace("'", "''"), safe="/")


def open_guest_session(
    share_url: str, session: requests.Session, timeout: int = 45,
) -> tuple[str, str, str, str | None]:
    """Redeem a share link. Returns (origin, site_path, shared_root, error)."""
    try:
        resp = session.get(share_url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return "", "", "", f"could not open share link: {exc}"
    final = urlparse(resp.url)
    if resp.status_code != 200 or any(m in resp.url.lower() for m in _AUTH_WALL_MARKERS):
        return "", "", "", f"share link did not grant guest access (HTTP {resp.status_code}); {_ACCESS_HINT}"
    root = server_relative_path(resp.url)
    if not root:
        return "", "", "", "share link opened but did not reveal which folder it shares"
    segments = [x for x in root.split("/") if x]
    site = "/" + "/".join(segments[:2]) if len(segments) >= 2 else ""
    return f"{final.scheme}://{final.netloc}", site, root, None


def _rest_get(session, origin, site, endpoint, timeout):
    return session.get(f"{origin}{site}/_api/web/{endpoint}",
                       headers=_ODATA_JSON, timeout=timeout)


def list_sharepoint_files(
    session: requests.Session, origin: str, site: str, folder: str,
    timeout: int = 45,
) -> tuple[list[SharePointFile], str | None]:
    """Every file directly inside ``folder``, following server-side paging.

    Deliberately sends no ``$top``: with it, SharePoint returns exactly that
    many rows and *no* next-page link, so a 2,612-file folder looked like a
    500-file one and nothing signalled the truncation. Without it the server
    returns the whole set (and, if it ever pages, a next-page link that the
    loop below follows). Callers should treat a result whose size is a round
    number as suspect and compare it to ``folder_item_count``.
    """
    files: list[SharePointFile] = []
    url = (f"{origin}{site}/_api/web/GetFolderByServerRelativeUrl('{_odata_path(folder)}')"
           f"/Files?$select=Name,Length,ServerRelativeUrl")
    while url:
        try:
            resp = session.get(url, headers=_ODATA_JSON, timeout=timeout)
        except requests.RequestException as exc:
            return files, f"listing failed: {exc}"
        if resp.status_code == 404:
            return files, f"folder not found: {folder}"
        if resp.status_code in (401, 403):
            return files, f"guest session may not read this folder ({resp.status_code})"
        if resp.status_code != 200:
            return files, f"listing returned HTTP {resp.status_code}"
        data = resp.json()
        files += [SharePointFile(v["Name"], v["ServerRelativeUrl"], int(v.get("Length") or 0))
                  for v in data.get("value", [])]
        url = data.get("odata.nextLink") or data.get("@odata.nextLink")
    return files, None


def folder_item_count(session, origin, site, folder, timeout=45) -> int | None:
    """The server's own count of items in a folder, for cross-checking a listing."""
    resp = _rest_get(session, origin, site,
                     f"GetFolderByServerRelativeUrl('{_odata_path(folder)}')?$select=ItemCount",
                     timeout)
    try:
        return int(resp.json()["ItemCount"]) if resp.status_code == 200 else None
    except (ValueError, KeyError, TypeError):
        return None


def list_sharepoint_subfolders(session, origin, site, folder, timeout=45) -> list[str]:
    resp = _rest_get(session, origin, site,
                     f"GetFolderByServerRelativeUrl('{_odata_path(folder)}')/Folders?$select=Name",
                     timeout)
    return [v["Name"] for v in resp.json().get("value", [])] if resp.status_code == 200 else []


def download_sharepoint_folder(
    share_url: str, dest_dir: Path, session: requests.Session | None = None,
    *, subfolder: str | None = None, max_files: int = 100,
    exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE,
    timeout: int = 60, delay: float = 0.5,
) -> tuple[list[Path], list[str]]:
    """Download PDFs from a SharePoint folder share (optionally one subfolder)."""
    sess = session or requests.Session()
    dest_dir.mkdir(parents=True, exist_ok=True)
    origin, site, root, error = open_guest_session(share_url, sess, timeout)
    if error:
        return [], [error]

    folder = root
    if subfolder:
        wanted = subfolder.strip("/")
        if root.rstrip("/").rsplit("/", 1)[-1] != wanted:
            folder = f"{root.rstrip('/')}/{wanted}"

    files, error = list_sharepoint_files(sess, origin, site, folder, timeout)
    errors: list[str] = []
    if error:
        hint = ""
        if "not found" in error:
            names = list_sharepoint_subfolders(sess, origin, site, root, timeout)
            hint = f" — folders available under the shared root: {', '.join(names) or '(none)'}"
        return [], [error + hint]

    expected = folder_item_count(sess, origin, site, folder, timeout)
    if expected is not None and expected > len(files):
        errors.append(f"listing incomplete: server reports {expected} items, got {len(files)}")

    skip = tuple(x.lower() for x in exclude)
    pdfs = [f for f in files
            if f.name.lower().endswith(".pdf") and not any(x in f.name.lower() for x in skip)]
    log.info("sharepoint %s: %d files, %d PDFs after excluding companions",
             folder, len(files), len(pdfs))

    saved: list[Path] = []
    for f in pdfs[:max_files]:
        try:
            resp = sess.get(
                f"{origin}{site}/_api/web/GetFileByServerRelativeUrl("
                f"'{_odata_path(f.server_relative_url)}')/$value", timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            errors.append(f"{f.name}: {exc}")
            continue
        if b"%PDF-" not in resp.content[:1024]:
            errors.append(f"{f.name}: not a PDF")
            continue
        path = dest_dir / Path(f.name).name
        path.write_bytes(resp.content)
        saved.append(path)
        time.sleep(delay)   # be polite to the tenant
    if len(pdfs) > max_files:
        errors.append(f"{len(pdfs) - max_files} more PDFs available; raise the limit to fetch them")
    return saved, errors


def download_onedrive_share(
    share_url: str,
    dest_dir: Path,
    session: requests.Session | None = None,
    max_files: int = 100,
    timeout: int = 60,
    subfolder: str | None = None,
    exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE,
) -> tuple[list[Path], list[str]]:
    """Download the PDF(s) behind a public share link. Returns (paths, errors).

    Tries the API-free single-file path first; only reaches for the
    folder-listing API if that is not applicable (i.e. the link really does
    point at a folder rather than one file).
    """
    sess = session or requests.Session()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Fail early and precisely: a sign-in wall makes every later attempt
    # produce a confusing "not a PDF" / "share not found" instead of the cause.
    diagnosis = diagnose_link(share_url, sess, timeout)
    if not diagnosis.accessible:
        return [], [diagnosis.reason]

    if is_sharepoint_folder_share(share_url):
        return download_sharepoint_folder(
            share_url, dest_dir, sess, subfolder=subfolder, max_files=max_files,
            exclude=exclude, timeout=timeout)

    data, direct_error = try_direct_download(share_url, sess, timeout)
    if data is not None:
        name = Path(urlparse(resolve_share_url(share_url, timeout)).path).name
        if not name.lower().endswith(".pdf"):
            name = "document.pdf"
        path = dest_dir / name
        path.write_bytes(data)
        return [path], []

    items, error = list_share_children(share_url, sess, timeout)
    if error:
        return [], [f"single-file download: {direct_error}", error]

    saved: list[Path] = []
    errors: list[str] = [f"single-file download: {direct_error} — "
                        f"treating link as a folder share instead"]
    for item in items:
        if item.is_folder:
            errors.append(f"{item.name}: nested folders are not traversed")
            continue
        if not item.name.lower().endswith(".pdf"):
            continue
        if not item.download_url:
            errors.append(f"{item.name}: no download URL in share response")
            continue
        try:
            resp = sess.get(item.download_url, timeout=timeout)
            resp.raise_for_status()
            data = resp.content
        except requests.RequestException as exc:
            errors.append(f"{item.name}: {exc}")
            continue
        if b"%PDF-" not in data[:1024]:
            errors.append(f"{item.name}: not a PDF")
            continue
        path = dest_dir / item.name
        path.write_bytes(data)
        saved.append(path)
        if len(saved) >= max_files:
            break
    return saved, errors
