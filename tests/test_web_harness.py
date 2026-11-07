"""Harness error-path tests, independent of a native binary; no engine mocks."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

import run_web as web


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
            args = argparse.Namespace(binary=str(root / 'missing-engine'), suite='web')
            code = await web.run(args, web.load_suite('web')[:1], root)
            result = json.loads((root / 'results.json').read_text())
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'ERROR')
            self.assertNotEqual(result['error']['status'], 'PASS')
            self.assertEqual(result['results'], [])

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


if __name__ == '__main__':
    unittest.main()
