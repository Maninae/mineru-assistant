#!/usr/bin/env python3
"""
Test suite for gog_cleaner module.

TDD approach: these tests define expected behavior before implementation.
Run with: python3 -m pytest scripts/firewall/test_gog_cleaner.py -v
Or: python3 scripts/firewall/test_gog_cleaner.py
"""
import json
import unittest
from pathlib import Path


# ============================================================================
# Test Fixtures - Real gog output samples
# ============================================================================

GMAIL_SEARCH_OUTPUT = {
    "nextPageToken": "04792200974035332325",
    "threads": [
        {
            "id": "19c64d49aa68fd3a",
            "date": "2026-02-15 21:03",
            "from": "\"OPF Energy, LLC\" <receipts@stripe.com>",
            "subject": "Your OPF Energy, LLC receipt [#1676-6663]",
            "labels": ["EV Charging", "IMPORTANT", "CATEGORY_UPDATES"],
            "messageCount": 1
        }
    ]
}

GMAIL_GET_OUTPUT = {
    "body": "Receipt from OPF Energy, LLC\nAmount paid: $4.75",
    "headers": {
        "bcc": "",
        "cc": "",
        "date": "Mon, 16 Feb 2026 05:03:00 +0000",
        "from": "\"OPF Energy, LLC\" <receipts@stripe.com>",
        "subject": "Your OPF Energy, LLC receipt [#1676-6663]",
        "to": "test-user@example.com"
    },
    "message": {
        "historyId": "13086021",
        "id": "19c64d49aa68fd3a",
        "internalDate": "1771218180000",
        "labelIds": ["Label_7436828567856003648", "IMPORTANT"],
        "payload": {
            "body": {},
            "headers": [
                {"name": "Delivered-To", "value": "test-user@example.com"},
                {"name": "Received", "value": "by 2002:a05:6802..."},
                {"name": "DKIM-Signature", "value": "v=1; a=rsa-sha256..."},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "body": {"data": "base64encodedcontent...", "size": 745},
                    "mimeType": "text/plain",
                    "partId": "0"
                }
            ]
        },
        "sizeEstimate": 53621,
        "snippet": "Receipt from OPF Energy, LLC Amount paid $4.75",
        "threadId": "19c64d49aa68fd3a"
    }
}

CALENDAR_EVENTS_OUTPUT = {
    "events": [
        {
            "created": "2025-09-19T22:50:08.000Z",
            "creator": {
                "email": "test-user@example.com",
                "self": True
            },
            "attendees": [
                {
                    "email": "guest@example.com",
                    "displayName": "Guest Person",
                    "responseStatus": "accepted",
                    "self": False,
                    "id": "att123",
                    "organizer": False
                }
            ],
            "end": {
                "dateTime": "2026-02-16T10:30:00-08:00",
                "timeZone": "America/Los_Angeles"
            },
            "etag": "\"3527335212529854\"",
            "eventType": "default",
            "htmlLink": "https://www.google.com/calendar/event?eid=abc123",
            "iCalUID": "v1rc86asda74tj3mbcs9tktgq2@google.com",
            "id": "v1rc86asda74tj3mbcs9tktgq2_20260216T170000Z",
            "kind": "calendar#event",
            "organizer": {
                "displayName": "Personal Calendar",
                "email": "cal@group.calendar.google.com",
                "self": True
            },
            "originalStartTime": {
                "dateTime": "2026-02-16T09:00:00-08:00",
                "timeZone": "America/Los_Angeles"
            },
            "recurringEventId": "v1rc86asda74tj3mbcs9tktgq2",
            "reminders": {"useDefault": True},
            "sequence": 1,
            "start": {
                "dateTime": "2026-02-16T09:00:00-08:00",
                "timeZone": "America/Los_Angeles"
            },
            "status": "confirmed",
            "summary": "OW - Allergy shot walk-in",
            "updated": "2025-11-20T19:40:06.264Z",
            "location": "123 Medical Center Dr"
        }
    ]
}


# ============================================================================
# Test Cases
# ============================================================================

