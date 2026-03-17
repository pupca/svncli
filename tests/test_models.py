"""Tests for data models."""

from svncli.models import SyncAction, SyncOp


class TestSyncAction:
    def test_str_upload(self):
        a = SyncAction(op=SyncOp.UPLOAD, remote_path="Repo/file.txt", reason="new file")
        s = str(a)
        assert "upload" in s
        assert "Repo/file.txt" in s
        assert "new file" in s

    def test_str_skip(self):
        a = SyncAction(op=SyncOp.SKIP, remote_path="Repo/file.txt", reason="unchanged")
        s = str(a)
        assert "skip" in s

    def test_str_delete(self):
        a = SyncAction(op=SyncOp.DELETE, remote_path="Repo/old.txt", reason="not in local")
        s = str(a)
        assert "delete" in s

    def test_all_ops_have_str(self):
        """Every SyncOp should produce a valid string."""
        for op in SyncOp:
            a = SyncAction(op=op, remote_path="test")
            s = str(a)
            assert op.value in s
