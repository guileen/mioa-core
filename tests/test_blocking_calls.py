"""Real threads/sockets: cancellation cannot abandon work or its returned resources."""
import asyncio
import socket
import threading
import unittest

from mioa_os import LocalBlockingCalls


class BlockingCallsTests(unittest.IsolatedAsyncioTestCase):
    def test_runner_shutdown_drains_and_discards_unclaimed_resource(self):
        peer, returned = socket.socketpair()
        peer.settimeout(2)
        release = threading.Event()
        discarded = []
        timer = None
        try:
            with asyncio.Runner() as runner:
                async def start():
                    loop = asyncio.get_running_loop()
                    entered = asyncio.Event()

                    def operation():
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(5):
                            raise TimeoutError("runner did not release worker")
                        return returned

                    def discard(channel):
                        discarded.append(channel)
                        channel.close()

                    pending = asyncio.create_task(LocalBlockingCalls().run(operation, discard=discard))
                    await asyncio.wait_for(entered.wait(), 2)
                    return pending

                pending = runner.run(start())
                timer = threading.Timer(0.05, release.set)
                timer.start()
                # Exiting Runner cancels every Task, not just the caller.
            self.assertTrue(pending.cancelled())
            self.assertEqual(discarded, [returned])
            self.assertEqual(peer.recv(1), b"")
        finally:
            release.set()
            if timer is not None:
                timer.cancel()
                timer.join()
            peer.close()
            returned.close()

    async def test_normal_completion_transfers_socket_to_caller_and_propagates_io_error(self):
        service = LocalBlockingCalls()
        sender, receiver = socket.socketpair()
        receiver.settimeout(2)
        try:
            sender.sendall(b"owned")

            def receive_and_transfer():
                if receiver.recv(5) != b"owned":
                    raise ValueError("socket payload mismatch")
                return receiver

            discarded = []
            result = await service.run(receive_and_transfer, discard=discarded.append)
            self.assertIs(result, receiver)
            self.assertEqual(discarded, [])
            sender.sendall(b"still-live")
            self.assertEqual(result.recv(10), b"still-live")
            sender.close()

            def require_live_peer():
                if receiver.recv(1) == b"":
                    raise EOFError("real peer exited")

            with self.assertRaises(EOFError):
                await service.run(require_live_peer, discard=discarded.append)
            self.assertEqual(discarded, [])
        finally:
            sender.close()
            receiver.close()

    async def test_repeated_cancellation_drains_worker_then_closes_unclaimed_socket_once(self):
        await self.cancel_resource_operation(cleanup_failure=False)

    async def test_cleanup_failure_preserves_cancellation_and_records_resource_error(self):
        await self.cancel_resource_operation(cleanup_failure=True)

    async def cancel_resource_operation(self, *, cleanup_failure):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        peer, returned = socket.socketpair()
        peer.settimeout(2)
        discarded = []
        worker_threads = []

        def create_resource():
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("test did not release resource worker")
            return returned

        def discard(channel):
            discarded.append(channel)
            channel.close()
            if cleanup_failure:
                raise OSError("cleanup failure after actual close")

        pending = asyncio.create_task(LocalBlockingCalls().run(create_resource, discard=discard))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.assertGreaterEqual(returned.fileno(), 0)
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(pending, 2)
            self.assertEqual(discarded, [returned])
            self.assertEqual(peer.recv(1), b"")
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            notes = getattr(cancelled.exception, "__notes__", [])
            if cleanup_failure:
                self.assertIn("cancelled blocking result cleanup failed: OSError", notes)
            else:
                self.assertEqual(notes, [])
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            peer.close()
            returned.close()

    async def test_cancelled_worker_io_failure_is_retrieved_without_discarding_a_result(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        peer, receiver = socket.socketpair()
        receiver.settimeout(2)
        discarded = []

        def read_until_peer_exit():
            loop.call_soon_threadsafe(entered.set)
            try:
                if receiver.recv(1) == b"":
                    raise EOFError("peer closed during cancellation")
                raise ValueError("unexpected data")
            finally:
                receiver.close()

        pending = asyncio.create_task(LocalBlockingCalls().run(read_until_peer_exit, discard=discarded.append))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            peer.close()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(pending, 2)
            self.assertEqual(discarded, [])
            self.assertIn("cancelled blocking operation ended with EOFError", cancelled.exception.__notes__)
            self.assertEqual(receiver.fileno(), -1)
        finally:
            peer.close()
            await asyncio.gather(pending, return_exceptions=True)
            receiver.close()


if __name__ == "__main__":
    unittest.main()
