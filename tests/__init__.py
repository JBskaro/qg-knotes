# SPDX-License-Identifier: GPL-3.0-only
"""Tests do not touch real keys: the Windows credential store is replaced with an empty one, key variables from
the registry are not read. Tests do not open message boxes on screen and do not go to the internet: a box not
replaced in the test itself fails the test, curl requests to outside addresses go to a nonexistent proxy and
fail at once."""

import atexit
import os
import pathlib
import shutil
import tempfile

import keyring
import keyring.backends.null
from PySide6.QtWidgets import QInputDialog, QMessageBox

from knotes import language, settings

keyring.set_keyring(keyring.backends.null.Keyring())
settings._user_variable = lambda name: os.environ.get(name, "")  # noqa: SLF001
for _name in ("OPENROUTER_API_KEY", "NEXARA_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_name, None)
os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"  # discard port: the connection is refused
os.environ["NO_PROXY"] = "127.0.0.1,localhost"  # the local fake server of the tests
_MODELS = pathlib.Path(tempfile.mkdtemp(prefix="модели_тестов_"))  # tests do not see the owner's downloaded models
settings.models_folder = lambda: _MODELS
language.windows_language = lambda: "ru"  # the tests expect Russian Windows, whatever the machine's language
atexit.register(shutil.rmtree, _MODELS, ignore_errors=True)


def _dialog_in_test(*_arguments: object, **_named: object) -> None:
    raise AssertionError("окно сообщения в тесте: подмените его в самом тесте (mock.patch.object)")


for _cls, _methods in (
    (QMessageBox, ("warning", "question", "information", "critical")),
    (QInputDialog, ("getText", "getItem")),
):
    for _method in _methods:
        setattr(_cls, _method, staticmethod(_dialog_in_test))
QMessageBox.exec = _dialog_in_test  # a box made by hand (style.ask) as well