class TestGogCleanerConfig(unittest.TestCase):
    """Test that the config file is valid and complete."""
    
    def setUp(self):
        config_path = Path(__file__).parent / "gog_cleaner_config.json"
        with open(config_path) as f:
            self.config = json.load(f)
    
    def test_config_has_required_services(self):
        """Config should define rules for all major gog services."""
        required = ["gmail", "calendar", "drive", "contacts"]
        for service in required:
            self.assertIn(service, self.config, f"Missing service: {service}")
    
    def test_config_has_command_classification(self):
        """Config should classify read vs write commands."""
        self.assertIn("command_classification", self.config)
        classification = self.config["command_classification"]
        self.assertIn("read_commands", classification)
        self.assertIn("write_commands", classification)
    
    def test_gmail_search_preserves_essential_fields(self):
        """Gmail search should keep id, date, from, subject."""
        gmail_search = self.config["gmail"]["search"]
        essential = ["id", "date", "from", "subject"]
        for field in essential:
            self.assertIn(field, gmail_search["keep"], f"Missing essential field: {field}")
    
    def test_gmail_get_drops_payload(self):
        """Gmail get should drop the huge payload/MIME structure."""
        gmail_get = self.config["gmail"]["get"]
        self.assertIn("payload", gmail_get["drop"])
    
    def test_calendar_drops_etag(self):
        """Calendar should drop etag, kind, and other metadata."""
        cal_events = self.config["calendar"]["events"]
        for field in ["etag", "kind", "iCalUID"]:
            self.assertIn(field, cal_events["drop"], f"Should drop: {field}")
    
    def test_calendar_keeps_essential_fields(self):
        """Calendar should keep summary, start, end, location."""
        cal_events = self.config["calendar"]["events"]
        essential = ["summary", "start", "end", "location", "id"]
        for field in essential:
            self.assertIn(field, cal_events["keep"], f"Missing essential: {field}")


class TestGogCleanerGmail(unittest.TestCase):
    """Test Gmail cleaning functionality."""
    
    def test_search_output_unchanged(self):
        """Gmail search output is already fairly clean, should pass through mostly unchanged."""
        from gog_cleaner import clean_gmail_search
        
        result = clean_gmail_search(GMAIL_SEARCH_OUTPUT)
        
        # Essential fields preserved
        self.assertIn("threads", result)
        self.assertEqual(len(result["threads"]), 1)
        thread = result["threads"][0]
        self.assertEqual(thread["id"], "19c64d49aa68fd3a")
        self.assertEqual(thread["from"], "\"OPF Energy, LLC\" <receipts@stripe.com>")
    
    def test_get_removes_payload(self):
        """Gmail get should remove the MIME payload structure but KEEP the
        top-level body as plain text (HTML stripped if needed). Full original
        body still available via --raw."""
        from gog_cleaner import clean_gmail_get

        result = clean_gmail_get(GMAIL_GET_OUTPUT)

        # Top-level body KEPT (as plain text)
        self.assertIn("body", result)
        self.assertIsInstance(result["body"], str)
        # No HTML markup should leak through
        self.assertNotIn("<", result["body"])
        self.assertNotIn(">", result["body"])
        # Real content preserved
        self.assertIn("$4.75", result["body"])

        # Headers simplified
        self.assertIn("headers", result)
        self.assertIn("from", result["headers"])

        # Message metadata cleaned, snippet kept
        self.assertIn("message", result)
        self.assertNotIn("payload", result["message"])
        self.assertNotIn("historyId", result["message"])
        self.assertNotIn("internalDate", result["message"])
        self.assertIn("snippet", result["message"])
        self.assertIn("$4.75", result["message"]["snippet"])

    def test_get_strips_html_from_body(self):
        """If gog hands us HTML in the top-level body (defensive), we strip it."""
        from gog_cleaner import clean_gmail_get
        sample = {
            "body": "<html><body><p>Hello <b>world</b></p><script>evil()</script></body></html>",
            "headers": {"from": "x@y.com"},
            "message": {"id": "abc", "snippet": "Hello world"},
        }
        result = clean_gmail_get(sample)
        self.assertIn("body", result)
        # Tags + script contents gone
        self.assertNotIn("<", result["body"])
        self.assertNotIn("evil", result["body"])
        # Readable text survived
        self.assertIn("Hello", result["body"])
        self.assertIn("world", result["body"])

    def test_get_body_truncated_when_huge(self):
        """A pathologically huge body is capped with a clear truncation marker."""
        from gog_cleaner import clean_gmail_get, BODY_MAX_CHARS
        big_text = "X" * (BODY_MAX_CHARS + 5000)
        sample = {"body": big_text, "message": {"id": "abc", "snippet": "x"}}
        result = clean_gmail_get(sample)
        self.assertIn("body", result)
        self.assertLessEqual(len(result["body"]), BODY_MAX_CHARS + 200)
        self.assertIn("truncated", result["body"])
        self.assertIn("--raw", result["body"])
    
    def test_get_preserves_snippet(self):
        """Gmail get should preserve the snippet field."""
        from gog_cleaner import clean_gmail_get
        
        result = clean_gmail_get(GMAIL_GET_OUTPUT)
        
        # Snippet should be accessible (either at top level or in message)
        snippet_found = (
            result.get("snippet") or 
            (result.get("message", {}).get("snippet"))
        )
        self.assertTrue(snippet_found)


