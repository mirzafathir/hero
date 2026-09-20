"""OneDrive share-link handling: the API-free single-file path first,
the flaky listing API only as a fallback, with actionable error messages."""
from __future__ import annotations

import requests

from hero.ingest.onedrive import (
    diagnose_link, download_sharepoint_folder, list_sharepoint_files, direct_download_url, download_onedrive_share, is_onedrive_url,
    list_share_children, try_direct_download,
)


class FakeResponse:
    def __init__(self, status_code=200, content=b"", json_data=None, url="",
                 headers=None):
        self.status_code = status_code
        self.content = content
        self._json = json_data
        self.url = url
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, responses):
        # responses: list of callables(url, **kwargs) -> FakeResponse, consumed in order
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        handler = self._responses.pop(0)
        return handler(url, **kwargs)

    def head(self, url, **kwargs):
        return FakeResponse(200, url=url)


# The first request of download_onedrive_share is the access probe.
PROBE_OK = lambda url, **kw: FakeResponse(200)  # noqa: E731

# The real browser URL supplied for the HERO capstone folder, and the redirect
# SharePoint answered it with when fetched anonymously (2026-09-20).
BROWSER_URL = (
    "https://oneojk-my.sharepoint.com/personal/faris_budi_ojk_go_id/_layouts/15/"
    "onedrive.aspx?id=%2Fpersonal%2Ffaris%5Fbudi%5Fojk%5Fgo%5Fid%2FDocuments%2F"
    "PUBLIC%5FGPSI%2FITS%20Capstone%20Project%202026%2FHERO%2Fdownloads&ga=1"
)
AUTH_REDIRECT = {"Location": "https://oneojk-my.sharepoint.com/personal/faris_budi_ojk_go_id/"
                             "_layouts/15/Authenticate.aspx?Source=%2Fpersonal"}


def test_is_onedrive_url_recognises_known_hosts():
    assert is_onedrive_url("https://1drv.ms/f/s!abc")
    assert is_onedrive_url("https://contoso-my.sharepoint.com/:f:/g/xyz")
    assert not is_onedrive_url("https://example.com/file.pdf")


def test_direct_download_url_adds_download_param():
    url = direct_download_url("https://onedrive.live.com/redir?resid=ABC123")
    assert "download=1" in url
    assert "resid=ABC123" in url


def test_direct_download_url_preserves_existing_params():
    url = direct_download_url("https://x.sharepoint.com/guestaccess.aspx?e=xyz&share=1")
    assert "download=1" in url
    assert "e=xyz" in url
    assert "share=1" in url


def test_try_direct_download_succeeds_on_pdf_response(monkeypatch):
    session = FakeSession([
        lambda url, **kw: FakeResponse(200, content=b"%PDF-1.4 fake pdf body"),
    ])
    monkeypatch.setattr(
        "hero.ingest.onedrive.resolve_share_url",
        lambda url, timeout=45: "https://onedrive.live.com/redir?resid=X",
    )
    data, error = try_direct_download("https://1drv.ms/f/s!x", session)
    assert data == b"%PDF-1.4 fake pdf body"
    assert error is None


def test_try_direct_download_rejects_html_response(monkeypatch):
    """A folder link ignores download=1 and returns a viewer page — must
    be recognised as 'not a PDF', not silently accepted."""
    session = FakeSession([
        lambda url, **kw: FakeResponse(200, content=b"<html>folder viewer</html>"),
    ])
    monkeypatch.setattr(
        "hero.ingest.onedrive.resolve_share_url",
        lambda url, timeout=45: "https://onedrive.live.com/redir?resid=FOLDER",
    )
    data, error = try_direct_download("https://1drv.ms/f/s!folder", session)
    assert data is None
    assert "not a PDF" in error


def test_list_share_children_reports_actionable_message_on_server_error(monkeypatch):
    session = FakeSession([lambda url, **kw: FakeResponse(500)])
    monkeypatch.setattr(
        "hero.ingest.onedrive.resolve_share_url", lambda url, timeout=45: url)
    items, error = list_share_children("https://1drv.ms/f/s!broken", session)
    assert items == []
    assert "500" in error
    assert "OneDrive desktop client" in error


def test_list_share_children_flags_auth_required(monkeypatch):
    session = FakeSession([lambda url, **kw: FakeResponse(401)])
    monkeypatch.setattr(
        "hero.ingest.onedrive.resolve_share_url", lambda url, timeout=45: url)
    items, error = list_share_children("https://1drv.ms/f/s!private", session)
    assert items == []
    assert "authentication" in error


