# brain-proxy

A local OpenAI-compatible endpoint backed by a **web chat AI**. Point Hermes's
model at it and every model call is answered by a real web chat (ChatGPT,
arena, ...) driven over CDP.

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app: `/v1/models`, `/v1/chat/completions`, `/health`, `/debug/dump` |
| `browser.py` | Playwright driver — attaches to a dedicated browser over CDP |
| `config.yaml` | brains, selectors, truncation, escalation, turn budget |
| `start-browser.ps1` | launches the dedicated browser (CDP on 9223) |
| `start-all.bat` | browser + server, used by the logon task |
| `run-proxy.bat` | server only |

## Run

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File start-browser.ps1
.venv\Scripts\python.exe server.py
```

Then log into the target site **once** in the browser window that appears.
The proxy drives the **owner's own Brave profile** (through the space-free
junction `C:\hermes\brave-userdata`), so existing logins are reused.
Hermes's own browser uses a separate profile on port 9222, so the two never
fight.

## Test

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/models
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"chatgpt","messages":[{"role":"user","content":"say hi"}]}'
```

## Point Hermes at it

In Hermes's model config:

```
provider:  openai-compatible   (or "custom")
base_url:  http://127.0.0.1:8000/v1
api_key:   sk-unused
model:     chatgpt             # chatgpt | arena-sonnet | arena-agent
```

Switch brains by changing `model` and restarting Hermes, or set `default` in
`config.yaml`. If one brain fails the proxy walks the `escalation` list.

## How it keeps the brain smart

- **One long-lived web chat per brain** — the thread *is* the memory.
- **Delta only**: turn 1 sends the full conversation, later turns send only the
  new messages. The original task is pinned on the first turn.
- **Tool schema** is injected only when it changes (hash-cached).
- **Tool results are truncated** per type (`tool_truncation` in config).
- **Re-anchor** every `reanchor_every` turns so the brain doesn't drift back
  into "ChatGPT helping a user".
- **Rotation**: at `max_turns_per_thread` the proxy asks for a summary, opens a
  fresh chat, and seeds it with that summary.
- **Escalation**: if a brain fails, it walks the `escalation` list.
- **Reply marker**: a random token is appended to each prompt; the reply is
  everything after the last occurrence, so extraction survives DOM changes.

## Debugging

```bash
curl "http://127.0.0.1:8000/debug/dump?brain=chatgpt"
```

Returns the page URL, every input box, and recent message containers. Use it to
fix selectors when a site redesigns.

## Related projects (this space already exists)

Audited 2026-11-19. The idea is not new — but most public tools do it a
different way, and the difference matters.

| Project | Approach | Verdict |
|---|---|---|
| **ia-web-parser** (PyPI) | Harvest the browser session once (Playwright), then call the provider's **own internal HTTP API** with those cookies. Streaming + `<tool_call>` parsing. | **Best of the bunch.** Fast, no DOM typing, supports Claude/Kimi/Gemini/DeepSeek/Qwen. |
| **siwc-proxy** (PyPI) | "Sign in with ChatGPT" token sharing + a real OAuth client; serves `/v1/chat/completions` and `/v1/responses`. | Cleanest auth story, but ChatGPT-only and needs a sign-in. |
| **ChatGPT-Web2API** (Octo-Lex, MartinezS8) | CDP-driven Chrome, types messages / clicks send / reads responses. OpenAI-compatible + MCP server. | Same architecture as this project. Confirms the approach; same brittleness. |
| **LLM-chat-scraper**, **duck_chat**, **chatsmith-api** | Reverse-engineered provider clients / scrapers. | Narrow, per-provider, need an API key for some. |

**The lesson:** the reliable tools do not drive the DOM. They reuse the
harvested session and call the backend API directly. DOM typing (what
`browser.py` does) is the simplest to build and the first to break — which is
exactly the flakiness seen with empty replies. The next iteration should
harvest the session and call the provider's API instead.

## Costs

Every call is a browser round trip: **20–90 seconds**. Hermes calls the model
many times per task, so tasks are minutes, not seconds. Raise Hermes's model
timeout accordingly.

## Logon task

```powershell
schtasks /create /tn "brain-proxy" /tr "cmd /c C:\hermes\brain-proxy\start-all.bat" /sc onlogon /rl LIMITED /f
```
