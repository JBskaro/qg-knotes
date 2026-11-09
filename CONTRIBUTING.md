# Contributing to QG Knotes

Thank you for your interest. Bug reports, ideas and pull requests are welcome. Before a large change, open an issue
and describe what you want to do: it saves both of us time.

## Contributor License Agreement

Every contribution needs a signed [Contributor License Agreement](CLA.md). The bot asks for it in your first pull
request; signing takes one comment.

## Setting up

QG Knotes is a Windows program: it records sound through WASAPI and uses Windows APIs. You need Windows 10 or 11 and
Python 3.13 (64-bit).

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python.exe -m knotes
```

`requirements.lock` pins every library with its SHA-256; `build.py` builds the portable version into `dist\` only
on exactly these versions.

## Checks

```bat
.venv\Scripts\python.exe -m ruff format knotes tests
.venv\Scripts\python.exe check.py
```

`check.py` is the quality guard: nothing that looks like a key in the repository, ruff, dead code (vulture),
duplicated code (pylint), all tests, test coverage of the logic at least 80%. The same checks run on GitHub for every
pull request.

## Rules for code

- One concept lives in one module. Before writing a function, look for an existing one and extend it.
- Fix the cause, not a single case. A bug fix starts with a test that fails on the old code.
- A function has at most 50 statements, complexity at most 10 and at most 5 parameters; public functions have type
  hints.
- Errors are not swallowed: they go to the log, and the user gets a clear message.
- Names, comments and docstrings are in English. Comments are short and explain what is not obvious; the history of
  changes belongs in commit messages, not in comments.
- Tests run without network, without sound output, without dialogs and without the real key store or user folders
  (`tests/__init__.py` sets this up).

## User data

- Never commit recordings, transcripts, minutes, settings or anything from real meetings, not even in tests. Test
  data is made up.
- Files written by earlier versions must keep loading: `tests/test_data_compat.py` reads samples of them. Field names
  in the program's files on disk are mapped in `knotes/disk_names.py`.

## Interface strings

Text shown to the user goes through `tr()`: `tr("Meetings: {n}", n=3)`. Write the English text in code and add its
Russian translation to `knotes/translations/ru/*.json`. Constants marked with `tr_key()` are Russian, some of them are
data as well, and have their English translation in `knotes/translations/en/*.json`. `tests/test_translations.py`
checks that every string has a translation with the same placeholders.

## Pull requests

- One change per pull request, with a short description of what changes and why.
- The checks on GitHub must pass.
- Screenshots help for changes in windows.

## Name and icon

The GPL covers the code, not the name. "QG Knotes", "Uzelok" ("Узелок") and the program icon identify this project.
If you distribute a modified version, give it a different name and icon so that users can tell it apart.
