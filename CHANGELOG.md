# Changelog

## 0.22.5 - 2026-11-13

- Fix a crash after startup when checking the desktop Teams microphone session.

## 0.22.4 - 2026-11-13

- Restore recording prompts for desktop Teams when Windows reports an active microphone session without updating the microphone access registry.
- Add a short fade when navigating between pages and meeting tabs, following the Windows animation setting.

## 0.22.3 - 2026-11-13

- Keep the local speech model status in sync between Settings and the model picker when a download finishes or files are removed (#3).

## 0.22.2 - 2026-11-13

- Speed up opening the meeting list and task board, and load long transcripts and minutes progressively.
- Allow entering a model ID directly in the minutes settings.
- Keep unrelated keyboard shortcut warnings from appearing when the theme changes.
- Add a CPU or GPU choice for local GigaAM recognition. In GPU mode, use Windows' high-performance adapter preference.

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