GMAIL_THREAD_GET_OUTPUT = {
    "downloaded": None,
    "thread": {
        "historyId": "13649671",
        "id": "19ed5b3ce9e52589",
        "messages": [
            {
                "historyId": "13649671",
                "id": "msg1",
                "threadId": "19ed5b3ce9e52589",
                "internalDate": "1781701791000",
                "sizeEstimate": 55754,
                "labelIds": ["UNREAD", "CATEGORY_UPDATES"],
                "snippet": "Your points price alerts are available!",
                "payload": {
                    "mimeType": "text/html",
                    "body": {"data": "PGh0bWw+" * 5000, "size": 49953},  # huge base64
                    "headers": [
                        {"name": "Delivered-To", "value": "test-user@example.com"},
                        {"name": "ARC-Seal", "value": "v=1; a=rsa-sha256..."},
                        {"name": "DKIM-Signature", "value": "v=1; a=rsa..."},
                        {"name": "From", "value": "Alerts <alerts@example.com>"},
                        {"name": "To", "value": "other-user@example.com"},
                        {"name": "Subject", "value": "Price alert SFO-HKG"},
                        {"name": "Date", "value": "Mon, 16 Jun 2026 12:00:00 +0000"},
                    ],
                },
            },
            {
                "historyId": "13650563",
                "id": "msg2",
                "threadId": "19ed5b3ce9e52589",
                "internalDate": "1781726797000",
                "sizeEstimate": 54940,
                "labelIds": ["UNREAD"],
                "snippet": "Second alert",
                "payload": {
                    "mimeType": "text/html",
                    "body": {"data": "PGh0bWw+" * 5000, "size": 49142},
                    "headers": [
                        {"name": "From", "value": "Alerts <alerts@example.com>"},
                        {"name": "Subject", "value": "Re: alert"},
                    ],
                },
            },
        ],
    },
}


