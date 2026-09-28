"""Feed registry loading from JSON and the out-of-tree app state dir.

Covers `config.load_feed_registry` (user file wins, bundled default fallback,
unsafe dirs rejected) and the MINERU_APP_STATE_DIR / MINERU_HOME defaults.
Python 3.9-compatible. Stdlib only.
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _app_test_setup  # noqa: F401  (puts app/ on sys.path)
import config


APP_DIR = Path(__file__).resolve().parent.parent / "app"
EXAMPLE_FEEDS_FILE = Path(__file__).resolve().parent.parent / "engine" / "config" / "feeds.example.json"


class FeedRegistryLoaderTest(unittest.TestCase):
    """load_feed_registry picks the right source and validates entries."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="mineru-feeds-")
        self.tmp = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def write_json(self, name, payload):
        target = self.tmp / name
        target.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return target

    def test_bundled_default_is_generic(self):
        registry = config.load_feed_registry(self.tmp / "absent.json", config.BUNDLED_DEFAULT_FEEDS_FILE)
        feed_ids = [feed["id"] for feed in registry]
        self.assertIn("morning", feed_ids)
        self.assertNotIn("pet", feed_ids)
        self.assertNotIn("model_watch", feed_ids)

    def test_user_file_replaces_default_and_fills_defaults(self):
        user_file = self.write_json("feeds.json", {"feeds": [
            {"id": "pet", "dirs": ["briefs_pet"]},
            {"id": "pet", "dirs": ["briefs_dupe"]},
        ]})
        registry = config.load_feed_registry(user_file, config.BUNDLED_DEFAULT_FEEDS_FILE)
        self.assertEqual(registry, [{
            "id": "pet", "dirs": ["briefs_pet"], "display_name": "pet",
            "emoji": "", "accent": "muted", "group": config.FEED_DEFAULT_GROUP,
        }])

    def test_unsafe_dirs_fail_loud_naming_the_entry(self):
        for bad_dir in ["../etc", "/etc", ".", "./", "cache", "logs", "profiles", "app-state",
                        "config", "engine", ".git", ".hidden", "memory", "memory/daily",
                        "briefs_ok/../cache", "reports/.secret", "creations/cache", ""]:
            with self.subTest(bad_dir=bad_dir):
                user_file = self.write_json("feeds.json", {"feeds": [
                    {"id": "ok", "dirs": ["briefs_ok"]},
                    {"id": "bad", "dirs": ["briefs_fine", bad_dir]},
                ]})
                with self.assertRaises(config.UnsafeFeedDirError) as raised:
                    config.load_feed_registry(user_file, config.BUNDLED_DEFAULT_FEEDS_FILE)
                self.assertIn("'bad'", str(raised.exception))
                self.assertIn(repr(bad_dir), str(raised.exception))

    def test_allowed_output_trees_accepted(self):
        good_dirs = ["briefs_pet", "briefs_x/sub", "reports", "reports/2026", "creations",
                     "inbox", "outbox", "./briefs_norm"]
        user_file = self.write_json("feeds.json", {"feeds": [{"id": "ok", "dirs": good_dirs}]})
        registry = config.load_feed_registry(user_file, config.BUNDLED_DEFAULT_FEEDS_FILE)
        self.assertEqual(registry[0]["dirs"], good_dirs)

    def test_broken_user_file_falls_back_to_default(self):
        user_file = self.write_json("feeds.json", "{not json")
        registry = config.load_feed_registry(user_file, config.BUNDLED_DEFAULT_FEEDS_FILE)
        self.assertIn("morning", [feed["id"] for feed in registry])

    def test_example_file_parses_fully(self):
        raw_feeds = json.loads(EXAMPLE_FEEDS_FILE.read_text(encoding="utf-8"))["feeds"]
        registry = config.load_feed_registry(EXAMPLE_FEEDS_FILE, self.tmp / "absent.json")
        self.assertEqual(len(registry), len(raw_feeds))


class StateDirTest(unittest.TestCase):
    """STATE_DIR lives outside app/ and follows the env seams."""

    def read_state_dir(self, extra_env):
        env = {key: value for key, value in os.environ.items()
               if key not in ("MINERU_APP_STATE_DIR", "MINERU_HOME")}
        env.update(extra_env)
        output = subprocess.check_output(
            [sys.executable, "-c", "import config; print(config.STATE_DIR); print(config.WRITE_ROOT)"],
            cwd=str(APP_DIR), env=env, text=True,
        )
        return output.splitlines()

    def test_default_is_mineru_home_app_state(self):
        state_dir, _ = self.read_state_dir({"MINERU_HOME": "/tmp/mineru-home-x"})
        self.assertEqual(state_dir, "/tmp/mineru-home-x/app-state")

    def test_env_override(self):
        state_dir, _ = self.read_state_dir({"MINERU_HOME": "/tmp/h", "MINERU_APP_STATE_DIR": "/tmp/custom-state"})
        self.assertEqual(state_dir, "/tmp/custom-state")

    def test_not_under_app_dir(self):
        self.assertNotIn(APP_DIR.resolve(), Path(config.STATE_DIR).resolve().parents)


if __name__ == "__main__":
    unittest.main()
