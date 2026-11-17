"""Print a bounded package summary of a Trivy JSON report for the CI log."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys


def summarize(report, limit=20):
    packages = defaultdict(Counter)
    for result in report.get('Results') or []:
        for finding in result.get('Vulnerabilities') or []:
            package = (result.get('Target', '?'), finding.get('PkgName', '?'),
                       finding.get('InstalledVersion', '?'))
            packages[package][finding.get('Severity', 'UNKNOWN')] += 1
    total = sum(sum(counts.values()) for counts in packages.values())
    lines = [f'Security scan: {total} findings across {len(packages)} installed packages.']
    ordered = sorted(packages, key=lambda key: (-packages[key]['CRITICAL'], -packages[key]['HIGH'], key))
    for target, name, version in ordered[:limit]:
        counts = packages[target, name, version]
        severities = ', '.join(f'{severity}={counts[severity]}' for severity in sorted(counts))
        lines.append(f'  {name} {version}: {severities} [{target}]')
    if len(ordered) > limit:
        lines.append(f'  ... {len(ordered) - limit} more packages; see the full JSON report.')
    return '\n'.join(lines)


def main():
    try:
        report = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        print(summarize(report))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f'Cannot summarize security report: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
