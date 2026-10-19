"""Generate synthetic MP4 initialization-period fixtures for media regression tests.

Requires FFmpeg/ffprobe with libx264. Use an empty output directory.
"""
import argparse
import json
import pathlib
import struct
import subprocess

FPS = 10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--intro-seconds', type=int, default=2)
    parser.add_argument('--main-seconds', type=int, default=1)
    options = parser.parse_args()
    INTRO_SECONDS, MAIN_SECONDS = options.intro_seconds, options.main_seconds
    if min(INTRO_SECONDS, MAIN_SECONDS) < 1:
        parser.error('Fixture durations must be positive')
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit('Use an empty output directory; existing artifacts are never overwritten.')
    logs = output / 'logs'
    logs.mkdir()
    counter = 0

    def run(label, command):
        nonlocal counter
        counter += 1
        result = subprocess.run(command, capture_output=True, timeout=180)
        (logs / f'{counter:02d}-{label}.stdout').write_bytes(result.stdout)
        (logs / f'{counter:02d}-{label}.stderr').write_bytes(result.stderr)
        return result

    def checked(label, command):
        result = run(label, command)
        if result.returncode:
            raise RuntimeError(f'{label} failed: {result.stderr.decode(errors="replace")[-2000:]}')
        return result

    def boxes(path):
        payload = path.read_bytes()
        result = []
        offset = 0
        while offset < len(payload):
            if offset + 8 > len(payload):
                raise ValueError(f'Truncated box in {path}')
            size, kind = struct.unpack_from('>I4s', payload, offset)
            header = 8
            if size == 1:
                size = struct.unpack_from('>Q', payload, offset + 8)[0]
                header = 16
            elif size == 0:
                size = len(payload) - offset
            if size < header or offset + size > len(payload):
                raise ValueError(f'Invalid box in {path}')
            result.append((kind.decode('ascii'), payload[offset:offset + size]))
            offset += size
        return result

    def init(path):
        return b''.join(data for kind, data in boxes(path) if kind in ('ftyp', 'moov'))

    def fragments(path):
        return b''.join(data for kind, data in boxes(path) if kind in ('moof', 'mdat'))

    def shift_fragment_timestamps(path, seconds):
        # MP4 muxing can rebase output_ts_offset. Set each fragment's decode
        # time explicitly so the healthy control has a continuous clock.
        probe = checked('fragment-timebase', ['ffprobe', '-v', 'error', '-select_streams',
                        'v:0', '-show_entries', 'stream=time_base', '-of', 'json', str(path)])
        numerator, denominator = map(int, json.loads(probe.stdout)['streams'][0]['time_base'].split('/'))
        ticks = seconds * denominator // numerator
        data = bytearray(path.read_bytes())

        def walk(start, end):
            offset = start
            while offset < end:
                size, kind = struct.unpack_from('>I4s', data, offset)
                header = 8
                if size == 1:
                    size = struct.unpack_from('>Q', data, offset + 8)[0]
                    header = 16
                elif size == 0:
                    size = end - offset
                if size < header or offset + size > end:
                    raise ValueError('Malformed generated fixture')
                payload = offset + header
                if kind in (b'moof', b'traf'):
                    walk(payload, offset + size)
                elif kind == b'tfdt':
                    fmt = '>Q' if data[payload] == 1 else '>I'
                    value = struct.unpack_from(fmt, data, payload + 4)[0]
                    struct.pack_into(fmt, data, payload + 4, value + ticks)
                offset += size

        walk(0, len(data))
        path.write_bytes(data)

    def generate(name, seconds, color, resolution, sps_id, offset, fragmented=True):
        path = output / f'{name}.mp4'
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                   f'color=c={color}:s={resolution}:r={FPS}', '-t', str(seconds),
                   '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                   '-profile:v', 'baseline', '-level:v', '3.0', '-g', str(FPS),
                   '-x264-params', f'sps-id={sps_id}:scenecut=0']
        if fragmented:
            command += ['-movflags', '+empty_moov+frag_keyframe+default_base_moof']
        checked('generate-' + name, command + ['-f', 'mp4', str(path)])
        if fragmented and offset:
            shift_fragment_timestamps(path, offset)
        return path

    intro = generate('intro', INTRO_SECONDS, 'red', '160x90', 0, 0)
    same = generate('main-same-codec', MAIN_SECONDS, 'blue', '160x90', 0, INTRO_SECONDS)
    changed = generate('main-new-codec', MAIN_SECONDS, 'blue', '320x180', 1, INTRO_SECONDS)
    normal_intro = generate('normal-intro', INTRO_SECONDS, 'red', '160x90', 0, 0, False)
    normal_main = generate('normal-main', MAIN_SECONDS, 'blue', '320x180', 1, 0, False)

    fixture_bytes = {
        'control-single-init': init(intro) + fragments(intro) + fragments(same),
        'duplicate-same-codec': init(intro) + fragments(intro) + init(same) + fragments(same),
        'duplicate-changed-codec': init(intro) + fragments(intro) + init(changed) + fragments(changed),
        # Wrong init maps must be rejected rather than silently losing frames.
        'mismatched-first-init': init(changed) + fragments(intro) + init(intro) + fragments(changed),
        'concatenated-normal-mp4': normal_intro.read_bytes() + normal_main.read_bytes(),
    }
    fixtures = {}
    for name, payload in fixture_bytes.items():
        fixtures[name] = output / f'{name}.mp4'
        fixtures[name].write_bytes(payload)

    (output / 'fixtures.json').write_text(json.dumps({
        'files': {name: str(path) for name, path in fixtures.items()},
        'frames': (INTRO_SECONDS + MAIN_SECONDS) * FPS,
        'intro_frames': INTRO_SECONDS * FPS, 'main_frames': MAIN_SECONDS * FPS}))


if __name__ == '__main__':
    main()