class TestGogCleanerGmailThreadGet(unittest.TestCase):
    """Test gmail thread get cleaning."""

    def test_thread_get_drops_payload_body(self):
        from gog_cleaner import clean_gmail_thread_get
        result = clean_gmail_thread_get(GMAIL_THREAD_GET_OUTPUT)
        for msg in result["thread"]["messages"]:
            self.assertNotIn("payload", msg, "payload (with base64 body) should be dropped")

    def test_thread_get_keeps_id_and_snippet(self):
        from gog_cleaner import clean_gmail_thread_get
        result = clean_gmail_thread_get(GMAIL_THREAD_GET_OUTPUT)
        for msg in result["thread"]["messages"]:
            self.assertIn("id", msg)
            self.assertIn("snippet", msg)

    def test_thread_get_extracts_plain_body(self):
        """Walker should prefer text/plain part and decode it directly (no
        conversion). Body must be present as plain text, no tags, no base64."""
        import base64 as _b64
        from gog_cleaner import clean_gmail_thread_get
        plain_text = "Hello Sam,\r\n\r\nYour order ships tomorrow.\r\nThanks!"
        encoded = _b64.urlsafe_b64encode(plain_text.encode("utf-8")).decode("ascii").rstrip("=")
        fixture = {
            "thread": {
                "id": "t1",
                "messages": [{
                    "id": "m1", "snippet": "Your order ships",
                    "payload": {
                        "mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": "shop@example.com"},
                                    {"name": "Subject", "value": "Order"}],
                        "parts": [
                            {"mimeType": "text/plain",
                             "body": {"data": encoded, "size": len(plain_text)}},
                            {"mimeType": "text/html",
                             "body": {"data": "PGh0bWw+", "size": 6}},
                        ],
                    },
                }],
            },
        }
        result = clean_gmail_thread_get(fixture)
        msg = result["thread"]["messages"][0]
        self.assertIn("body", msg)
        self.assertIn("Your order ships tomorrow", msg["body"])
        # No tags, no base64 leakage
        self.assertNotIn("<", msg["body"])
        self.assertNotIn("PGh0bWw", msg["body"])

    def test_thread_get_extracts_html_body_when_no_plain(self):
        """If only text/html exists, convert HTML→text."""
        import base64 as _b64
        from gog_cleaner import clean_gmail_thread_get
        html = "<html><body><h1>Sale!</h1><p>50% off everything.</p></body></html>"
        encoded = _b64.urlsafe_b64encode(html.encode("utf-8")).decode("ascii").rstrip("=")
        fixture = {
            "thread": {
                "id": "t1",
                "messages": [{
                    "id": "m1", "snippet": "Sale",
                    "payload": {
                        "mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": "x@y.com"}],
                        "parts": [
                            {"mimeType": "text/html",
                             "body": {"data": encoded, "size": len(html)}},
                        ],
                    },
                }],
            },
        }
        result = clean_gmail_thread_get(fixture)
        msg = result["thread"]["messages"][0]
        self.assertIn("body", msg)
        self.assertIn("Sale", msg["body"])
        self.assertIn("50% off", msg["body"])
        self.assertNotIn("<", msg["body"])
        self.assertNotIn(">", msg["body"])

    def test_thread_get_extracts_html_body_with_head_meta_link(self):
        """Real HTML emails carry <meta>/<link> in <head>. Void elements have no
        end tag, so they must not poison the drop-depth counter (the June 2026
        regression: every marketing/transactional email came back body-less)."""
        import base64 as _b64
        from gog_cleaner import clean_gmail_thread_get
        html = (
            '<html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width">'
            '<link rel="stylesheet" href="x.css"><title>Receipt</title>'
            "<style>.a{color:red}</style></head>"
            "<body><p>We processed your AutoPay payment of $123.45.</p></body></html>"
        )
        encoded = _b64.urlsafe_b64encode(html.encode("utf-8")).decode("ascii").rstrip("=")
        fixture = {
            "thread": {
                "id": "t1",
                "messages": [{
                    "id": "m1", "snippet": "We processed",
                    "payload": {
                        "mimeType": "text/html",
                        "headers": [{"name": "From", "value": "x@y.com"}],
                        "body": {"data": encoded, "size": len(html)},
                    },
                }],
            },
        }
        result = clean_gmail_thread_get(fixture)
        msg = result["thread"]["messages"][0]
        self.assertIn("body", msg)
        self.assertIn("AutoPay payment of $123.45", msg["body"])
        # head-section noise must stay out of the text
        self.assertNotIn("color:red", msg["body"])
        self.assertNotIn("Receipt", msg["body"])

    def test_html_to_text_body_tag_resets_unclosed_head_tags(self):
        """An unclosed <title>/<head> must not suppress the body: the <body>
        start tag resets the drop depth."""
        from gog_cleaner import html_to_text
        html = "<html><head><title>Oops no close tag<body><p>Visible text</p></body></html>"
        self.assertIn("Visible text", html_to_text(html))

    def test_thread_get_body_truncated_when_huge(self):
        """Body cap applies in thread_get too."""
        import base64 as _b64
        from gog_cleaner import clean_gmail_thread_get, BODY_MAX_CHARS
        huge = "Line of email text. " * 2000  # ~40K chars
        encoded = _b64.urlsafe_b64encode(huge.encode("utf-8")).decode("ascii").rstrip("=")
        fixture = {
            "thread": {
                "id": "t1",
                "messages": [{
                    "id": "m1", "snippet": "x",
                    "payload": {
                        "parts": [{"mimeType": "text/plain",
                                   "body": {"data": encoded, "size": len(huge)}}],
                    },
                }],
            },
        }
        result = clean_gmail_thread_get(fixture)
        msg = result["thread"]["messages"][0]
        self.assertIn("body", msg)
        self.assertLessEqual(len(msg["body"]), BODY_MAX_CHARS + 200)
        self.assertIn("truncated", msg["body"])

    def test_thread_get_synthesizes_compact_headers(self):
        from gog_cleaner import clean_gmail_thread_get
        result = clean_gmail_thread_get(GMAIL_THREAD_GET_OUTPUT)
        msg0 = result["thread"]["messages"][0]
        self.assertIn("headers", msg0)
        h = msg0["headers"]
        # Synthesized from payload.headers, lowercased keys
        self.assertEqual(h.get("from"), "Alerts <alerts@example.com>")
        self.assertEqual(h.get("subject"), "Price alert SFO-HKG")
        self.assertEqual(h.get("date"), "Mon, 16 Jun 2026 12:00:00 +0000")
        # Transport-level headers must not leak through
        self.assertNotIn("delivered-to", h)
        self.assertNotIn("arc-seal", h)
        self.assertNotIn("dkim-signature", h)

    def test_thread_get_drops_redundant_per_message_fields(self):
        from gog_cleaner import clean_gmail_thread_get
        result = clean_gmail_thread_get(GMAIL_THREAD_GET_OUTPUT)
        for msg in result["thread"]["messages"]:
            for f in ("historyId", "internalDate", "sizeEstimate",
                      "labelIds", "threadId"):
                self.assertNotIn(f, msg, "{} should be dropped".format(f))

    def test_thread_get_huge_size_reduction(self):
        """Cleaning a 4-msg HTML thread should drop > 90% of bytes."""
        import json as _j
        from gog_cleaner import clean_gmail_thread_get
        original = len(_j.dumps(GMAIL_THREAD_GET_OUTPUT))
        cleaned = len(_j.dumps(clean_gmail_thread_get(GMAIL_THREAD_GET_OUTPUT)))
        reduction = (original - cleaned) / original
        self.assertGreater(reduction, 0.9,
            "Expected >90% reduction, got {:.1%}".format(reduction))

    def test_dispatch_gmail_thread_get(self):
        """clean() should dispatch 'gmail thread get <id>' to the thread cleaner."""
        import json as _j
        from gog_cleaner import clean
        result = clean(
            ["gmail", "thread", "get", "19ed5b3ce9e52589", "--json"],
            _j.dumps(GMAIL_THREAD_GET_OUTPUT),
        )
        parsed = _j.loads(result)
        # payload (with base64) must be gone
        for msg in parsed["thread"]["messages"]:
            self.assertNotIn("payload", msg)


