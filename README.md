# brain-proxy

A local OpenAI-compatible endpoint backed by a **web chat AI**. Point Hermes's
model at it and every model call is answered by a real web chat (ChatGPT,
Arena, DeepSeek, z.ai) driven in a **dedicated** browser profile.

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app: `/v1/models`, `/v1/chat/completions`, `/health`, `/debug/dump` |
| `browser.py` | Patchright driver — launches a persistent context on a dedicated profile |
| `brainjson.py` | parses the brain's reply into a tool call or a final answer |
| `config.yaml` | brains, selectors, truncation, escalation, turn budget |
| `tests/` | deterministic tests (no browser, no account) |
| `start-hermes-browser.ps1` | launches **Hermes's own** browser on CDP 9222 |
| `start-all.bat` | server, used by the logon task |
| `run-proxy.bat` | server only |

## Run

```powershell
.venv\Scripts\python.exe server.py
```

The proxy launches Brave itself on the **dedicated** profile
`C:\hermes\brain-proxy\profile` (see `browser.user_data_dir` in
`config.yaml`) and keeps it alive between turns, so the login survives.

Log into the target site **once**, by hand, in the Brave window that opens.
Nothing is typed for you and no credential is stored.

Hermes's *own* browser is a separate program on a separate profile; launch it
with `start-hermes-browser.ps1` if you need it on CDP 9222. The proxy does
not use it and does not touch it.

## Test

```bash
python -m pytest tests/ -q     # deterministic, no browser and no account
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
model:     chatgpt
```

Available models: `chatgpt` (default), `arena-chat`, `arena-code`,
`arena-gpt5`, `arena-agent`, `deepseek`, `zai`.

Switch brains by changing `model`, or set `default` in `config.yaml`. If one
brain fails the proxy walks the `escalation` list (`escalation_enabled: false`
by default).

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
- **Freshness check**: the response bubbles are snapshotted before the prompt
  is sent, so a reply that is really the *previous* turn's answer is never
  returned as this turn's.
- **Never invent a result**: when tools are offered, the brain must answer
  with a tool call or an explicit `{"final": ...}`. Prose that merely claims
  a command result is refused, retried once, and then fails loudly - it is
  never passed through as a finished answer.
- **Self-recovery**: a free web chat fails in a few predictable ways, and each
  has an obvious human remedy. The proxy recognises the message and applies it,
  then waits, instead of handing the failure straight back:

  | Site says | The proxy does |
  |---|---|
  | ran out of memory / context limit | resends the prompt |
  | reached the tool-call limit for this turn | sends `Continue` |
  | workspace over budget / files not saved | starts a fresh session |
  | session expired / reload the page | reloads the tab |

  Each condition is capped (`max`, default 2) so a genuinely broken thread
  fails loudly rather than looping. A phrase only counts when it is *new* since
  the prompt was sent, so an answer that merely discusses "the workspace is
  over budget" is not mistaken for a fault. Add your own conditions under
  `recovery:` in `config.yaml`; they are appended to these defaults.
- **Tab and session changes**: if the tab navigates to another thread on the
  same site mid-run, the proxy re-anchors on it and updates its thread map. If
  it navigates off the site entirely, that is reported as `site_changed`. This
  holds in chat mode as well as agent mode.
- **You can intervene**: type in the composer and the proxy waits (up to three
  escalating pauses) instead of failing the turn, and it never trips the circuit
  breaker over it. It still refuses to overwrite text it did not write.
- **Tools go where they belong**: a brain that runs in its own sandbox
  (Arena's agent has a workspace and a shell) is not handed Hermes's whole tool
  list. `advertise_tools` in `config.yaml` restricts a brain to the tools that
  genuinely need your device; unset means "offer everything", which is right for
  a brain with no workspace of its own. With nothing advertised the brain works
  autonomously and Hermes only relays its answer - fewer tokens and less work
  on your machine.
- **No automation flags**: the browser is launched with an empty argument list.
  `--disable-blink-features=AutomationControlled` and `--no-sandbox` are bot
  fingerprints (Brave prints a warning banner for either), so `navigator.webdriver`
  is hidden with an init script instead, which leaves nothing in the command line.

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
