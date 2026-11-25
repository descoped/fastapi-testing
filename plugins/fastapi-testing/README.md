# fastapi-testing plugin for Claude Code

Skills that make Claude Code fluent in [fastapi-testing](https://github.com/descoped/fastapi-testing), so it writes
integration tests that serve your FastAPI application on a real uvicorn server, with real HTTP, Server-Sent Events
and WebSocket connections.

| Skill | Use it to |
|---|---|
| `/fastapi-testing:write-tests` | add fastapi-testing to a project, write fixtures and tests, fake upstream services, test SSE and WebSockets, prototype in a notebook |
| `/fastapi-testing:troubleshoot` | diagnose a test run that hangs, leaks connections or warnings, fails at teardown, or broke after an upgrade |

Claude Code also picks the right skill by itself when you ask for tests of a FastAPI app.

## Install

```bash
claude plugin marketplace add descoped/fastapi-testing
claude plugin install fastapi-testing@fastapi-testing
```

The plugin version follows the library version, so the skills describe the release on PyPI.
