# qg-knotes tools: parameters

All tools answer with JSON. Names of fields in the answers are Russian (for example `встреча`, `ключ`,
`вид_встречи`). A refusal comes back as text that says what is wrong and what to do.

Common parameters:

- `meeting`: the folder name of the meeting, the field `встреча` from `list_meetings`.
- `project`: a project name; an empty string means meetings without a project.
- `date_from`, `date_to`: `YYYY-MM-DD`, both inclusive.
- `limit`: how many meetings to return, from 1.

## Reading

| Tool | Parameters (required in bold) |
|---|---|
| `list_meetings` | `project`, `date_from`, `date_to`, `limit` |
| `search_meetings` | **`query`**, `project`, `date_from`, `date_to`, `limit` (`встреч_найдено` counts all found) |
| `get_protocol` | **`meeting`**, `view`: «полный» (draft with time stamps and «Для аналитика»), «письмо», «база_знаний» |
| `get_transcript` | **`meeting`**, `from_time`, `to_time` (`mm:ss` or `h:mm:ss`), `speaker` (a name as in the transcript). Up to about 30 000 characters; `следующий_from_time` is the `from_time` of the next piece, null at the end |
| `get_meeting_notes` | **`meeting`** |
| `get_project_notes` | **`project`** |
| `list_open_items` | `project`, `date_from`, `date_to`, `include_done` (true also returns done and archived items) |
| `get_costs` | `project`, `date_from`, `date_to` |
| `get_protocol_task` | **`meeting`**, `kind` (for this task only, not saved). The transcript is not in the task: read it with `get_transcript` |

## Changes

| Tool | Parameters (required in bold) |
|---|---|
| `save_protocol` | **`meeting`**, **`text`** (Markdown, up to 200000 characters), `model` (your model, for the record) |
| `update_protocol` | **`meeting`**, **`text`** (Markdown in the draft format, up to 200000 characters) |
| `compose_protocol` | **`meeting`**, `kind` |
| `set_task_status` | **`task`** (the field `ключ` from `list_open_items`), **`status`** |
| `update_task` | **`task`**, `text`, `description`, `assignee`, `due` (`YYYY-MM-DD`, empty removes it), `priority`, `labels` (all labels of the card), `status` |
| `add_task` | **`text`**, `assignee`, `due`, `project` |
| `set_speaker_names` | **`meeting`**, **`names`** (an object, for example `{"Собеседник 1": "Anna"}`) |
| `set_meeting_info` | **`meeting`**, `title`, `project` (empty string: no project) |

A card from the minutes edited with `update_task` changes only on the board; the minutes stay as they were.

## Values

- `status`: «сделать», «в работе», «готово».
- `priority`: «срочный», «высокий», «средний», «низкий»; an empty string removes the priority.
- `kind`, the meeting type, sets the sections of the minutes. Without it the meeting's own type is used (the field
  `вид_встречи` in `list_meetings`):
  - «Рабочая встреча»: decisions, tasks, questions; for a meeting unlike the other types.
  - «Обследование»: gathering requirements, how the client works now and what they need.
  - «Показ доработки»: a demo or acceptance, what was shown and the remarks.
  - «Согласование ЧТЗ»: reading the spec with the client, what to change and what is still disputed.
  - «Статус проекта»: a regular progress meeting, done, in progress, blockers.
  - «Обучение»: users are shown how to work in the program.
  - «Внутренняя встреча»: the team without the client, minutes without a greeting to the client.