class TestGogCleanerCalendar(unittest.TestCase):
    """Test Calendar cleaning functionality."""
    
    def test_events_removes_metadata(self):
        """Calendar events should remove etag, kind, iCalUID, etc."""
        from gog_cleaner import clean_calendar_events
        
        result = clean_calendar_events(CALENDAR_EVENTS_OUTPUT)
        
        self.assertIn("events", result)
        event = result["events"][0]
        
        # These should be removed
        self.assertNotIn("etag", event)
        self.assertNotIn("kind", event)
        self.assertNotIn("iCalUID", event)
        self.assertNotIn("recurringEventId", event)
        self.assertNotIn("sequence", event)
        self.assertNotIn("updated", event)
        self.assertNotIn("created", event)
    
    def test_events_drops_dead_fields(self):
        """Calendar drops htmlLink/eventType/conferenceData (browser links,
        near-constant, heavy Meet blobs) but KEEPS creator/organizer/attendees
        trimmed to identity essentials (email/displayName/+responseStatus)."""
        from gog_cleaner import clean_calendar_events
        result = clean_calendar_events(CALENDAR_EVENTS_OUTPUT)
        event = result["events"][0]
        # Still dropped entirely
        for f in ("htmlLink", "eventType", "conferenceData", "etag", "kind"):
            self.assertNotIn(f, event, "Should drop: {}".format(f))
        # creator/organizer KEPT but trimmed (email/displayName, no self/id)
        self.assertEqual(event["creator"].get("email"), "test-user@example.com")
        self.assertNotIn("self", event["creator"])
        self.assertIn("organizer", event)
        self.assertNotIn("self", event["organizer"])
        # attendees KEPT but trimmed (email/displayName/responseStatus, no self/id)
        att = event["attendees"][0]
        self.assertEqual(att.get("email"), "guest@example.com")
        self.assertEqual(att.get("responseStatus"), "accepted")
        self.assertNotIn("self", att)
        self.assertNotIn("id", att)

    def test_events_keeps_essential_fields(self):
        """Calendar events should keep summary, start, end, location, id."""
        from gog_cleaner import clean_calendar_events
        
        result = clean_calendar_events(CALENDAR_EVENTS_OUTPUT)
        event = result["events"][0]
        
        self.assertEqual(event["summary"], "OW - Allergy shot walk-in")
        self.assertEqual(event["location"], "123 Medical Center Dr")
        self.assertIn("start", event)
        self.assertIn("end", event)
        self.assertIn("id", event)
    
    def test_events_simplifies_datetime(self):
        """Start/end should drop timeZone, keep dateTime."""
        from gog_cleaner import clean_calendar_events

        result = clean_calendar_events(CALENDAR_EVENTS_OUTPUT)
        event = result["events"][0]

        # timeZone should be dropped from start/end
        self.assertNotIn("timeZone", event.get("start", {}))
        self.assertNotIn("timeZone", event.get("end", {}))

        # dateTime should be preserved
        self.assertIn("dateTime", event["start"])

    def test_singular_event_shape_cleaned(self):
        """
        `calendar event <calId> <eventId>` returns {"event": {one_event}}
        (a dict, not a list). clean_calendar_events must apply the same
        drops/trims as the list shape, and pass sibling keys through.
        """
        from gog_cleaner import clean_calendar_events

        # Reuse the existing event dict from the list fixture so the
        # singular-shape assertions stay in lockstep with the list-shape ones.
        singular_input = {
            "downloaded": None,  # sibling key — must pass through unchanged
            "event": CALENDAR_EVENTS_OUTPUT["events"][0],
        }

        result = clean_calendar_events(singular_input)

        # Sibling key preserved verbatim
        self.assertIn("downloaded", result)
        self.assertIsNone(result["downloaded"])

        # Singular event present under the same key
        self.assertIn("event", result)
        self.assertNotIn("events", result)  # didn't accidentally pluralize
        event = result["event"]
        self.assertIsInstance(event, dict)

        # Same DROPS as the list path
        for f in ("etag", "kind", "iCalUID", "htmlLink", "eventType",
                  "recurringEventId", "originalStartTime", "sequence",
                  "reminders", "updated", "created"):
            self.assertNotIn(f, event, "Singular should drop: {}".format(f))

        # Same TRIMS as the list path
        self.assertEqual(event["creator"].get("email"), "test-user@example.com")
        self.assertNotIn("self", event["creator"])
        self.assertNotIn("self", event["organizer"])
        self.assertNotIn("timeZone", event["start"])
        self.assertNotIn("timeZone", event["end"])
        self.assertIn("dateTime", event["start"])

        # Same KEEPS as the list path
        self.assertEqual(event["summary"], "OW - Allergy shot walk-in")
        self.assertEqual(event["location"], "123 Medical Center Dr")
        self.assertIn("id", event)
        self.assertEqual(event["status"], "confirmed")

        # Attendees trimmed (email/displayName/responseStatus only)
        att = event["attendees"][0]
        self.assertEqual(att.get("email"), "guest@example.com")
        self.assertNotIn("self", att)
        self.assertNotIn("id", att)

    def test_dispatch_singular_calendar_event(self):
        """
        clean() dispatcher must route `calendar event <calId> <eventId>` to
        clean_calendar_events. Previously only `calendar events` (plural) was
        routed, so singular reads returned the full noisy blob.
        """
        from gog_cleaner import clean

        singular_input = {
            "downloaded": None,
            "event": CALENDAR_EVENTS_OUTPUT["events"][0],
        }
        raw = json.dumps(singular_input)
        out = clean(
            ["calendar", "event",
             "personal-cal-abc123@group.calendar.google.com",
             "v1rc86asda74tj3mbcs9tktgq2_20260216T170000Z",
             "--json"],
            raw,
        )
        parsed = json.loads(out)
        self.assertIn("event", parsed)
        # Proof the dispatcher actually invoked the cleaner — etag would still
        # be present if it had fallen through unchanged.
        self.assertNotIn("etag", parsed["event"])
        self.assertNotIn("htmlLink", parsed["event"])
        self.assertNotIn("self", parsed["event"]["creator"])


