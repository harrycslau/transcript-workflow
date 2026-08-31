"""Tests for runtime directory creation and path safety."""

from __future__ import annotations

import os

import pytest

from brainlib.paths import ensure_runtime_dirs, is_writable_dir, runtime_directories

from factories import make_config


def test_ensure_runtime_dirs_creates_all_dirs(tmp_path):
    config = make_config(tmp_path)
    created = ensure_runtime_dirs(config)
    assert created == runtime_directories(config)
    for directory in runtime_directories(config):
        assert directory.is_dir()


def test_ensure_runtime_dirs_is_idempotent(tmp_path):
    config = make_config(tmp_path)
    ensure_runtime_dirs(config)
    assert ensure_runtime_dirs(config) == []


def test_database_parent_directory_is_created(tmp_path):
    config = make_config(tmp_path)
    ensure_runtime_dirs(config)
    assert config.storage.database.parent.is_dir()


def test_is_writable_dir_writable_directory(tmp_path):
    assert is_writable_dir(tmp_path) is True


def test_is_writable_dir_missing_path(tmp_path):
    assert is_writable_dir(tmp_path / "missing") is False


def test_is_writable_dir_regular_file(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    assert is_writable_dir(a_file) is False


def test_is_writable_dir_preserves_preexisting_probe_file(tmp_path):
    probe = tmp_path / ".brain-write-probe"
    probe.write_text("precious content", encoding="utf-8")
    assert is_writable_dir(tmp_path) is True
    assert probe.read_text(encoding="utf-8") == "precious content"


def test_is_writable_dir_leaves_no_probe_files_behind(tmp_path):
    assert is_writable_dir(tmp_path) is True
    leftovers = list(tmp_path.glob(".brain-write-probe*"))
    assert leftovers == []


def test_is_writable_dir_probe_creation_failure(tmp_path, monkeypatch):
    import tempfile as tempfile_module

    def broken_mkstemp(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tempfile_module, "mkstemp", broken_mkstemp)
    assert is_writable_dir(tmp_path) is False


def test_is_writable_dir_unwritable_directory(tmp_path):
    target = tmp_path / "readonly"
    target.mkdir()
    os.chmod(target, 0o500)
    try:
        assert is_writable_dir(target) is False
    finally:
        os.chmod(target, 0o700)
