# Drafts

A local iMessage reply assistant for macOS. It reads your Messages history, learns how you
write to a specific person, and drafts replies in your voice for you to approve. Works with
Anthropic, OpenAI, any OpenAI-compatible provider, or a local model.

Everything runs on your Mac. The only thing that leaves the machine is a sample of your own
sent messages (for learning a voice) and recent thread context (for drafting a reply), both
sent to the Anthropic API. Nothing is uploaded anywhere else, and nothing is ever written to
the Messages database.

<!-- Add a screenshot here: ![Drafts](docs/screenshot.png) -->

## What it does

- Reads your Messages database read-only and shows a conversation
- Learns your texting style per contact from your own sent messages
- Drafts replies in that style, which you approve, edit, or reject
- Runs several conversations side by side, each with its own voice and state
- Optionally auto-sends after a countdown, with a hold on anything risky

## Requirements

- macOS with Messages signed in (this cannot work on Linux or Windows — see [Why macOS only](#why-macos-only))
- Python 3.9 or newer
- An API key from Anthropic, OpenAI, or any OpenAI-compatible provider — or a local model
  and no key at all (see [Model providers](#model-providers))
- Full Disk Access granted to Terminal

No pip installs. The whole thing uses the Python standard library.

## Setup

### 1. Clone and enter the repo

```bash
git clone https://github.com/YOUR_USERNAME/drafts.git
cd drafts
```

### 2. Grant Full Disk Access to Terminal

System Settings → Privacy & Security → Full Disk Access → enable **Terminal**, then quit and
reopen Terminal. The Messages database is protected; without this, nothing can read it.

### 3. Set your API key

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

To make it permanent, add that line to `~/.zshrc`. Type it in a terminal editor rather than
pasting from a rich-text app — smart quotes will break it.

Using something other than Anthropic? See [Model providers](#model-providers) below.

### 4. Run it

```bash
python3 imsg_web.py
```

It prints a URL and opens your browser. The URL contains a token stored in
`~/.imsg_assist/token`; requests without it are refused, so other pages in your browser can't
reach the endpoints.

## Using it

The left rail is the flow, top to bottom.

**Add a contact.** Type a number in `+1XXXXXXXXXX` form, or click Browse to pick from your
recent conversations. Names come from your macOS Contacts automatically; unmatched numbers get
a `+ name` button so you can label them yourself. Each contact becomes a column.

**Learn a voice.** Click **Voice** in the column header, then *Learn from this thread*. This is
the one meaningful API charge — it sends up to 400 of your sent messages in a single call and
writes a style guide to `~/.imsg_assist/<number>/style.md`.

Read what it produces. The style guide is plain markdown you can edit in the panel or in any
text editor, and hand-written corrections work better than re-learning. If a thread is thin
(under ~50 of your messages), use *All my threads* instead to learn your general voice.

**Save a voice for reuse.** *Save as reusable* stores the guide under a name in
`~/.imsg_assist/presets/`. Any new contact can apply it from the dropdown — no API call, no cost.

**Start watching.** The column polls every 4 seconds. When that person texts you, a draft
appears in the thread as a dashed outline where the message would go. Send, Edit, Again, or
Skip. Nothing is sent until you click Send.

### Auto-send

Switch the mode toggle to **Auto-send** and the Start button becomes Arm. Arming asks you to
type the last four digits of the number — deliberate friction, since this is the one action
with no undo.

Once armed, drafts send on their own after a countdown (default 20 seconds) unless you hit
Cancel. Defaults worth keeping:

- **Wait before sending** — your window to catch a bad draft
- **Stop after N replies** — the column disarms itself at the cap
- **Hold times, numbers and promises** — drafts containing a specific time, a day, an amount,
  a number over three digits, a phone number, or a commitment phrase ("i'll", "sounds good")
  skip the countdown and wait for you

That last one matters most. Auto-send is fine for banter and bad at anything the model could
invent. The server also enforces its own floor — 25 seconds between auto-sends and 12 per hour
per contact — so a runaway browser tab can't get around the limits.

Auto-replies are a real thing to hand to another person. Consider whether the people on the
other end would be fine knowing, and start with the delay and the risk-hold on.

## Command line

The same engine works without the browser:

```bash
python3 imsg_assist.py doctor --to +1XXXXXXXXXX   # check access, preview the thread
python3 imsg_assist.py style  --to +1XXXXXXXXXX   # learn a voice (add --global if thin)
python3 imsg_assist.py watch  --to +1XXXXXXXXXX   # draft-and-approve loop
```

Set `IMSG_TO` in your shell to skip `--to`. Style profiles are shared with the web UI.

## Files

| File | Purpose |
|---|---|
| `imsg_assist.py` | Engine — database reads, style learning, drafting, sending |
| `imsg_web.py` | Local HTTP server and API |
| `imsg_ui.html` | The interface |
| `probe.py` | Diagnostics for when reads or Contacts lookups misbehave |

State lives outside the repo, in `~/.imsg_assist/`:

```
~/.imsg_assist/
├── token                     # server token, reused across restarts
├── roster.json               # saved contacts
├── presets/<name>.md         # reusable style guides
└── <number>/
    ├── style.md              # that contact's voice
    └── last_seen.txt         # polling cursor
```

## Model providers

Two API shapes are supported: Anthropic's, and OpenAI's `/v1/chat/completions`. Most providers
speak the second one, so this covers OpenAI, Groq, OpenRouter, Together, DeepSeek, Mistral, and
local servers like Ollama and LM Studio.

**Anthropic** (default):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export IMSG_MODEL=claude-sonnet-5
```

**OpenAI:**

```bash
export OPENAI_API_KEY=sk-...
export IMSG_MODEL=gpt-4o-mini
```

**Any OpenAI-compatible provider** — set the base URL, without the `/v1` suffix:

```bash
export IMSG_PROVIDER=openai
export IMSG_API_KEY=your-key
export IMSG_BASE_URL=https://api.groq.com/openai
export IMSG_MODEL=llama-3.3-70b-versatile
```

**A local model**, which keeps your messages entirely on your machine — no key needed:

```bash
export IMSG_PROVIDER=openai
export IMSG_BASE_URL=http://localhost:11434
export IMSG_MODEL=llama3.1
```

Provider is chosen by: `IMSG_PROVIDER` if set, else `ANTHROPIC_API_KEY` if present, else
`OPENAI_API_KEY` or `IMSG_BASE_URL`, else Anthropic. Keys are read from `IMSG_API_KEY` first,
then the provider's usual variable.

A note on quality: this task is style imitation over a long context, and smaller models tend to
drift toward generic phrasing. Try your preferred model on a few drafts before trusting it, and
remember you can always hand-edit `style.md` to compensate.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Provider key; `IMSG_API_KEY` overrides both |
| `IMSG_PROVIDER` | auto | `anthropic` or `openai` |
| `IMSG_BASE_URL` | provider default | Custom endpoint, no `/v1` suffix |
| `IMSG_MODEL` | `claude-sonnet-5` | Model name for your provider |
| `IMSG_PORT` | `8765` | Change if the port is taken |
| `IMSG_TO` | — | Default contact for the CLI |

## Troubleshooting

**Anything at all — restart the server first.** Python caches imported modules, so editing a
`.py` file changes nothing until the process restarts. `imsg_ui.html` is the exception; that
only needs a browser reload.

**`SSL: CERTIFICATE_VERIFY_FAILED`** — a python.org install ships certificates but doesn't
install them. Run `open "/Applications/Python 3.x/Install Certificates.command"`, matching your
version. Don't disable certificate verification; you'd be sending your messages over an
unauthenticated connection.

**New messages don't appear** — the Messages database uses a write-ahead log, and recent
messages live there until macOS checkpoints them. This is handled, but `probe.py <number>`
will confirm what the reader can see.

**`Errno 24: Too many open files`** — a descriptor leak, fixed in current versions. It can also
surface confusingly as "no usable temporary directory." Restart the server.

**A 501 with your JSON in the error** — an HTTP keep-alive desync from a rejected request,
fixed in current versions. Restart and open the printed URL fresh.

**A contact shows as a number** — that number isn't in your macOS Contacts, or Contacts isn't
readable. Use `+ name` in Browse to label it by hand. `python3 probe.py <number> --contacts`
searches every Contacts database and prints what it finds.

**Drafts don't sound like you** — edit `~/.imsg_assist/<number>/style.md` directly. Concrete
corrections ("never uses exclamation points with this person") work better than re-learning.

## Why macOS only

iMessage has no server-side API. Messages live in a SQLite database on your Mac, and sending
goes through AppleScript talking to Messages.app, which needs a signed-in GUI session. A Linux
VPS has neither. If you want to reach this from your phone, run it on your Mac and connect over
[Tailscale](https://tailscale.com) rather than exposing the port — there's no HTTPS and no
login here, which is fine on `127.0.0.1` and genuinely unsafe on a public IP.

## Privacy and safety

- The Messages database is opened **read-only**. Nothing is written to it.
- Sending is restricted to the one contact a column is bound to.
- Learning a voice sends **your own** sent messages, never the other person's.
- Drafting sends recent thread context, which does include their messages.
- The API key is read from the environment and never written to disk by this tool.
- Nothing is logged or transmitted anywhere except your configured model provider. Point
  `IMSG_BASE_URL` at a local model and nothing leaves the machine at all.

Read the source before granting Full Disk Access to anything, including this.

## License

MIT — see [LICENSE](LICENSE).