class TestGogCleanerCommandClassification(unittest.TestCase):
    """Test command type detection (read vs write)."""
    
    def test_gmail_search_is_read(self):
        """gmail search should be classified as read."""
        from gog_cleaner import is_read_command
        
        self.assertTrue(is_read_command(["gmail", "search", "from:boss"]))
    
    def test_gmail_send_is_write(self):
        """gmail send should be classified as write."""
        from gog_cleaner import is_read_command
        
        self.assertFalse(is_read_command(["gmail", "send", "--to", "x@y.com"]))
    
    def test_calendar_events_is_read(self):
        """calendar events should be classified as read."""
        from gog_cleaner import is_read_command
        
        self.assertTrue(is_read_command(["calendar", "events", "--days", "7"]))
    
    def test_calendar_create_is_write(self):
        """calendar create should be classified as write."""
        from gog_cleaner import is_read_command
        
        self.assertFalse(is_read_command(["calendar", "create", "Meeting"]))
    
    def test_unknown_command_defaults_to_read(self):
        """Unknown commands should default to read (fail safe)."""
        from gog_cleaner import is_read_command
        
        # Unknown service
        self.assertTrue(is_read_command(["unknownservice", "action"]))
        
        # Unknown action
        self.assertTrue(is_read_command(["gmail", "unknownaction"]))
    
    def test_empty_args_defaults_to_read(self):
        """Empty or minimal args should default to read (fail safe)."""
        from gog_cleaner import is_read_command
        
        self.assertTrue(is_read_command([]))
        self.assertTrue(is_read_command(["gmail"]))
    
    def test_nested_gmail_labels_list_is_read(self):
        """gmail labels list should be classified as read."""
        from gog_cleaner import is_read_command
        
        self.assertTrue(is_read_command(["gmail", "labels", "list"]))
    
    def test_nested_gmail_labels_create_is_write(self):
        """gmail labels create should be classified as write."""
        from gog_cleaner import is_read_command
        
        self.assertFalse(is_read_command(["gmail", "labels", "create"]))
    
    def test_nested_gmail_drafts_list_is_read(self):
        """gmail drafts list should be classified as read."""
        from gog_cleaner import is_read_command
        
        self.assertTrue(is_read_command(["gmail", "drafts", "list"]))
    
    def test_nested_gmail_drafts_send_is_write(self):
        """gmail drafts send should be classified as write."""
        from gog_cleaner import is_read_command
        
        self.assertFalse(is_read_command(["gmail", "drafts", "send"]))
    
    def test_drive_ls_is_read(self):
        """drive ls should be classified as read (corrected command name)."""
        from gog_cleaner import is_read_command
        
        self.assertTrue(is_read_command(["drive", "ls"]))
    
    def test_drive_mkdir_is_write(self):
        """drive mkdir should be classified as write (corrected command name)."""
        from gog_cleaner import is_read_command
        
        self.assertFalse(is_read_command(["drive", "mkdir", "NewFolder"]))


