"""Scan diagnostics must retain the original failure and the full report."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from security_summary import summarize

ROOT = Path(__file__).resolve().parents[1]
REPORT = {'Results': [{'Target': 'debian', 'Vulnerabilities': [
    {'PkgName': 'chromium', 'InstalledVersion': '150', 'Severity': 'HIGH'},
    {'PkgName': 'chromium', 'InstalledVersion': '150', 'Severity': 'CRITICAL'},
    {'PkgName': 'chromium', 'InstalledVersion': '150', 'Severity': 'HIGH'},
    {'PkgName': 'chromium-common', 'InstalledVersion': '150', 'Severity': 'HIGH'},
]}, {'Target': 'library', 'Vulnerabilities': None}]}


class SecuritySummaryTests(unittest.TestCase):
    def test_findings_are_grouped_by_installed_package_and_severity(self):
        summary = summarize(REPORT)
        self.assertIn('4 findings across 2 installed packages', summary)
        self.assertIn('chromium 150: CRITICAL=1, HIGH=2 [debian]', summary)
        self.assertIn('chromium-common 150: HIGH=1 [debian]', summary)

    def test_log_is_bounded_but_total_is_preserved(self):
        summary = summarize(REPORT, limit=1)
        self.assertIn('4 findings across 2 installed packages', summary)
        self.assertNotIn('chromium-common', summary)
        self.assertIn('1 more packages', summary)

    def test_reports_without_vulnerabilities(self):
        for report in ({}, {'Results': None}, {'Results': [{'Target': 'empty'}]}):
            with self.subTest(report=report):
                self.assertIn('0 findings across 0 installed packages', summarize(report))


@unittest.skipIf(os.name == 'nt' or not shutil.which('bash'), 'Scan wrapper requires a POSIX shell')
class ScanWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        docker = self.directory / 'docker'
        docker.write_text('''#!/bin/bash
if [[ "$1 $2" == 'image save' ]]; then
    printf 'fake image archive'
elif [[ "$*" == *'--entrypoint sh'* ]]; then
    cat > /dev/null
elif [[ "$*" == *'--output /scan/report.json'* ]]; then
    if [[ "$FAKE_REPORT" != missing ]]; then
        for arg in "$@"; do
            if [[ "$arg" == type=bind,source=*,target=/scan ]]; then
                directory=${arg#type=bind,source=}
                directory=${directory%,target=/scan}
                cp -- "$FAKE_REPORT" "$directory/report.json"
            fi
        done
    fi
    exit "$FAKE_SCAN_STATUS"
elif [[ "$1 $2" == 'volume rm' ]]; then
    printf 'cleaned' > "$FAKE_CLEANUP"
    exit 9  # Cleanup errors must not mask the original scanner result.
fi
''', encoding='utf-8')
        docker.chmod(0o755)
        self.report = self.directory / 'report.json'
        self.report.write_text(json.dumps(REPORT), encoding='utf-8')
        self.env = {**os.environ, 'PATH': str(self.directory) + os.pathsep + os.environ['PATH'],
                    'FAKE_REPORT': str(self.report), 'FAKE_CLEANUP': str(self.directory / 'cleanup'),
                    'TEST_ARTIFACT_DIR': str(self.directory / 'artifacts')}

    def scan(self, status, report=None):
        env = {**self.env, 'FAKE_SCAN_STATUS': str(status)}
        if report is not None:
            env['FAKE_REPORT'] = report
        result = subprocess.run(['bash', str(ROOT / 'tests/scan_image.sh')],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertTrue((self.directory / 'cleanup').is_file())
        return result

    def test_vulnerabilities_fail_with_summary_and_full_artifact(self):
        result = self.scan(1)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn('chromium 150: CRITICAL=1, HIGH=2', result.stdout)
        self.assertNotIn('PASS:', result.stdout)
        artifact = self.directory / 'artifacts/security.json'
        self.assertEqual(REPORT, json.loads(artifact.read_text()))

    def test_scanner_errors_without_report_preserve_exit_code(self):
        result = self.scan(7, 'missing')
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn('failed without producing a JSON report', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

    def test_invalid_report_cannot_hide_scan_failure(self):
        self.report.write_text('not JSON', encoding='utf-8')
        result = self.scan(2)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn('Cannot summarize security report', result.stderr)
        self.assertEqual('not JSON', (self.directory / 'artifacts/security.json').read_text())

    def test_success_remains_success(self):
        self.report.write_text('{"Results": []}', encoding='utf-8')
        result = self.scan(0)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('PASS: no fixable critical/high', result.stdout)


if __name__ == '__main__':
    unittest.main()
