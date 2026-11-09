# Security policy

## Supported versions

Security fixes go into the latest release. Update to it before reporting a problem: the program checks for new
versions on GitHub and offers to update.

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Report it privately on GitHub: the "Security" tab of the repository,
"Report a vulnerability". Please include:

- the version of QG Knotes and of Windows;
- what an attacker can do and what is needed for it;
- steps to reproduce, or a proof of concept.

The maintainer aims to answer within a week and to agree with you on when the problem is described publicly, after
a fixed version is out.

## How the program handles sensitive data

- API keys are stored in the Windows Credential Manager, not in files.
- Recordings, transcripts, minutes and notes stay in the recordings folder on the user's computer.
- With cloud speech recognition, the sound of a meeting is sent to the provider the user chose; with recognition on
  this computer, nothing leaves the computer. Minutes are written by the chosen language model provider or by the
  user's own AI assistant over MCP.
- The program's own windows are excluded from screen recording and screen sharing where Windows allows it.
- Updates come from the releases of the GitHub repository over HTTPS.
