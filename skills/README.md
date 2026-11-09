# Skills for AI assistants

`qg-knotes/` is an [Agent Skills](https://agentskills.io/specification) skill: a folder with `SKILL.md` that tells an
assistant how to use the QG Knotes MCP server. The skill alone has no data: connect the MCP server first, then add
the skill.

The command that starts the server is the program with `--mcp`, for example
`C:\Users\<you>\QG Knotes\QG Knotes.exe --mcp`. In QG Knotes, Settings, section "App", "Copy config for Claude" copies
it with the right path for your computer.

## Claude Desktop

1. MCP: Claude Desktop, Settings, Developer, Edit Config. If `claude_desktop_config.json` is empty, paste the copied
   config as a whole; if it already has `mcpServers`, move only the `qg-knotes` entry there. Restart Claude.
2. Skill: zip the `qg-knotes` folder (the folder itself at the root of the zip), then in Claude Settings,
   Capabilities, Skills, "Upload skill". The menu names may differ between versions.

## Claude Code

1. MCP: `claude mcp add --scope user qg-knotes -- "C:\Users\<you>\QG Knotes\QG Knotes.exe" --mcp`
2. Skill: copy the `qg-knotes` folder to `%USERPROFILE%\.claude\skills\`.

## ChatGPT desktop app and Codex

ChatGPT on the web cannot start a program on your computer, so it cannot reach QG Knotes; use the desktop app or
Codex.

1. MCP: in the ChatGPT desktop app, Settings, MCP servers, Add server, STDIO: the command is the path to
   `QG Knotes.exe`, the argument `--mcp`. The desktop app, Codex CLI and the IDE extension share
   `%USERPROFILE%\.codex\config.toml`, so this also works there:

   ```toml
   [mcp_servers.qg-knotes]
   command = 'C:\Users\<you>\QG Knotes\QG Knotes.exe'
   args = ["--mcp"]
   ```

2. Skill: copy the `qg-knotes` folder to `%USERPROFILE%\.agents\skills\`.

## Cursor

1. MCP: `%USERPROFILE%\.cursor\mcp.json` (or `.cursor\mcp.json` in a project):

   ```json
   {
     "mcpServers": {
       "qg-knotes": {
         "type": "stdio",
         "command": "C:\\Users\\<you>\\QG Knotes\\QG Knotes.exe",
         "args": ["--mcp"]
       }
     }
   }
   ```

2. Skill: copy the `qg-knotes` folder to `%USERPROFILE%\.cursor\skills\`. Cursor also reads `%USERPROFILE%\.agents\skills\`
   and `%USERPROFILE%\.claude\skills\`, so one copy there serves several assistants.

## Check

Ask the assistant "which meetings did I have this week?". It should call `list_meetings` and list them. If it says it
has no such tools, the MCP server is not connected: check the path to `QG Knotes.exe` and restart the assistant.
