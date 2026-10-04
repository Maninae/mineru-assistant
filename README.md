<p align="center">
  <img src="assets/banner/banner.png" alt="mineru: the aurora fox logo, its neon strokes filled with mineru CLI commands, beside a terminal running mineru setup, memory warm-resume, and cron status" width="820">
</p>

<p align="center">Your own AI assistant, running on your Mac. It reads your mail, calendar, and texts, remembers what matters, and messages you on Telegram.</p>

<p align="center">
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue.svg"></a>
  <img alt="Platform: macOS" src="https://img.shields.io/badge/platform-macOS-lightgrey.svg">
</p>

---

Every morning it sends you something like this:

> 🌤️ **Tuesday, Oct 6**: light day, one errand.
> ☀️ 64°F
>
> 📅 9:30 AM: dentist (Market St, leave by 9:05)
> 📅 7:00 PM: dinner with Sam, Thai place downtown
> 📅 **Tomorrow:** car service drop-off, 8:00 AM
>
> 💬 **Needs your attention:**
> - 🗓️ **Priya**: asked about the 18th or the 19th for brunch. Pick one so it sticks.
> - 📦 **Package**: the lamp arrived at the office, not home. Front desk has it.
> - 🧾 **Dentist**: new patient form still unsigned; it's in your inbox from Friday.
>
> 🫧 **Skippable:** two newsletters, a shipping confirmation, a statement notice.

And from the terminal:

```text
❯ mineru setup                      # one guided first run: profile, install, secrets
❯ mineru memory warm-resume         # what happened in the last three days
❯ mineru cron status                # which briefs run when, and whether they're healthy
```

---

## Get it

```bash
git clone https://github.com/Maninae/mineru-assistant.git
cd mineru-assistant && python3 -m venv .venv && source .venv/bin/activate
pip install -e .
mineru setup
```

`mineru setup` creates your private profile (name, persona, time zone), installs the engine into `~/.mineru`, and walks you through the secrets it needs. Scheduled briefs run through Claude Code on macOS `launchd`; the Telegram side needs a bot token from BotFather.

## What it does

- **Briefs on a schedule**: morning brief, news digest, inbox triage, weekly transactions, a daily curiosity question. Keep the ones you want, drop the rest.
- **Memory that lasts**: daily logs consolidate into long-term notes, searchable from the CLI or by the assistant itself.
- **Your accounts, read carefully**: Gmail, Google Calendar, Drive, iMessage, Slack, a browser, your finances. Reads go through a prompt-injection firewall; writes wait for your go.
- **Private by construction**: this repo is the engine. Your identity, memory, and secrets live in a separate folder you own and never touch this code.
- **More than one assistant per Mac**: each profile gets its own persona, memory, and bot.

> [!NOTE]
> Status: runs the author's assistant every day. Onboarding a fresh operator through `mineru setup` is the part still being smoothed; expect rough edges there.

---

**[Architecture](./ARCHITECTURE.md)** · **[Contributing](./CONTRIBUTING.md)**

MIT. See [LICENSE](./LICENSE).
