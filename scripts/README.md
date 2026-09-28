# Scripts

Operational scripts and CLI tools.

## Structure

```
scripts/
├── script.py              ← Single-file utilities
├── script.sh
├── tool-name/             ← Multi-file tools (subdirectory)
│   ├── tool-name          ← Entry point
│   ├── *.py               ← Modules
│   ├── lib.py             ← Shared code
│   └── .venv/             ← Dependencies (if needed)
└── firewall/              ← Prompt injection firewall
```

## Current Scripts

| Script | Purpose |
|--------|---------|
| `build_contact_map.py` | Export macOS Contacts to JSON |
| `lookup_contact.py` | Resolve phone/email to contact name |
| `deliver-output.py` | Send file contents to the operator's primary chat channel (configurable via `DELIVERY_CHANNEL` env var) |
| `concat-memory.py` | Concatenate memory files for export |
| `collect-daily-sessions.sh` | List daily session files |

## Multi-File Tools

| Tool | Purpose | Entry Point |
|------|---------|-------------|
| `monarch/` | Monarch Money finance CLI | `bin/monarch` → `scripts/monarch/monarch` |
| `firewall/` | Prompt injection screening | Used by `bin/*-firewall` wrappers |

## Adding Scripts

- **Single-file**: Just add `scripts/my-script.py`
- **Multi-file**: Create `scripts/my-tool/` with entry point, then symlink from `bin/`

If a tool needs dependencies, create a `.venv` inside its subdirectory.
