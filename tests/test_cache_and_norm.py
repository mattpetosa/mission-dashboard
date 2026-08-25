"""Regression tests for the audit fixes. Standard library only -- run with:

    ./venv/bin/python -m unittest discover -s tests -v

app.py starts the LL2 refresher and the image warmer at import, both of which
talk to the network, so the app tests stub the ll2 module out before importing
it. ll2 itself is import-safe.
"""
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_app(cache_dir):
    """Import app.py with a stubbed ll2 and its image cache pointed at tmp."""
    stub = types.ModuleType("ll2")
    stub.IMAGE_HOSTS = {"i.ytimg.com", "pbs.twimg.com"}
    stub.USER_AGENT = "test"
    stub.FeedStore = lambda: types.SimpleNamespace(
        snapshot=lambda: ({}, 0), status=lambda: {}
    )
    stub.start_refresher = lambda store: None
    stub.build_payload = lambda store: {"version": 0}
    stub.iso = lambda ts: "1970-01-01T00:00:00Z"
    sys.modules["ll2"] = stub
    sys.modules.pop("app", None)
    import app
    app.IMG_CACHE = str(cache_dir)
    return app


class ImageCacheEvictionTest(unittest.TestCase):
    """The proxy is unauthenticated and i.ytimg.com is allowlisted, so the
    cache has to have a ceiling."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)
        self.app = _load_app(self.cache)
        self.app.IMG_CACHE_MAX_BYTES = 10_000
        self.app.IMG_CACHE_TRIM_TO = 0.5
        self.app.IMG_EVICT_MIN_AGE = 0

    def _write(self, name, size, age=0):
        path = self.cache / name
        path.write_bytes(b"x" * size)
        if age:
            past = time.time() - age
            os.utime(path, (past, past))
        return path

    def _total(self):
        return sum(f.stat().st_size for f in self.cache.iterdir())

    def test_under_the_cap_nothing_is_evicted(self):
        self._write("a.jpg", 4000, age=10000)
        self.assertEqual(self.app.evict_images(force=True), 0)
        self.assertEqual(self._total(), 4000)

    def test_over_the_cap_trims_oldest_first(self):
        old = self._write("old.jpg", 6000, age=10000)
        mid = self._write("mid.jpg", 6000, age=5000)
        new = self._write("new.jpg", 3000, age=100)
        self.assertGreater(self._total(), self.app.IMG_CACHE_MAX_BYTES)

        self.app.evict_images(force=True)

        self.assertFalse(old.exists(), "oldest file should go first")
        self.assertTrue(new.exists(), "newest file should survive")
        self.assertLessEqual(self._total(), self.app.IMG_CACHE_MAX_BYTES * 0.5)
        del mid

    def test_recently_touched_files_are_never_evicted(self):
        self.app.IMG_EVICT_MIN_AGE = 300
        fresh = self._write("fresh.jpg", 20000, age=1)
        self.app.evict_images(force=True)
        self.assertTrue(fresh.exists())

    def test_sweeps_are_rate_limited(self):
        self._write("a.jpg", 20000, age=10000)
        self.app.evict_images(force=True)
        self._write("b.jpg", 20000, age=10000)
        # Second call inside IMG_SWEEP_INTERVAL is a no-op, not a rescan.
        self.assertEqual(self.app.evict_images(), 0)
        self.assertTrue((self.cache / "b.jpg").exists())


class ImageLockTableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = _load_app(Path(self._tmp.name))

    def test_lock_table_is_bounded(self):
        self.app.IMG_LOCKS_MAX = 8
        for i in range(200):
            self.app._img_lock("key-%d" % i)
        self.assertLessEqual(len(self.app._img_locks), 8)

    def test_held_locks_are_not_dropped(self):
        self.app.IMG_LOCKS_MAX = 4
        held = self.app._img_lock("held")
        held.acquire()
        self.addCleanup(held.release)
        for i in range(50):
            self.app._img_lock("other-%d" % i)
        self.assertIs(self.app._img_lock("held"), held)

    def test_same_key_returns_the_same_lock(self):
        self.assertIs(self.app._img_lock("k"), self.app._img_lock("k"))


class NormaliserTest(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("ll2", None)
        import ll2
        self.ll2 = ll2

    def test_img_refuses_hosts_the_proxy_would_403(self):
        good = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
        bad = "https://example.com/logo.png"
        self.assertEqual(self.ll2._img(good), good)
        self.assertIsNone(self.ll2._img(bad))
        self.assertIsNone(self.ll2._img({"image_url": bad}))
        self.assertEqual(self.ll2._img({"image_url": good}), good)

    def test_img_falls_back_to_the_other_key_when_one_is_off_allowlist(self):
        good = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
        obj = {"image_url": "http://insecure.example/x.png", "thumbnail_url": good}
        self.assertEqual(self.ll2._img(obj), good)

    def test_latitude_is_bounded_at_90(self):
        self.assertIsNone(self.ll2._coord(105.0, 90.0))
        self.assertEqual(self.ll2._coord(28.5, 90.0), 28.5)
        self.assertEqual(self.ll2._coord(-105.0), -105.0)  # a valid longitude


class BackoffPersistenceTest(unittest.TestCase):
    """A crash loop must not replay the priming burst at an endpoint that is
    already rate-limiting us."""

    def setUp(self):
        sys.modules.pop("ll2", None)
        import ll2
        self.ll2 = ll2
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = ll2.CACHE_DIR
        ll2.CACHE_DIR = self._tmp.name
        self.addCleanup(setattr, ll2, "CACHE_DIR", self._old)

    def test_failure_backoff_survives_a_restart(self):
        store = self.ll2.FeedStore()
        store._fetch = lambda name: (_ for _ in ()).throw(RuntimeError("rate limited by LL2 (429)"))
        self.assertFalse(store.refresh("upcoming"))

        # A brand new process, same disk.
        reborn = self.ll2.FeedStore()
        calls = []
        reborn._fetch = lambda name: calls.append(name)
        self.assertFalse(reborn.refresh("upcoming"))
        self.assertEqual(calls, [], "backed-off feed must not be refetched on restart")
        self.assertEqual(reborn._feeds["upcoming"]["failures"], 1)

    def test_a_successful_fetch_clears_the_backoff_on_disk(self):
        store = self.ll2.FeedStore()
        store._fetch = lambda name: (_ for _ in ()).throw(RuntimeError("boom"))
        store.refresh("upcoming")
        store._fetch = lambda name: {"results": []}
        store._feeds["upcoming"]["next_try"] = 0
        self.assertTrue(store.refresh("upcoming"))

        reborn = self.ll2.FeedStore()
        self.assertEqual(reborn._feeds["upcoming"]["failures"], 0)
        self.assertEqual(reborn._feeds["upcoming"]["next_try"], 0)


if __name__ == "__main__":
    unittest.main()