class TestGogCleanerDispatch(unittest.TestCase):
    """Test the main clean() dispatcher function."""
    
    def test_dispatches_to_correct_cleaner(self):
        """clean() should dispatch to the correct service cleaner."""
        from gog_cleaner import clean
        
        # Gmail search
        result = clean(["gmail", "search", "test"], json.dumps(GMAIL_SEARCH_OUTPUT))
        self.assertIn("threads", json.loads(result))
        
        # Calendar events
        result = clean(["calendar", "events"], json.dumps(CALENDAR_EVENTS_OUTPUT))
        parsed = json.loads(result)
        self.assertIn("events", parsed)
        self.assertNotIn("etag", parsed["events"][0])
    
    def test_returns_unchanged_for_write_commands(self):
        """Write commands should return output unchanged."""
        from gog_cleaner import clean
        
        output = '{"status": "sent", "messageId": "abc123"}'
        result = clean(["gmail", "send", "--to", "x@y.com"], output)
        
        self.assertEqual(result, output)
    
    def test_handles_invalid_json(self):
        """Should handle non-JSON output gracefully."""
        from gog_cleaner import clean
        
        output = "Not JSON - some error message"
        result = clean(["gmail", "search", "test"], output)
        
        # Should return original when it can't parse
        self.assertEqual(result, output)


