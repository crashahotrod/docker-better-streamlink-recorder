"""Measure long-recording normalization; tiny frames measure overhead, not HD speed."""
import argparse
import json
import pathlib
import resource
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seconds', type=int, default=28800)
parser.add_argument('--output', type=pathlib.Path, required=True)
args = parser.parse_args()
if args.seconds < 1 or args.output.exists():
    parser.error('Use a positive duration and a new output file')
with tempfile.TemporaryDirectory(prefix='recorder-benchmark-') as temporary:
    job = pathlib.Path(temporary)
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    f'color=red:size=32x32:rate=1:duration={args.seconds}', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-movflags', 'empty_moov+frag_keyframe+default_base_moof',
                    '-f', 'mp4', str(job / 'capture.bin')], check=True)
    started = time.monotonic()
    subprocess.run([sys.executable, str(ROOT / 'media.py'), 'normalize', str(job)], check=True)
    elapsed = time.monotonic() - started
    manifest = json.loads((job / 'normalized.json').read_text())
    commands = [json.loads(p.read_text()) for p in (job / 'diagnostics').rglob('*.command.json')]
    result = {
        'ffmpeg': subprocess.check_output(['ffmpeg', '-version'], text=True).splitlines()[0],
        'seconds': args.seconds, 'frames': manifest['parts'][0]['validation']['frames'][0],
        'resolution': '32x32', 'fps': 1, 'audio': False,
        'capture_bytes': (job / 'capture.bin').stat().st_size,
        'output_bytes': sum((job / p['file']).stat().st_size for p in manifest['parts']),
        'normalization_seconds': round(elapsed, 3),
        'stream_copy_seconds': sum(c['elapsed_seconds'] for c in commands if '-c' in c['command']),
        'decode_validation_seconds': sum(c['elapsed_seconds'] for c in commands if 'framehash' in c['command']),
        'max_child_rss_kib': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        'retained_job_bytes': sum(p.stat().st_size for p in job.rglob('*') if p.is_file()),
        'limits': 'RSS includes fixture generation and FFmpeg children. Tiny frames do not predict HD decoding speed. '
                  'Peak scratch includes capture + extracted periods + candidates + streamed framehash logs; '
                  'final publication needs a separate storage copy. No video encoder is used during normalization.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
