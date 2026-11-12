"""Harness error-path tests, independent of a native binary; no engine mocks."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import run_web as web
import toon
import evidence_view


class HarnessTests(unittest.TestCase):
    def test_assertion_failure_cannot_pass_by_message_wording(self):
        result = {'complete': True, 'subtests': [{'name': 'geometry', 'status': 'FAIL', 'assertions': 1, 'message': 'PASS'}]}
        with self.assertRaises(web.CaseFailure) as caught:
            web.validate_assertions(result, ['geometry'])
        self.assertEqual(caught.exception.status, 'FAIL')
        self.assertEqual(web.failure_record(caught.exception)['status'], 'FAIL')

    def test_missing_empty_duplicate_or_unexecuted_assertions_are_errors(self):
        invalid = [None, {'complete': True, 'subtests': []},
                   {'complete': False, 'subtests': []},
                   {'complete': True, 'subtests': [{'name': 'geometry', 'status': 'PASS', 'assertions': 0}]},
                   {'complete': True, 'subtests': [{'name': 'wrong', 'status': 'PASS', 'assertions': 1}]},
                   {'complete': True, 'subtests': [{'name': 'geometry', 'status': 'PASS', 'assertions': 1}] * 2}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(web.CaseFailure):
                web.validate_assertions(value, ['geometry'])

    def test_real_pixel_difference_has_nonzero_metrics_and_diagnostic(self):
        actual = bytes((255, 0, 0, 255, 0, 0, 255, 255))
        reference = bytes((255, 0, 0, 255, 0, 255, 0, 255))
        metrics, _ = web.compare_pixels(actual, reference, 2, 1)
        self.assertEqual(metrics, {'different_pixels': 1, 'max_channel_difference': 255})
        same, _ = web.compare_pixels(actual, actual, 2, 1)
        self.assertEqual(same['different_pixels'], 0)
        with self.assertRaises(web.CaseFailure):
            web.probes(actual, [[1, 0, '00ff00ff']])
        with self.assertRaises(web.CaseFailure):
            web.compare_pixels(actual, reference[:4], 2, 1)

    def test_png_contains_exact_rgba_and_valid_chunk_checksums(self):
        rgba = bytes((255, 0, 0, 255, 0, 255, 0, 255))
        png = web.png_bytes(2, 1, rgba)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        position, image = 8, b''
        while position < len(png):
            size = struct.unpack_from('>I', png, position)[0]
            kind = png[position + 4:position + 8]
            data = png[position + 8:position + 8 + size]
            checksum = struct.unpack_from('>I', png, position + 8 + size)[0]
            self.assertEqual(checksum, zlib.crc32(kind + data))
            if kind == b'IHDR':
                self.assertEqual(struct.unpack('>IIBBBBB', data), (2, 1, 8, 6, 0, 0, 0))
            if kind == b'IDAT':
                image += data
            position += 12 + size
        self.assertEqual(zlib.decompress(image), b'\0' + rgba)
        with self.assertRaises(ValueError):
            web.png_bytes(2, 2, rgba)

    def test_crash_and_script_exception_remain_nonpassing(self):
        self.assertEqual(web.failure_record(RuntimeError('PASS'), -11)['status'], 'CRASH')
        self.assertEqual(web.failure_record(RuntimeError('PASS'))['status'], 'ERROR')

    def test_wpt_nonpassing_subtests_and_harness_errors_preserve_outcomes(self):
        for statuses, expected in [([0, 1], "FAIL"), ([1, 2], "TIMEOUT"),
                                   ([0, 3], "UNSUPPORTED"), ([0, 4], "UNSUPPORTED")]:
            value = {"complete": True, "harness": {"status": 0},
                     "subtests": [{"name": str(i), "status": status} for i, status in enumerate(statuses)]}
            with self.subTest(statuses=statuses), self.assertRaises(web.CaseFailure) as caught:
                web.validate_wpt(value)
            self.assertEqual(caught.exception.status, expected)
        with self.assertRaises(web.CaseFailure) as caught:
            web.validate_wpt({"complete": True, "harness": {"status": 1}, "subtests": []})
        self.assertEqual(caught.exception.status, "ERROR")

    def test_wpt_empty_duplicate_and_unknown_results_are_not_semantic_passes(self):
        for rows in ([], [{"name": "test", "status": 99}],
                     [{"name": "same", "status": 0}] * 2):
            with self.subTest(rows=rows), self.assertRaises(web.CaseFailure) as caught:
                web.validate_wpt({"complete": True, "harness": {"status": 0}, "subtests": rows})
            self.assertEqual(caught.exception.status, "ERROR")

    def test_malformed_script_results_remain_readable_error_evidence(self):
        for invalid in (7, True, "invalid", {"status": "PASS"}, None):
            with self.subTest(subtests=invalid):
                value = {"complete": True, "subtests": invalid}
                with self.assertRaises(web.CaseFailure) as caught:
                    web.validate_assertions(value, ["geometry"])
                record = dict(id="script-geometry", **web.failure_record(caught.exception))
                for row in (record, {"id": record["id"], "status": record["status"],
                                    "result": {"results": [record]}}):
                    report = {"status": "ERROR", "results": [row]}
                    projected = evidence_view.view(report)["results"][0]
                    self.assertEqual(projected["status"], "ERROR")
                    self.assertEqual(projected["message"], str(caught.exception))
                    self.assertNotIn("subtests", projected)
                    self.assertNotIn("failures", projected)
                    self.assertIn("ERROR", evidence_view.render(report))

    def test_assertion_failure_projection_keeps_counts_and_failure_diagnostics(self):
        value = {"complete": True,
                 "subtests": [{"name": "append", "status": "PASS", "assertions": 1},
                              {"name": "remove", "status": "FAIL", "assertions": 1,
                               "message": "node retained"}]}
        with self.assertRaises(web.CaseFailure) as caught:
            web.validate_assertions(value, ["append", "remove"])
        report = {"status": "FAIL", "results": [
            dict(id="script-dom", **web.failure_record(caught.exception))]}
        projected = evidence_view.view(report)["results"][0]
        self.assertEqual(projected["status"], "FAIL")
        self.assertEqual(projected["subtests"], {"passed": 1, "total": 2})
        self.assertEqual(projected["failures"], [
            {"status": "FAIL", "name": "remove", "message": "node retained"}])
        self.assertIn("node retained", evidence_view.render(report))



class ToonStructureTests(unittest.TestCase):
    def test_nested_evidence_preserves_value_types_and_scope(self):
        cases = [
            ({"results": [{}, None]},
             "results[2]:\n  -\n  - null"),
            ([[{"id": 1}, {"id": 2}]],
             "[1]:\n  - [2]:\n    - id: 1\n    - id: 2"),
            ([{"checks": [{}, {"status": "FAIL"}], "meta": {}}],
             "[1]:\n  - checks[2]:\n      -\n      - status: FAIL\n    meta:"),
            ([{"checks": [{"id": 1}, {"id": 2}], "meta": {}}],
             "[1]:\n  - checks[2]{id}:\n      1\n      2\n    meta:"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(toon.encode(value), expected)


class AsyncHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_deadline_cancels_unfinished_work_and_reports_timeout(self):
        cancelled = asyncio.Event()
        async def unfinished():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with self.assertRaises(web.CaseFailure) as caught:
            await web.bounded(unfinished(), 0.01)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(web.failure_record(caught.exception)['status'], 'TIMEOUT')
        self.assertEqual(await web.bounded(asyncio.sleep(0, result=17), 1), 17)

    async def test_missing_binary_writes_nonpassing_machine_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(binary=str(root / 'missing-engine'), suite='web', format='toon')
            code = await web.run(args, web.load_suite('web')[:1], root)
            result = json.loads((root / 'results.json').read_text())
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'ERROR')
            self.assertNotEqual(result['error']['status'], 'PASS')
            self.assertEqual(result['results'], [])

    async def _assert_pending_request_cleanup(self, *, cancel):
        connected, entered = asyncio.Event(), asyncio.Event()
        connections = []
        original = asyncio.start_server
        failure = web.CaseFailure("FAIL", "original scenario failure")

        async def start_server(callback, *args, **kwargs):
            async def accepted(reader, writer):
                connected.set()
                await callback(reader, writer)
            return await original(accepted, *args, **kwargs)

        async def exercise():
            async with web.fixture_server() as origin:
                address = web.urlsplit(origin)
                reader, writer = await asyncio.open_connection(address.hostname, address.port)
                connections.append((reader, writer))
                writer.write(b"GET /fixtures/empty.html HTTP/1.1\r\n")
                await writer.drain()
                await connected.wait()
                entered.set()
                if cancel:
                    await asyncio.Event().wait()
                raise failure

        with patch.object(web.asyncio, "start_server", start_server):
            task = asyncio.create_task(exercise())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if cancel:
                    task.cancel()
                done, _ = await asyncio.wait({task}, timeout=0.5)
                self.assertIn(task, done, "fixture waited for a live client before closing it")
                with self.assertRaises(asyncio.CancelledError if cancel else web.CaseFailure) as caught:
                    await task
                if not cancel:
                    self.assertIs(caught.exception, failure)
                # Closing a socket with unread request bytes may reset it.
                with contextlib.suppress(ConnectionResetError):
                    self.assertEqual(await asyncio.wait_for(connections[0][0].read(), 1), b"")
            finally:
                for _, writer in connections:
                    writer.close()
                    with contextlib.suppress(ConnectionResetError):
                        await writer.wait_closed()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_fixture_failure_closes_unfinished_http_client_without_masking_cause(self):
        await self._assert_pending_request_cleanup(cancel=False)

    async def test_fixture_cancellation_closes_unfinished_http_client(self):
        await self._assert_pending_request_cleanup(cancel=True)

    async def test_loopback_server_serves_exact_fixture_not_repository_files(self):
        async with web.fixture_server() as origin:
            from urllib.parse import urlsplit
            address = urlsplit(origin)
            async def request(path):
                reader, writer = await asyncio.open_connection(address.hostname, address.port)
                writer.write(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'.encode())
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 2)
                writer.close()
                await writer.wait_closed()
                return response
            served = await request('/fixtures/empty.html')
            self.assertIn(b'200 OK\r\n', served)
            self.assertEqual(served.split(b'\r\n\r\n', 1)[1], (web.HERE / 'web/fixtures/empty.html').read_bytes())
            for path in ('/../run_web.py', '/%2e%2e/run_web.py', '/cases.json'):
                self.assertIn(b'404 Not Found', await request(path))

    async def test_http_session_fixture_rejects_wrong_credentials_and_revoked_cookie(self):
        async with web.fixture_server(authentication=True) as origin:
            address = web.urlsplit(origin)
            async def request(path, *, form=None, cookie=None):
                reader, writer = await asyncio.open_connection(address.hostname, address.port)
                body = (form or "").encode()
                headers = (f"{'POST' if form is not None else 'GET'} {path} HTTP/1.1\r\n"
                           f"Host: {address.netloc}\r\nContent-Length: {len(body)}\r\n"
                           "Content-Type: application/x-www-form-urlencoded\r\n")
                if cookie is not None:
                    headers += f"Cookie: {cookie}\r\n"
                try:
                    writer.write((headers + "\r\n").encode() + body)
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), 2)
                    head, content = response.split(b"\r\n\r\n", 1)
                    return head.decode("latin-1"), content
                finally:
                    writer.close()
                    await writer.wait_closed()
            private = "/session/private"
            head, content = await request(private)
            self.assertTrue(head.startswith("HTTP/1.1 401 "))
            self.assertNotIn(b"server-only draft", content)
            head, _ = await request("/session/login", form="username=local-user&password=wrong")
            self.assertTrue(head.startswith("HTTP/1.1 401 "))
            self.assertNotIn("Set-Cookie:", head)
            head, _ = await request("/session/login", form="username=local-user&password=fixture-password")
            self.assertTrue(head.startswith("HTTP/1.1 303 "))
            value = next(line.removeprefix("Set-Cookie: ") for line in head.split("\r\n") if line.startswith("Set-Cookie: "))
            self.assertIn("HttpOnly", value)
            cookie = value.split(";", 1)[0]
            head, content = await request(private, cookie=cookie)
            self.assertTrue(head.startswith("HTTP/1.1 200 "))
            self.assertIn(b"server-only draft", content)
            head, _ = await request("/session/logout", form="", cookie=cookie)
            self.assertTrue(head.startswith("HTTP/1.1 303 "))
            self.assertIn("Max-Age=0", head)
            head, content = await request(private, cookie=cookie)
            self.assertTrue(head.startswith("HTTP/1.1 401 "))
            self.assertNotIn(b"server-only draft", content)


if __name__ == '__main__':
    unittest.main()