class TestTokenSavings(unittest.TestCase):
    """Test that cleaning actually reduces output size."""
    
    def test_gmail_get_reduces_size(self):
        """Cleaning gmail get should significantly reduce size."""
        from gog_cleaner import clean_gmail_get
        
        original_size = len(json.dumps(GMAIL_GET_OUTPUT))
        result = clean_gmail_get(GMAIL_GET_OUTPUT)
        cleaned_size = len(json.dumps(result))
        
        # Should be at least 50% smaller (payload is huge)
        reduction = (original_size - cleaned_size) / original_size
        self.assertGreater(reduction, 0.3, 
            f"Expected >30% reduction, got {reduction:.1%}")
    
    def test_calendar_events_reduces_size(self):
        """Cleaning calendar events should reduce size."""
        from gog_cleaner import clean_calendar_events
        
        original_size = len(json.dumps(CALENDAR_EVENTS_OUTPUT))
        result = clean_calendar_events(CALENDAR_EVENTS_OUTPUT)
        cleaned_size = len(json.dumps(result))
        
        # Should be smaller
        self.assertLess(cleaned_size, original_size)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    # Run tests
    unittest.main(verbosity=2)


class TestHtmlToTextUnclosedDropTags(unittest.TestCase):
    """Unclosed <script>/<style> in the BODY throws HTMLParser into CDATA mode
    to EOF — the coverage fallback must recover the content that follows."""

    def test_unclosed_style_in_body_preserves_following_text(self):
        from gog_cleaner import html_to_text
        html = ("<html><body><div>Hi Sam,</div>"
                "<style>.header{color:blue}\n"
                "<div>Your order #12345 has shipped.</div>"
                "<p>Total: $99.99</p></body></html>")
        text = html_to_text(html)
        self.assertIn("Your order #12345 has shipped", text)
        self.assertIn("$99.99", text)

    def test_unclosed_script_in_body_preserves_following_text(self):
        from gog_cleaner import html_to_text
        html = ("<html><body><p>Before script</p>"
                "<script>var x = 1;<p>After script continues here</p>"
                "</body></html>")
        self.assertIn("After script continues here", html_to_text(html))

    def test_wellformed_html_does_not_trigger_fallback_noise(self):
        from gog_cleaner import html_to_text
        html = ("<html><head><style>.a{color:red}</style></head>"
                "<body><p>Clean body text only.</p></body></html>")
        text = html_to_text(html)
        self.assertIn("Clean body text only.", text)
        self.assertNotIn("color:red", text)


class TestGmailGetPayloadBodyFallback(unittest.TestCase):
    """gmail get must walk message.payload for a body when gog didn't hoist
    one — otherwise the message silently reduces to its snippet."""

    def test_body_extracted_from_payload_when_not_hoisted(self):
        import base64 as _b64
        from gog_cleaner import clean_gmail_get
        body_text = "PAYMENT DUE: $500 by tomorrow"
        encoded = _b64.urlsafe_b64encode(body_text.encode()).decode().rstrip("=")
        fixture = {
            "headers": {"from": "x@y.com", "subject": "Test"},
            "message": {
                "id": "abc", "snippet": "Important payment details",
                "payload": {"mimeType": "text/plain",
                            "body": {"data": encoded, "size": len(body_text)}},
            },
        }
        result = clean_gmail_get(fixture)
        self.assertIn("body", result)
        self.assertIn("PAYMENT DUE: $500", result["body"])

    def test_hoisted_body_still_wins(self):
        from gog_cleaner import clean_gmail_get
        fixture = {
            "body": "Hoisted body text",
            "headers": {"from": "x@y.com"},
            "message": {"id": "abc", "snippet": "snip"},
        }
        result = clean_gmail_get(fixture)
        self.assertEqual(result["body"], "Hoisted body text")


class TestAttachmentOnlyBodyMarker(unittest.TestCase):
    """Very large messages carry attachmentId instead of inline data — surface
    a marker instead of pretending the email is empty."""

    def test_attachment_only_text_part_yields_marker(self):
        from gog_cleaner import _walk_parts_for_body
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain",
                 "body": {"attachmentId": "ATTACH_123", "size": 1234567}},
                {"mimeType": "text/html",
                 "body": {"attachmentId": "ATTACH_456", "size": 1234567}},
            ],
        }
        result = _walk_parts_for_body(payload)
        self.assertIn("attachmentId", result)
        self.assertIn("not inlined", result)

    def test_truly_empty_payload_still_returns_empty(self):
        from gog_cleaner import _walk_parts_for_body
        self.assertEqual(_walk_parts_for_body({"mimeType": "text/plain",
                                               "body": {"size": 0}}), "")
