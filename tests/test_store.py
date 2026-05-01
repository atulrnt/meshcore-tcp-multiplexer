import asyncio
import os
import tempfile
import unittest

from store import MessageStore, STORABLE_TYPES


class TestStorableTypes(unittest.TestCase):
    def test_contains_expected_types(self):
        for t in (7, 8, 16, 17):
            self.assertIn(t, STORABLE_TYPES)

    def test_excludes_non_message_types(self):
        for t in (0, 1, 2, 5, 10, 15, 18, 100):
            self.assertNotIn(t, STORABLE_TYPES)


class TestMessageStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = MessageStore(self.db_path)

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    async def test_store_and_retrieve(self):
        data = b"\x3e\x05\x00\x07hello"
        await self.store.store(7, data)
        frames = await self.store.load_since("1.2.3.4")
        self.assertIn(data, frames)

    async def test_new_client_gets_all_messages(self):
        await self.store.store(7, b"msg1")
        await self.store.store(8, b"msg2")
        frames = await self.store.load_since("10.0.0.1")
        self.assertEqual(len(frames), 2)

    async def test_empty_store_returns_empty_list(self):
        frames = await self.store.load_since("1.2.3.4")
        self.assertEqual(frames, [])

    async def test_watermark_excludes_old_messages(self):
        await self.store.store(7, b"old_msg")
        await self.store.update_client("1.2.3.4")
        await asyncio.sleep(0.05)
        await self.store.store(7, b"new_msg")
        frames = await self.store.load_since("1.2.3.4")
        self.assertEqual(len(frames), 1)
        self.assertIn(b"new_msg", frames)
        self.assertNotIn(b"old_msg", frames)

    async def test_multiple_clients_have_independent_watermarks(self):
        await self.store.store(7, b"before")
        await self.store.update_client("client_a")
        await asyncio.sleep(0.05)
        await self.store.store(7, b"after")

        frames_a = await self.store.load_since("client_a")
        frames_b = await self.store.load_since("client_b")

        self.assertEqual(len(frames_a), 1)
        self.assertIn(b"after", frames_a)
        self.assertEqual(len(frames_b), 2)

    async def test_update_client_is_idempotent(self):
        await self.store.update_client("192.168.1.1")
        await self.store.update_client("192.168.1.1")

    async def test_returning_client_only_gets_new_messages(self):
        await self.store.store(16, b"private_msg")
        await self.store.update_client("2.3.4.5")
        await asyncio.sleep(0.05)
        await self.store.store(17, b"channel_msg")

        frames = await self.store.load_since("2.3.4.5")
        self.assertEqual(len(frames), 1)
        self.assertIn(b"channel_msg", frames)

    async def test_frames_returned_in_order(self):
        for i in range(5):
            await self.store.store(7, f"msg{i}".encode())
            await asyncio.sleep(0.02)
        frames = await self.store.load_since("1.2.3.4")
        self.assertEqual(frames, [f"msg{i}".encode() for i in range(5)])
