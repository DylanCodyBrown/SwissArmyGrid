"""An in-memory operating system vault for tests: the container has no real one, and tests must never
touch a person's Credential Manager or Keychain."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import keyring
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1.0

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


@contextmanager
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    fake = MemoryKeyring()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(previous)
