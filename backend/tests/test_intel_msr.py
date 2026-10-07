"""IntelMsrReader validity guards, exercised against a fake .NET object (no driver needed)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app.collectors.intel_msr import IntelMsrReader, MsrUnavailable
from app.collectors.throttle import MSR_RAPL_POWER_UNIT


class FakeDotNetMsr:
    """Mimics ``IntelMsr.ReadMsr(index, out value)`` which pythonnet returns as ``(ok, value)``."""

    def __init__(self, values: dict[int, int], *, ok: bool = True) -> None:
        self.values, self.ok, self.closed = values, ok, False

    def ReadMsr(self, index: int, _out: int) -> tuple[bool, int]:  # noqa: N802 - .NET naming
        return self.ok, self.values.get(index, 0)

    def Close(self) -> None:  # noqa: N802
        self.closed = True


def reader_with(msr: FakeDotNetMsr) -> IntelMsrReader:
    r = IntelMsrReader(Path("."))
    r._msr, r._uint64 = msr, int  # type: ignore[assignment]
    return r


class IntelMsrReaderTests(unittest.TestCase):
    def test_genuine_reads_pass_through(self) -> None:
        r = reader_with(FakeDotNetMsr({MSR_RAPL_POWER_UNIT: 0xA0E03, 0x64F: 0x800, 0x1B1: 0x80000000}))
        s = r.read()
        self.assertEqual((s.perf_limit_reasons, s.package_therm_status, s.power_unit), (0x800, 0x80000000, 0xA0E03))

    def test_all_zero_reads_are_rejected_not_reported_as_clean(self) -> None:
        # LHM returns (True, 0) without elevation. That must NOT become "detected: not throttling".
        r = reader_with(FakeDotNetMsr({}))
        with self.assertRaises(MsrUnavailable):
            r.read()

    def test_failed_reads_are_rejected(self) -> None:
        r = reader_with(FakeDotNetMsr({MSR_RAPL_POWER_UNIT: 0xA0E03}, ok=False))
        with self.assertRaises(MsrUnavailable):
            r.read()

    def test_start_refuses_when_not_elevated(self) -> None:
        with mock.patch("app.collectors.intel_msr.is_admin", return_value=False):
            with self.assertRaises(MsrUnavailable) as cm:
                IntelMsrReader(Path(".")).start()
        self.assertIn("not elevated", str(cm.exception))

    def test_read_before_start(self) -> None:
        with self.assertRaises(MsrUnavailable):
            IntelMsrReader(Path(".")).read()


if __name__ == "__main__":
    unittest.main()
