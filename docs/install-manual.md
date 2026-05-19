# Manual installation (step-by-step)

The recommended path is `./scripts/install.sh` — see [README.md § Installation](../README.md#installation). Use this manual procedure only if:

- You're auditing what the installer does before letting it run
- You're on a locked-down or air-gapped machine where the installer can't fetch dependencies
- You're contributing changes to the installer itself and want to bisect a step
- You're porting the project to a Linux/Windows host and need to know which steps are macOS-specific

The procedure assumes a fresh macOS shell with Homebrew already present.

---

## Prerequisites

```bash
# Homebrew formulas
brew install python@3.12 node libreoffice poppler portaudio expat

# Casks (skip if .app already present)
brew install --cask google-chrome

# Microsoft PowerPoint 2016+ must be installed separately. After install,
# grant Automation permission:
#   System Settings → Privacy & Security → Automation
#   → enable PowerPoint for your terminal (Terminal/iTerm/Warp/...)
# This permission is needed by AppleScript-driven tools (navigate_slide,
# control_slideshow, switch_window).

# AWS credentials (env vars, ~/.aws/credentials, named profile, or IAM role)
aws configure

# Enable Bedrock model access in your region:
#   amazon.nova-2-sonic-v1:0                     (the voice model)
#   us.anthropic.claude-haiku-4-5-20251001-v1:0  (slide vision + fast transforms)
#   us.anthropic.claude-sonnet-4-6                (executive summary)
#   us.amazon.nova-2-lite-v1:0                    (intent classification)
# AWS Console → Bedrock → Model access → Enable.
```

> **Why `expat`?** Homebrew's current `python@3.12` is linked against a newer `libexpat` than the system's `/usr/lib/libexpat.1.dylib` ships. Without the Homebrew copy on `DYLD_LIBRARY_PATH`, `xml.parsers.expat` (and therefore `plistlib`, `xmlrpc`, and any XML-heavy library) fails at import with `Symbol not found: _XML_SetAllocTrackerActivationThreshold`. The scripted installer bakes the fix into the venv's `activate` script; the manual procedure threads `DYLD_LIBRARY_PATH` through every command instead.

---

## Setup

```bash
# Python venv (DYLD_LIBRARY_PATH is required because of the expat issue above)
DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib" \
  "$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
source .venv/bin/activate

# Python packages
DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib" pip install -r requirements.txt
DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib" playwright install chromium

# Node subprojects
(cd websocket-server && npm install)
(cd visor            && npm install)

# Environment template
cp .env.example .env
# then edit .env and fill in (at minimum):
#   FINALYSIS_API_KEY=<your key>          # if running the bundled financial specialist
#   AWS_REGION=us-east-1                  # whatever region has Nova Sonic enabled
```

After this, `./start.sh path/to/deck.pptx` works exactly as documented in the README.

---

## Skipping the `DYLD_LIBRARY_PATH` plumbing

You can avoid threading `DYLD_LIBRARY_PATH` through every Python command by running the installer once:

```bash
./scripts/install.sh
```

It bakes `DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib"` into `.venv/bin/activate`, so every subsequent `source .venv/bin/activate` (and every `start.sh` invocation, which sources `.venv/bin/activate` defensively) picks it up automatically.

If you've completed the manual steps above and want to bake the fix in retroactively without re-running the installer, append this to `.venv/bin/activate` (replace `$(brew --prefix expat)/lib` with the literal path):

```bash
# Bake expat into the venv so plistlib/xmlrpc/any XML library can import.
export DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib${DYLD_LIBRARY_PATH:+:${DYLD_LIBRARY_PATH}}"
```

---

## macOS Accessibility (optional, for the spacebar mute hotkey)

The global spacebar mute hotkey + floating "Live AI" indicator (see README § Stage ergonomics) require Accessibility permission for whatever terminal launches the helper:

- System Settings → Privacy & Security → Accessibility → toggle ON for your terminal app

If you skip this:
- The helper still starts (visible in `logs/mute_helper.log`)
- Spacebar from non-voice-UI apps will not be intercepted
- The floating overlay will not appear
- The Mute button in `localhost:3000` continues working

You'll see a clear instruction in `start.sh`'s phase 7/7 output if Accessibility is missing.

---

## Verifying the install

```bash
# Python deps + entrypoint
PYTHONPATH=. .venv/bin/python -c "import src.api_server; print('api_server: OK')"

# Bedrock probe (uses your real credentials — should print 3 'ok's after start.sh)
./start.sh path/to/any.pptx
curl -s http://localhost:8000/diagnose | jq '.bedrock.checks'
./stop.sh
```

If anything fails, see README § Operations & troubleshooting for the most common symptoms and fixes.
