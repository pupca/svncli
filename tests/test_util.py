"""Tests for utility functions."""

from svncli.util import fmt_size, log_verbose, normalize_remote_path


class TestFmtSize:
    def test_none(self):
        assert fmt_size(None) == "<DIR>"

    def test_bytes(self):
        assert fmt_size(0) == "0B"
        assert fmt_size(512) == "512B"
        assert fmt_size(1023) == "1023B"

    def test_kilobytes(self):
        result = fmt_size(1024)
        assert "KB" in result
        assert "1.0" in result

    def test_megabytes(self):
        result = fmt_size(1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = fmt_size(1024 * 1024 * 1024)
        assert "GB" in result

    def test_large(self):
        result = fmt_size(2 * 1024 * 1024 * 1024 * 1024)
        assert "TB" in result


class TestLogVerbose:
    def test_verbose_true(self, capsys):
        log_verbose("test message", True)
        captured = capsys.readouterr()
        assert "test message" in captured.err

    def test_verbose_false(self, capsys):
        log_verbose("test message", False)
        captured = capsys.readouterr()
        assert captured.err == ""


class TestNormalize:
    def test_already_clean(self):
        assert normalize_remote_path("a/b/c") == "a/b/c"

    def test_single_segment(self):
        assert normalize_remote_path("Repo") == "Repo"
