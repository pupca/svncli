"""Tests for the client module — HTML parsing and URL building."""

from __future__ import annotations

import os

import pytest

from svncli.client import SVNWebClient
from svncli.util import encode_svn_path, normalize_remote_path, parse_path

# ── URL encoding ────────────────────────────────────────────────────


class TestEncodeSvnPath:
    def test_simple(self):
        assert encode_svn_path("Repo/folder") == "Repo%2Ffolder"

    def test_nested(self):
        assert encode_svn_path("Repo/a/b/c") == "Repo%2Fa%2Fb%2Fc"

    def test_spaces(self):
        assert encode_svn_path("Repo/my folder") == "Repo%2Fmy%20folder"

    def test_leading_trailing_slashes(self):
        assert encode_svn_path("/Repo/folder/") == "Repo%2Ffolder"

    def test_empty(self):
        assert encode_svn_path("") == ""

    def test_special_chars(self):
        result = encode_svn_path("Repo/file (1).xml")
        assert "%2F" in result
        assert "%28" in result  # (
        assert "%29" in result  # )


class TestNormalize:
    def test_strips_slashes(self):
        assert normalize_remote_path("/a/b/c/") == "a/b/c"

    def test_collapses_doubles(self):
        assert normalize_remote_path("a//b///c") == "a/b/c"

    def test_empty(self):
        assert normalize_remote_path("") == ""


# ── HTML parsing ────────────────────────────────────────────────────

SAMPLE_LISTING_HTML = """
<html><body>
<form name="dir_list" method="post" action="delete.jsp?url=Repo">
<table>
<input type="hidden" name="flags" multiple="yes" value="0" />
<input type="hidden" name="types" multiple="yes" value="images/directory.gif" />
<input type="hidden" name="names" multiple="yes" value="subdir" />
<input type="hidden" name="revisions" multiple="yes" value="42" />
<input type="hidden" name="sizes" multiple="yes" value="&lt;DIR&gt;" />
<input type="hidden" name="dates" multiple="yes" value="2026-03-17 12:00" />
<input type="hidden" name="ages" multiple="yes" value="0 minutes" />
<input type="hidden" name="authors" multiple="yes" value="admin" />
<input type="hidden" name="comments" multiple="yes" value="created dir" />

<input type="hidden" name="flags" multiple="yes" value="0" />
<input type="hidden" name="types" multiple="yes" value="images/file.gif" />
<input type="hidden" name="names" multiple="yes" value="readme.txt" />
<input type="hidden" name="revisions" multiple="yes" value="100" />
<input type="hidden" name="sizes" multiple="yes" value="1 234" />
<input type="hidden" name="dates" multiple="yes" value="2026-03-17 15:30" />
<input type="hidden" name="ages" multiple="yes" value="1 hour" />
<input type="hidden" name="authors" multiple="yes" value="user1" />
<input type="hidden" name="comments" multiple="yes" value="updated readme" />

<input type="hidden" name="flags" multiple="yes" value="0" />
<input type="hidden" name="types" multiple="yes" value="images/file.gif" />
<input type="hidden" name="names" multiple="yes" value="big.bin" />
<input type="hidden" name="revisions" multiple="yes" value="50" />
<input type="hidden" name="sizes" multiple="yes" value="10 485 760" />
<input type="hidden" name="dates" multiple="yes" value="2026-01-01 00:00" />
<input type="hidden" name="ages" multiple="yes" value="2 months" />
<input type="hidden" name="authors" multiple="yes" value="user2" />
<input type="hidden" name="comments" multiple="yes" value="binary blob" />
</table>
</form>
</body></html>
"""


