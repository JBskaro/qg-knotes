# Changelog

## 0.23.4 - 2026-11-25

- Rename a note directly in the notebook list by double-clicking its title, pressing F2, or choosing Rename from its menu.
- Enter or clicking away saves the new title. Escape cancels the edit.

## 0.23.3 - 2026-11-25

- Rename notebook notes from the list, the note menu or with F2.
- Use a theme-aware update icon that matches the sidebar.
- Add RouterAI and Groq for speech and minutes, and DeepSeek for minutes. Track reported charges in their original currency.
- Improve recovery of recordings, minutes and expense receipts, calendar reminders and privacy masking.

## 0.23.2 - 2026-11-19

- Search transcripts, minutes and notebooks in the background and jump to the matching point in a transcript.
- Create a task from selected text and restore a previous version of the minutes.
- Keep the transcription response across spending journal failures and count each receipt once.
- Preserve screen recording fragments and unsaved project edits on errors. Detect changed source recordings and prevent project name collisions.
- Apply privacy masking to custom minutes instructions, respect calendar cancellations and paginate transcript lines that share a timestamp.
- Show the local recognition model, processing time and CPU or DirectML backend.

## 0.23.1 - 2026-11-15

- Check for updates at startup and in the background. Show the sidebar button only when the release has a matching package with a checksum, and confirm before downloading.
- Make Esc and the default Enter action cancel note and task deletion.
- Keep selected task cards, their status, and card edits when the wording in the minutes changes. Show items with the same wording and different assignees separately.
- Refresh spending while the summary is open and retain new charges in a reserve journal if the main journal cannot be written.
- Count spending by payment date and meeting duration by meeting date in the summary table.
- Keep the editor in sync with the notebook project filter after moving a note.

## 0.23.0 - 2026-11-15

- Add a standalone notebook with formatting, project filters, archive, deletion, and restoration of the last note and cursor.
- Improve meeting notes with timed entries, local search, and private lines kept out of assistant responses and exports.
- Add a meeting filter for recordings that need transcription or minutes.
- Let users edit or write minutes manually and select which items to add to the task board. Add card deletion and a filter for open tasks due today or overdue.
- Calculate the spending total from the local journal and show provider key usage separately.
- Show a download icon above Settings when GitHub has a newer release. Clicking the icon starts installation, then the app restarts.

## 0.22.6 - 2026-11-13

- Show an update notification and keep the Update button next to search when installation is postponed.
- Apply personal-data masking across MCP meeting and task responses, including task identifiers derived from minutes.
- Keep imported files separate from same-named recordings and allow interrupted imports to be retried.
- Update the bundled FFmpeg to 9.0.2 and keep provider response bodies out of diagnostic warnings.
- Pin GitHub Actions to commit hashes and remove unused code.

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
