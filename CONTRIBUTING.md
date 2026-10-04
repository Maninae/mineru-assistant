# Contributing

## Set up a dev checkout

```bash
git clone https://github.com/Maninae/mineru-assistant.git
cd mineru-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

The `mineru` command lands on the venv's PATH. The suite runs on any machine with no configuration: it rehearses a full install against the synthetic profile in `fixtures/`.

## Ground rules

- **Nothing personal in the engine.** No real names, paths, account ids, calendar ids, or phone numbers, in code, tests, fixtures, or templates. The leak gates in `tests/` fail the build on any hit; see [ARCHITECTURE.md](./ARCHITECTURE.md) for how they load your own banned literals.
- **Generic over situational.** A job or connector belongs in the engine only if a second operator would plausibly want it. Anything specific to one person's life lives in that person's private profile.
- **Templates, not renders.** Charter, prompt, recurring, and launchd files are `.template` sources under `engine/`; rendered copies in a workspace are ephemeral.
- **Python 3.9 compatible** across the runtime: some components run under the system interpreter.
- **Tests with every change.** Add or update the pytest coverage for what you touch; the suite must stay green.

## Where things live

The module map and the install flow are in [ARCHITECTURE.md](./ARCHITECTURE.md). Each package directory with more than a couple of files carries its own `CLAUDE.md` describing its responsibilities and how to extend it.
