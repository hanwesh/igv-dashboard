"""All test archives live in pytest temporary directories and block real provider calls."""

import socket
from urllib.request import OpenerDirector

import pytest

from tests.synthetic import TEST_END, Provider, seed


@pytest.fixture(autouse=True)
def no_financial_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not call a live financial endpoint")

    monkeypatch.setattr(OpenerDirector, "open", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.delenv("DATA_PUBLICATION_APPROVED", raising=False)
    monkeypatch.delenv("SEC_PUBLICATION_APPROVED", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "data"
    seed(root, provider=Provider(warning_period=TEST_END))
    return root
