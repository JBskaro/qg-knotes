# Changelog

## 0.22.1 - 2026-11-12

- Fix Windows startup when a conflicting ICU library was bundled with Qt.
- Bundle the Visual C++ runtime used by PySide6 (contributed in PR #1).
- Add a manual update check button to App settings.
- Try DirectML for local GigaAM recognition on supported Windows GPUs, with CPU fallback.

## 0.22.0 - 2026-11-09

First public release of QG Knotes.

- Record meetings on Windows and transcribe speech locally or through a configured provider.
- Draft minutes, track decisions and tasks, and organize meetings by project.
- Connect an AI assistant to local meetings through MCP.
- Group tasks in generated minutes by customer and implementer when both sides are known.