def test_download_onedrive_share_uses_direct_path_for_single_file(tmp_path, monkeypatch):
    session = FakeSession([
        PROBE_OK,   # diagnose_link runs first, before any download
        lambda url, **kw: FakeResponse(200, content=b"%PDF-1.4 hello"),
    ])
    monkeypatch.setattr(
        "hero.ingest.onedrive.resolve_share_url",
        lambda url, timeout=45: "https://onedrive.live.com/redir?resid=X",
    )
    paths, errors = download_onedrive_share("https://1drv.ms/f/s!x", tmp_path, session)
    assert len(paths) == 1
    assert paths[0].read_bytes().startswith(b"%PDF-")
    assert errors == []


def test_download_onedrive_share_falls_back_to_listing_for_folder(tmp_path, monkeypatch):
    calls = {"n": 0}

    def direct_attempt(url, **kw):
        return FakeResponse(200, content=b"<html>folder</html>")

    def listing_attempt(url, **kw):
        return FakeResponse(200, json_data={
            "children": [
                {"name": "reg1.pdf", "size": 10,
                 "@microsoft.graph.downloadUrl": "https://x/reg1.pdf"},
                {"name": "notes.txt", "size": 5},
            ]
        })

    def file_download(url, **kw):
        return FakeResponse(200, content=b"%PDF-1.4 doc")

    session = FakeSession([PROBE_OK, direct_attempt, listing_attempt, file_download])
    monkeypatch.setattr(
        "hero.ingest.onedrive.resolve_share_url", lambda url, timeout=45: url)

    paths, errors = download_onedrive_share("https://1drv.ms/f/s!folder", tmp_path, session)
    assert len(paths) == 1
    assert paths[0].name == "reg1.pdf"
    assert any("treating link as a folder share" in e for e in errors)


def test_diagnose_browser_url_behind_sign_in_is_not_accessible():
    """onedrive.aspx?id=… is a signed-in user's address bar, not a share link."""
    session = FakeSession([lambda url, **kw: FakeResponse(302, headers=AUTH_REDIRECT)])
    d = diagnose_link(BROWSER_URL, session)
    assert d.accessible is False
    assert d.kind == "browser-url"
    assert "share link" in d.reason
    assert "Anyone with the link" in d.reason
    assert d.server_path.endswith("/HERO/downloads")


def test_diagnose_org_only_share_link_names_the_real_cause():
    """A well-formed :f: share link can still demand sign-in."""
    url = "https://oneojk-my.sharepoint.com/:f:/g/personal/x/Eabc?e=123"
    session = FakeSession([lambda url, **kw: FakeResponse(302, headers=AUTH_REDIRECT)])
    d = diagnose_link(url, session)
    assert d.accessible is False
    assert d.kind == "anonymous-share"
    assert "People in your organization" in d.reason


def test_diagnose_open_link_is_accessible():
    url = "https://oneojk-my.sharepoint.com/:f:/g/personal/x/Eabc?e=123"
    session = FakeSession([lambda url, **kw: FakeResponse(200)])
    assert diagnose_link(url, session).accessible is True


def test_download_stops_at_sign_in_wall_without_further_requests(tmp_path):
    """No fallback attempts once we know a login is required — HERO does not
    try to get past authentication."""
    session = FakeSession([lambda url, **kw: FakeResponse(302, headers=AUTH_REDIRECT)])
    paths, errors = download_onedrive_share(BROWSER_URL, tmp_path, session)
    assert paths == []
    assert len(errors) == 1 and "sign-in" in errors[0]
    assert len(session.calls) == 1


def test_diagnose_reports_network_failure_instead_of_raising():
    class Boom:
        def get(self, url, **kw):
            raise requests.ConnectionError("dns")
    d = diagnose_link(BROWSER_URL, Boom())
    assert d.accessible is False and "could not reach" in d.reason


def test_diagnose_bare_403_gets_the_same_specific_explanation():
    """SharePoint sometimes answers with 403 instead of redirecting to login."""
    session = FakeSession([lambda url, **kw: FakeResponse(403)])
    d = diagnose_link(BROWSER_URL, session)
    assert d.accessible is False
    assert d.kind == "browser-url"
    assert "Anyone with the link" in d.reason


# --- SharePoint folder shares (guest session + REST API) --------------------

SP_SHARE = "https://oneojk-my.sharepoint.com/:f:/g/personal/x_ojk_go_id/Eabc?e=1"
SP_LANDING = ("https://oneojk-my.sharepoint.com/personal/x_ojk_go_id/_layouts/15/onedrive.aspx"
              "?id=%2Fpersonal%2Fx_ojk_go_id%2FDocuments%2FHERO&ga=1")


def _routes(table):
    """A fake session that answers by URL substring, recording every call."""
    class S:
        def __init__(self):
            self.calls = []
        def get(self, url, **kw):
            self.calls.append(url)
            for needle, resp in table:
                if needle in url:
                    return resp(url) if callable(resp) else resp
            return FakeResponse(404)
    return S()