class TestParseDirectoryListing:
    def setup_method(self):
        self.client = SVNWebClient("https://example.com", "dummy=cookie")

    def test_parses_items(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        assert len(items) == 3

    def test_directory_detection(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        subdir = next(i for i in items if i.name == "subdir")
        assert subdir.is_dir is True
        assert subdir.size is None

    def test_file_detection(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        readme = next(i for i in items if i.name == "readme.txt")
        assert readme.is_dir is False
        assert readme.size == 1234  # "1 234" with space separator

    def test_large_size_with_spaces(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        big = next(i for i in items if i.name == "big.bin")
        assert big.size == 10485760  # "10 485 760"

    def test_revision_parsing(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        readme = next(i for i in items if i.name == "readme.txt")
        assert readme.revision == 100

    def test_date_parsing(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        readme = next(i for i in items if i.name == "readme.txt")
        assert readme.last_modified is not None
        assert readme.last_modified.year == 2026
        assert readme.last_modified.hour == 15

    def test_path_construction(self):
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        readme = next(i for i in items if i.name == "readme.txt")
        assert readme.path == "Repo/readme.txt"
        subdir = next(i for i in items if i.name == "subdir")
        assert subdir.path == "Repo/subdir"

    def test_empty_listing(self):
        items = self.client._parse_directory_listing("<html></html>", "Repo")
        assert items == []

    def test_html_entities_in_size(self):
        """Size field for dirs is &lt;DIR&gt; which should be unescaped."""
        items = self.client._parse_directory_listing(SAMPLE_LISTING_HTML, "Repo")
        subdir = next(i for i in items if i.name == "subdir")
        assert subdir.is_dir is True


# ── URL building ────────────────────────────────────────────────────


class TestUrlBuilding:
    def setup_method(self):
        self.client = SVNWebClient("https://example.com", "dummy=cookie")

    def test_url_with_path(self):
        url = self.client._url("directoryContent.jsp", "Repo/folder")
        assert url == "https://example.com/polarion/svnwebclient/directoryContent.jsp?url=Repo%2Ffolder"

    def test_url_with_extra_params(self):
        url = self.client._url("fileDownload.jsp", "Repo/file.txt", attachment="true")
        assert "url=Repo%2Ffile.txt" in url
        assert "attachment=true" in url

    def test_url_no_path(self):
        url = self.client._url("directoryContent.jsp")
        assert url == "https://example.com/polarion/svnwebclient/directoryContent.jsp"

    def test_base_url_auto_appends_suffix(self):
        client = SVNWebClient("https://example.com", "dummy=cookie")
        assert client.base_url == "https://example.com/polarion/svnwebclient"

    def test_base_url_preserves_existing_suffix(self):
        client = SVNWebClient("https://example.com/polarion/svnwebclient", "dummy=cookie")
        assert client.base_url == "https://example.com/polarion/svnwebclient"


# ── Path parsing ────────────────────────────────────────────────────


class TestParsePath:
    def test_remote_with_server(self):
        p = parse_path("https://server.example.com:Repo/folder")
        assert p.server == "https://server.example.com"
        assert p.path == "Repo/folder"
        assert p.is_remote

    def test_remote_with_port(self):
        p = parse_path("https://server.example.com:8443:Repo/folder")
        assert p.server == "https://server.example.com:8443"
        assert p.path == "Repo/folder"

    def test_remote_empty_path(self):
        p = parse_path("https://server.example.com:")
        assert p.server == "https://server.example.com"
        assert p.path == ""
        assert p.is_remote

    def test_local_absolute(self):
        p = parse_path("/home/user/folder")
        assert p.is_local
        assert p.path == "/home/user/folder"

    def test_local_relative(self):
        p = parse_path("./folder")
        assert p.is_local
        assert p.path == "./folder"

    def test_local_home(self):
        p = parse_path("~/folder")
        assert p.is_local
        assert p.path == os.path.expanduser("~/folder")

    def test_bare_path_raises(self):
        with pytest.raises(ValueError, match="Cannot parse path"):
            parse_path("Repo/folder")

    def test_str_roundtrip(self):
        p = parse_path("https://server.com:Repo/path")
        assert str(p) == "https://server.com:Repo/path"

    def test_str_local(self):
        p = parse_path("./local")
        assert str(p) == "./local"
