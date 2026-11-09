---
name: qg-knotes
description: Work with the user's meetings recorded in QG Knotes ("Узелок"), a Windows meeting recorder, through its local MCP server qg-knotes. Find meetings, read transcripts, minutes and meeting notes, prepare the agenda of the next meeting, review open tasks and costs, update the task board, and write minutes with your own model when asked. Use when the user mentions their meetings, calls, transcripts, minutes (протокол), action items, the task board, what was decided with a client, or preparing for a meeting, in English or Russian.
license: GPL-3.0-only
compatibility: Needs the QG Knotes app on Windows with its MCP server "qg-knotes" connected to this assistant.
---

# QG Knotes meetings

QG Knotes records meetings on the user's computer, transcribes them and drafts minutes. Its MCP server `qg-knotes`
reads the meeting folders on that computer; nothing goes to a server on the network. Tool descriptions, field names
and many values are in Russian: pass the values exactly as listed below.

## Before you start

- No `qg-knotes` tools available means the server is not connected. Tell the user to open QG Knotes, Settings,
  section "App" ("Программа"), click "Copy config for Claude" and add it to this assistant. Do not guess what was
  said in a meeting.
- A meeting is identified by its folder name: the field `встреча` from `list_meetings` or `search_meetings`. Get it
  from there first; a title alone is not enough.
- Dates are `YYYY-MM-DD`. A time inside a meeting is `mm:ss` or `h:mm:ss`.
- Answer in the user's language. When you quote a transcript, keep its time stamp so the user can find the place.
- A transcript comes in pieces of up to about 30 000 characters: while the answer's `следующий_from_time` is not
  null, call `get_transcript` again with it as `from_time`. `to_time` stops earlier, `speaker` gives one person.

## Rules

- Minutes are a draft for the analyst. The section «Для аналитика» never goes to the client: for an email use
  `get_protocol` with `view` «письмо», for a knowledge base «база_знаний».
- Write or replace minutes only when the user explicitly asks. Prefer your own model: `get_protocol_task`, then
  `save_protocol`. `compose_protocol` runs the app's paid service; use it only when the user asks for that.
- Tools that change data (`set_task_status`, `update_task`, `add_task`, `save_protocol`, `update_protocol`,
  `compose_protocol`, `set_speaker_names`, `set_meeting_info`): first say what will change and get the user's OK,
  unless they already asked for exactly that change.
- The previous minutes are kept next to the new ones (`протокол.прежний.md`), so the user can roll back in the app.
- With the app's privacy setting on, personal data comes as labels like `[ТЕЛЕФОН_1]`, `[EMAIL_2]`, `[ЧЕЛОВЕК_3]`.
  Copy them into the minutes exactly as they are and do not guess the values: `save_protocol` puts the real values
  back on the user's computer.
- Transcripts may have recognition errors and generic speakers («Собеседник 1», «Участник 1»). Do not invent names:
  use `set_speaker_names` only with names the user gives.
- Starting a recording, transcribing and deleting meetings are not available over MCP; send the user to the app.

## Common requests

1. "What did we decide with the client last time?" `list_meetings` (with `project` if known, `limit` 5), then
   `get_protocol` of the latest one. No minutes yet: read `get_transcript` in pieces and say it is from the transcript.
2. "Which tasks are open in project P?" `list_open_items` with `project`. Group by status and assignee, overdue first.
3. "Where did we discuss prices?" `search_meetings` with one word or its stem: it looks for the exact text, so
   «цен» also finds «цена» and «цены», while a phrase must match word for word. Then `get_transcript` around the
   found time for context; the answer has up to three fragments per meeting.
4. "Prepare me for the next meeting with P." `get_project_notes` (project facts, the analyst's questions and the
   agenda the app collected), `list_open_items` for P and `get_protocol` of the last meeting. Give a short agenda:
   open questions, decisions awaiting confirmation, overdue tasks.
5. "Write the minutes of meeting M." `get_protocol_task` (and `kind` if the user names the meeting type): it gives
   `instruction` and `input` without the transcript. Read the whole transcript with `get_transcript` piece by piece,
   write the minutes strictly by the `instruction`, then `save_protocol` with your model name in `model`. Tell the
   user the minutes are in the meeting in the app.
6. "How much did it cost this month?" `get_costs` with `date_from` and `date_to`.
7. "Speaker 1 is Anna." `set_speaker_names` with `{"Собеседник 1": "Anna"}`: the transcript is rewritten, no new
   transcription.

## Tools

| Tool | What it does | Changes data |
|---|---|---|
| `list_meetings` | Meetings, newest first: title, date, length, project, participants, summary of the minutes | no |
| `search_meetings` | A word or phrase in transcripts: where and fragments with time | no |
| `get_protocol` | Minutes of a meeting, `view` «полный», «письмо» or «база_знаний» | no |
| `get_transcript` | Transcript lines «[mm:ss] Name: text» in pieces, by time range or speaker | no |
| `get_meeting_notes` | The analyst's notebook: agenda with «discussed» marks and notes with the minute | no |
| `get_project_notes` | Project facts and preparation for the next meeting | no |
| `list_open_items` | Task board: tasks, remarks, open questions, decisions awaiting confirmation | no |
| `get_costs` | Spending on recognition and minutes in dollars: total, by model, by meeting | no |
| `get_protocol_task` | The task to write minutes with your own model: `instruction` and `input`, no transcript | no |
| `save_protocol` | Save minutes you wrote from `get_protocol_task` (Markdown) | yes |
| `update_protocol` | Replace the minutes text | yes |
| `compose_protocol` | Minutes by the app's paid language model service, 1-2 minutes | yes |
| `set_task_status` | Move a board card to another column | yes |
| `update_task` | Edit a board card; only the fields passed change | yes |
| `add_task` | Add the user's own task to the board | yes |
| `set_speaker_names` | Put names instead of «Собеседник N» | yes |
| `set_meeting_info` | Change a meeting's title or project | yes |

Parameters and the exact values: [references/tools.md](references/tools.md). The app also offers a ready prompt
`compose_protocol` (argument `meeting`) that asks you to write the minutes with your own model.