def _json(data, code=200):
    r = FakeResponse(code, json_data=data)
    r.headers = {"Content-Type": "application/json"}
    return r


def test_listing_never_sends_top_which_truncates_silently():
    """With $top SharePoint returns exactly that many rows and no next link,
    so a 2,612-file folder was read as 500. Regression guard."""
    sess = _routes([("/Files", _json({"value": [
        {"Name": "a.pdf", "Length": 1, "ServerRelativeUrl": "/p/a.pdf"}]}))])
    files, err = list_sharepoint_files(sess, "https://h", "/personal/x", "/personal/x/D")
    assert err is None and len(files) == 1
    assert "$top" not in sess.calls[0]


def test_listing_follows_next_page_links():
    page2 = "https://h/next2"
    sess = _routes([
        ("next2", _json({"value": [{"Name": "b.pdf", "Length": 1, "ServerRelativeUrl": "/b"}]})),
        ("/Files", _json({"value": [{"Name": "a.pdf", "Length": 1, "ServerRelativeUrl": "/a"}],
                          "odata.nextLink": page2})),
    ])
    files, _ = list_sharepoint_files(sess, "https://h", "/personal/x", "/personal/x/D")
    assert [f.name for f in files] == ["a.pdf", "b.pdf"]


def test_sharepoint_download_reads_only_requested_subfolder_and_skips_companions(tmp_path):
    listing = {"value": [
        {"Name": "Peraturan_OJK_1_2022.pdf", "Length": 9, "ServerRelativeUrl": "/personal/x_ojk_go_id/Documents/HERO/downloads/Peraturan_OJK_1_2022.pdf"},
        {"Name": "FAQ_SEOJK_1.pdf", "Length": 9, "ServerRelativeUrl": "/personal/x_ojk_go_id/Documents/HERO/downloads/FAQ_SEOJK_1.pdf"},
        {"Name": "Abstrak_POJK_2.pdf", "Length": 9, "ServerRelativeUrl": "/personal/x_ojk_go_id/Documents/HERO/downloads/Abstrak_POJK_2.pdf"},
        {"Name": "catatan.txt", "Length": 9, "ServerRelativeUrl": "/personal/x_ojk_go_id/Documents/HERO/downloads/catatan.txt"},
    ]}
    landing = FakeResponse(200, url=SP_LANDING)
    sess = _routes([
        ("/:f:/", landing),
        ("GetFileByServerRelativeUrl", FakeResponse(200, content=b"%PDF-1.4 x")),
        ("ItemCount", _json({"ItemCount": 4})),
        ("/Files", _json(listing)),
    ])
    paths, errors = download_sharepoint_folder(
        SP_SHARE, tmp_path, sess, subfolder="downloads", delay=0)
    assert [p.name for p in paths] == ["Peraturan_OJK_1_2022.pdf"]
    assert errors == []
    listed = [c for c in sess.calls if "/Files" in c]
    assert "HERO/downloads" in listed[0].replace("%2F", "/")
    assert not any("Data%20Internal" in c or "Administration" in c for c in sess.calls)


def test_sharepoint_download_flags_incomplete_listing(tmp_path):
    sess = _routes([
        ("/:f:/", FakeResponse(200, url=SP_LANDING)),
        ("ItemCount", _json({"ItemCount": 2612})),
        ("/Files", _json({"value": [{"Name": "a.pdf", "Length": 1, "ServerRelativeUrl": "/a"}]})),
        ("GetFileByServerRelativeUrl", FakeResponse(200, content=b"%PDF-1.4")),
    ])
    _, errors = download_sharepoint_folder(SP_SHARE, tmp_path, sess, delay=0)
    assert any("listing incomplete" in e and "2612" in e for e in errors)


def test_missing_subfolder_names_the_folders_that_do_exist(tmp_path):
    sess = _routes([
        ("/:f:/", FakeResponse(200, url=SP_LANDING)),
        ("/Folders", _json({"value": [{"Name": "downloads"}, {"Name": "Administration"}]})),
    ])
    paths, errors = download_sharepoint_folder(SP_SHARE, tmp_path, sess, subfolder="nope", delay=0)
    assert paths == [] and "folder not found" in errors[0] and "downloads" in errors[0]


def test_legacy_api_error_body_is_not_mistaken_for_a_file(monkeypatch):
    """api.onedrive.com now answers 308 {"error": "User migrated"}; that used
    to be parsed as a single unnamed file."""
    sess = FakeSession([lambda url, **kw: FakeResponse(308)])
    monkeypatch.setattr("hero.ingest.onedrive.resolve_share_url", lambda u, timeout=45: u)
    items, err = list_share_children("https://x.sharepoint.com/:f:/g/a", sess)
    assert items == [] and "no longer serves" in err
