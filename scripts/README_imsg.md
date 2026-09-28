# iMessage CLI (`imsg`) — Notes & Learnings

Documentation for the `imsg` CLI and our wrapper tools.

---

## Tool Chain

```
imsg-firewall → imsg-named → imsg
     ↓              ↓          ↓
  (Haiku       (contact     (raw CLI,
   prompt       resolver)    reads Messages.app
   injection                 SQLite database)
   screening)
```

**Always use `imsg-firewall` for reading** — it's the safe entry point that screens inbound content for prompt injection. For sending, use `imsg send` directly (outbound has no injection risk).

---

## Key Commands

```bash
# List recent chats
imsg-firewall chats --limit 20 --json

# Get message history for a specific chat
imsg-firewall history --chat-id 91 --limit 50 --json

# Refresh contact mapping (if contacts changed)
imsg-firewall --refresh chats --limit 10 --json
```

---

## Field Semantics

### `chats` output

```json
{
  "id": 91,
  "identifier": "+15551234567",
  "name": "",
  "service": "iMessage",
  "last_message_at": "2026-02-15T22:23:33.618000-08:00",
  "resolved_name": "Jordan Lee"
}
```

| Field | Description |
|-------|-------------|
| `id` | **Chat ID** — stable database primary key, never changes |
| `identifier` | Phone number, email, or group chat UUID |
| `name` | Display name (often empty for 1:1 chats) |
| `service` | `iMessage` or `SMS` |
| `resolved_name` | Added by `imsg-named` — contact name from address book |

### `history` output

```json
{
  "id": 207833,
  "chat_id": 91,
  "sender": "+15551234567",
  "is_from_me": true,
  "text": "Hello!",
  "created_at": "2026-02-15T22:23:33.618000-08:00",
  "attachments": [],
  "reactions": []
}
```

| Field | Description |
|-------|-------------|
| `id` | Message ID (database ROWID) |
| `chat_id` | Which chat this message belongs to |
| `sender` | **⚠️ Misleading name!** This is the chat's identifier, NOT who sent the message. Same value for all messages in a 1:1 chat. |
| `is_from_me` | `true` if you sent it, `false` if received |
| `text` | Message content (may contain `\ufffc` for attachment placeholders) |
| `attachments` | Array of attachment metadata |
| `reactions` | Array of tapback reactions |

### Important: `sender` field semantics

In a 1:1 chat, **`sender` is always the other person's identifier**, regardless of `is_from_me`:

```json
{"sender": "+15551234567", "is_from_me": true, "text": "Hi!"}   // you sent this
{"sender": "+15551234567", "is_from_me": false, "text": "Hey!"} // the other person sent this
```

This is why exclusion filtering works — we check `sender` against the exclusion list, and all messages in a conversation with an excluded contact get filtered.

---

## Timestamp Conversion

**Timestamps are automatically converted from UTC to local time.**

The underlying `imsg` CLI outputs timestamps in UTC (e.g., `2026-02-22T06:27:44.088Z`). The `lookup_contact.py` script converts these to local time with timezone offset:

| Before (UTC) | After (Local PST) |
|--------------|-------------------|
| `2026-02-22T06:27:44.088Z` | `2026-02-21T22:27:44.088000-08:00` |

This conversion happens transparently for `created_at` and `last_message_at` fields.

**Implementation:** `scripts/lookup_contact.py` — `utc_to_local()` and `convert_timestamps()` functions.

---

## Chat ID Stability

**Chat IDs are stable and permanent.** They're database primary keys (`ROWID` in `chat` table) assigned when a conversation is first created. Running `imsg history --chat-id 59` today or in 5 years will always refer to the same conversation.

---

## Contact Exclusion

Some contacts have opted out of iMessage data processing. These are listed in:

```
cache/excluded_imsg_contacts.json
```

Format:
```json
{
  "identifiers": ["+15551234567", "someone@email.com"],
  "notes": {
    "+15551234567": "Requested opt-out 2026-02-15"
  }
}
```

**Behavior:**
- `chats` command: Excluded contacts don't appear in the list
- `history` command: Returns an error if the chat belongs to an excluded contact
- **Group chats:** If ANY member of a group chat is excluded, the entire group chat is excluded
- Phone number normalization handles format variations (+1, no +1, 10-digit)
- Email addresses are matched case-insensitively

**Group membership** is tracked in `cache/group_members.json`, built from the Messages database. Run `imsg-named --refresh` to rebuild both contact and group caches.

---

## Group Chats

Group chat identifiers look like:
- `chat47865114366260456` (iMessage)
- `chat480559177346600336` (SMS)

These are UUIDs, not phone numbers. The `name` field may contain the group name if set.

---

## Attachments

Messages with attachments have `text` containing `\ufffc` (object replacement character) and an `attachments` array:

```json
{
  "text": "\ufffc",
  "attachments": [{
    "filename": "~/Library/Messages/Attachments/.../IMG_3534.PNG",
    "mime_type": "image/png",
    "total_bytes": 1011873,
    "missing": false
  }]
}
```

---

## Files

| File | Purpose |
|------|---------|
| `bin/imsg-firewall` | Safe entry point with prompt injection screening |
| `bin/imsg-named` | Wrapper that adds contact name resolution |
| `scripts/lookup_contact.py` | Contact resolution + exclusion filtering |
| `scripts/build_contact_map.py` | Builds contact mapping from macOS Contacts |
| `scripts/build_group_members.py` | Builds group membership cache from Messages DB |
| `cache/contact_mapping.json` | Phone/email → name mapping |
| `cache/group_members.json` | Chat ID → member identifiers mapping |
| `cache/excluded_imsg_contacts.json` | Contacts excluded from processing |

---

## Prompt Injection Firewall

The firewall screens all iMessage content through Claude Haiku before returning it. This protects against messages designed to manipulate the AI assistant.

If content is blocked, you'll see:
```
🛡️ CONTENT BLOCKED BY PROMPT INJECTION GUARD
```

False positives can happen. If needed, inspect messages directly in Messages.app.

---

*Last updated: 2026-02-21*
