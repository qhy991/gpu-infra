import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kernel_infra.server import KernelInfraServer


class ServerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def server(self, root, socket):
        root.mkdir(exist_ok=True)
        manager = SimpleNamespace(
            store=SimpleNamespace(root=root),
            recover_interrupted=AsyncMock(return_value=1), close=AsyncMock(),
        )
        services = SimpleNamespace(
            recover_interrupted=AsyncMock(return_value=2), close=AsyncMock(),
        )
        return KernelInfraServer(manager, services, socket)

    async def test_duplicate_owner_never_reconciles_live_jobs(self):
        first = self.server(self.root / "state", self.root / "a.sock")
        self.assertEqual(await first.start(), 3)
        self.addAsyncCleanup(first.close)
        for root, socket in ((self.root / "state", self.root / "a.sock"),
                             (self.root / "state", self.root / "b.sock"),
                             (self.root / "other", self.root / "a.sock")):
            second = self.server(root, socket)
            with self.assertRaisesRegex(RuntimeError, "owner already active"):
                await second.start()
            await second.close()
            second.manager.recover_interrupted.assert_not_awaited()
            second.services.recover_interrupted.assert_not_awaited()
            second.manager.close.assert_not_awaited()
            reader, writer = await asyncio.open_unix_connection(first.socket_path)
            writer.write(b'{"op":"invalid"}\n')
            await writer.drain()
            self.assertIn(b'unknown operation', await reader.readline())
            writer.close()
            await writer.wait_closed()

    async def test_failed_recovery_releases_owner_for_successor(self):
        failed = self.server(self.root / "state", self.root / "a.sock")
        failed.manager.recover_interrupted.side_effect = RuntimeError("broker unavailable")
        with self.assertRaisesRegex(RuntimeError, "broker unavailable"):
            await failed.start()
        self.assertFalse(failed.socket_path.exists())
        successor = self.server(self.root / "state", self.root / "a.sock")
        self.assertEqual(await successor.start(), 3)
        await successor.close()
        self.assertFalse(successor.socket_path.exists())

    async def test_legacy_live_socket_is_checked_before_recovery(self):
        socket = self.root / "legacy.sock"
        async def handle(reader, writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()
        legacy = await asyncio.start_unix_server(handle, path=socket)
        try:
            new = self.server(self.root / "state", socket)
            with self.assertRaisesRegex(RuntimeError, "socket already active"):
                await new.start()
            new.manager.recover_interrupted.assert_not_awaited()
            new.services.recover_interrupted.assert_not_awaited()
            self.assertTrue(socket.exists())
        finally:
            legacy.close()
            await legacy.wait_closed()


if __name__ == "__main__":
    unittest.main()
