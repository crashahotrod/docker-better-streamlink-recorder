#!/usr/bin/env python3
"""Bounded-memory media normalization, completeness checks, and recovery tools."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from fractions import Fraction


class UnsafeMedia(Exception):
    pass


ERRORS = re.compile(r'non-existing PPS|decode_slice_header|no frame!|invalid data|'
                    r'error (?:while|opening|submitting|processing|decoding)|'
                    r'decoder thread returned error|corrupt|missing picture|'
                    r'non.monoton|invalid.*(?:dts|pts)|moov atom not found', re.I)
AV_TYPES = ('video', 'audio')
IO_ERRORS = re.compile(r'No space left|Permission denied|Read-only file system|Input/output error', re.I)


def atomic_json(path, value):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def boxes(source, start, end):
    """Yield validated box offsets, never loading a media-data payload."""
    position = start
    while position < end:
        source.seek(position)
        header = source.read(8)
        if len(header) != 8:
            raise UnsafeMedia(f'Truncated box header at {position}')
        size, kind = struct.unpack('>I4s', header)
        header_size = 8
        if size == 1:
            extended = source.read(8)
            if len(extended) != 8:
                raise UnsafeMedia('Truncated extended box size')
            size = struct.unpack('>Q', extended)[0]
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise UnsafeMedia(f'Invalid {kind!r} box size at {position}')
        yield kind, position, position + header_size, position + size
        position += size


def integer(source, offset, size=4, end=None):
    if end is not None and offset + size > end:
        raise UnsafeMedia('Field extends beyond its container box')
    source.seek(offset)
    value = source.read(size)
    if len(value) != size:
        raise UnsafeMedia('Truncated sample table')
    return int.from_bytes(value, 'big')


def movie_tracks(source, start, end):
    tracks = {}
    for kind, _, payload, stop in boxes(source, start, end):
        if kind != b'trak':
            continue
        track = {'samples': 0}

        def visit(begin, finish, depth=0):
            if depth > 8:
                raise UnsafeMedia('Track container nesting limit exceeded')
            for atom, _, body, limit in boxes(source, begin, finish):
                if atom in (b'mdia', b'minf', b'stbl'):
                    visit(body, limit, depth + 1)
                elif atom == b'tkhd':
                    version = integer(source, body, 1, limit)
                    if version not in (0, 1):
                        raise UnsafeMedia('Unknown track-header version')
                    track['id'] = integer(source, body + (20 if version else 12), end=limit)
                elif atom == b'hdlr':
                    if body + 12 > limit:
                        raise UnsafeMedia('Truncated track handler')
                    source.seek(body + 8)
                    track['handler'] = source.read(4).decode('ascii', 'replace')
                elif atom in (b'stsz', b'stz2'):
                    if body + 12 > limit:
                        raise UnsafeMedia('Truncated sample count')
                    track['samples'] = integer(source, body + 8)
                    count = track['samples']
                    if atom == b'stsz':
                        required = 0 if integer(source, body + 4) else count * 4
                    else:
                        bits = integer(source, body + 7, 1)
                        if bits not in (4, 8, 16):
                            raise UnsafeMedia('Invalid compact sample size')
                        required = (count * bits + 7) // 8
                    if body + 12 + required > limit:
                        raise UnsafeMedia('Truncated sample-size table')
                elif atom == b'mdhd':
                    version = integer(source, body, 1, limit)
                    if version not in (0, 1):
                        raise UnsafeMedia('Unknown media-header version')
                    track['timescale'] = integer(source, body + (20 if version else 12), end=limit)

        visit(payload, stop)
        if 'id' not in track or track['id'] in tracks:
            raise UnsafeMedia('Missing or duplicate track identity')
        tracks[track['id']] = track
        if len(tracks) > 64:
            raise UnsafeMedia('Track limit exceeded')
    if not tracks:
        raise UnsafeMedia('MP4 initialization contains no tracks')
    return tracks


def fragment_samples(source, start, end, tracks):
    for atom, _, payload, stop in boxes(source, start, end):
        if atom != b'traf':
            continue
        track_id = None
        samples = 0
        for kind, _, body, limit in boxes(source, payload, stop):
            if kind == b'tfhd':
                flags = integer(source, body, end=limit) & 0xffffff
                if flags & 1:
                    # Relocating fragments with absolute data offsets requires
                    # another repair strategy; retaining them is safer than guessing.
                    raise UnsafeMedia('Absolute fragment data offsets require manual recovery')
                track_id = integer(source, body + 4, end=limit)
            elif kind == b'trun':
                if body + 8 > limit:
                    raise UnsafeMedia('Truncated fragment sample count')
                count = integer(source, body + 4)
                flags = integer(source, body) & 0xffffff
                fields = sum(bool(flags & bit) for bit in (0x100, 0x200, 0x400, 0x800))
                required = 8 + 4 * bool(flags & 1) + 4 * bool(flags & 4) + 4 * count * fields
                if body + required > limit:
                    raise UnsafeMedia('Truncated fragment sample table')
                samples += count
        if track_id not in tracks:
            raise UnsafeMedia('Fragment references an unknown initialization track')
        tracks[track_id]['samples'] += samples


def periods(path):
    """Index independently initialized MP4 periods or identify a transport stream."""
    length = path.stat().st_size
    if not length:
        raise UnsafeMedia('Empty capture')
    with path.open('rb') as source:
        prefix = source.read(8)
        if prefix[4:8] not in (b'ftyp', b'styp'):
            # A repeated sync byte distinguishes TS without trusting its suffix.
            source.seek(0)
            probe = source.read(188 * 5)
            if len(probe) >= 376 and probe[0] == 0x47 and probe[188] == 0x47:
                return 'mpegts', [{'start': 0, 'end': length, 'tracks': {}}]
            raise UnsafeMedia('Unrecognized capture container; original retained')
        result = []
        current = None
        for kind, position, payload, end in boxes(source, 0, length):
            if integer(source, position) == 0:
                raise UnsafeMedia('Open-ended MP4 box cannot establish complete period boundaries')
            if kind == b'ftyp':
                if current is not None:
                    current['end'] = position
                    result.append(current)
                if len(result) >= 4096:
                    raise UnsafeMedia('Initialization period limit exceeded')
                current = {'start': position, 'end': length, 'tracks': {}, 'moov': False}
            elif current is None:
                raise UnsafeMedia('Capture does not begin with an initialization file type')
            elif kind == b'moov':
                if current['moov']:
                    raise UnsafeMedia('Repeated MOOV without an independent file-type boundary')
                current['tracks'] = movie_tracks(source, payload, end)
                current['moov'] = True
            elif kind == b'moof':
                if not current['moov']:
                    raise UnsafeMedia('Fragment precedes its initialization')
                fragment_samples(source, payload, end, current['tracks'])
        if current is not None:
            result.append(current)
        if not result or any(not period['moov'] for period in result):
            raise UnsafeMedia('Incomplete MP4 initialization')
        return 'mp4', result


def copy_range(source, destination, start, end):
    with source.open('rb') as reader, destination.open('xb') as writer:
        reader.seek(start)
        remaining = end - start
        while remaining:
            block = reader.read(min(1024 * 1024, remaining))
            if not block:
                raise UnsafeMedia('Capture shortened during normalization')
            writer.write(block)
            remaining -= len(block)


class Tools:
    def __init__(self, directory):
        self.directory = pathlib.Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.number = 0

    def run(self, label, args):
        self.number += 1
        base = self.directory / f'{self.number:04d}-{label}'
        stdout = base.with_suffix('.stdout')
        stderr = base.with_suffix('.stderr')
        started = time.monotonic()
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        errors = []
        io_errors = []
        with stderr.open(errors='replace') as messages:
            for line in messages:
                if ERRORS.search(line) and len(errors) < 20:
                    errors.append(line.rstrip())
                if IO_ERRORS.search(line) and len(io_errors) < 3:
                    io_errors.append(line.rstrip())
        atomic_json(base.with_suffix('.command.json'), {
            'command': list(map(str, args)), 'exit': process.returncode,
            'elapsed_seconds': round(time.monotonic() - started, 3), 'errors': errors})
        if io_errors:
            raise OSError('; '.join(io_errors))
        if process.returncode or errors:
            raise UnsafeMedia(f'{label}: exit={process.returncode}; ' + '; '.join(errors[:3]))
        return stdout

    def inspect(self, path, workspace, deep=True):
        output = self.run('probe', ['ffprobe', '-v', 'warning', '-count_packets',
            '-show_streams', '-show_format', '-show_data', '-of', 'json', str(path)])
        if output.stat().st_size > 16 * 1024 * 1024:
            raise UnsafeMedia('Probe metadata limit exceeded')
        probe = json.loads(output.read_text())
        streams = [s for s in probe.get('streams', []) if s.get('codec_type') in AV_TYPES]
        if not any(s['codec_type'] == 'video' for s in streams):
            raise UnsafeMedia('No video stream')
        signature_keys = ('codec_type', 'codec_name', 'codec_tag_string', 'profile', 'level',
                          'width', 'height', 'pix_fmt', 'sample_aspect_ratio', 'time_base',
                          'sample_rate', 'channels', 'channel_layout', 'extradata')
        for stream in streams:
            if int(stream.get('nb_read_packets', 0)) <= 0:
                raise UnsafeMedia('Empty media track')
        result = {'signature': [{k: s.get(k) for k in signature_keys} for s in streams],
                  'packets': [int(s['nb_read_packets']) for s in streams],
                  'stream_types': [s['codec_type'] for s in streams],
                  'track_ids': [int(s.get('id', '0'), 0) for s in streams],
                  'start_times': [float(s.get('start_time', 0)) for s in streams],
                  'format': probe.get('format', {}).get('format_name'),
                  'duration': float(probe.get('format', {}).get('duration', 0))}
        packet_file = self.run('packets', ['ffprobe', '-v', 'warning', '-show_packets',
            '-show_data_hash', 'sha256', '-show_entries', 'packet=stream_index,pts,dts,duration,data_hash',
            '-of', 'compact=p=0:nk=0', str(path)])
        hashes = {s['index']: hashlib.sha256() for s in streams}
        packet_timing = {index: hashlib.sha256() for index in hashes}
        hashed_packets = {index: 0 for index in hashes}
        current_index = None
        with packet_file.open() as packets:
            for line in packets:
                fields = dict(item.split('=', 1) for item in line.rstrip().split('|') if '=' in item)
                if 'stream_index' in fields:
                    current_index = int(fields['stream_index'])
                    if current_index in packet_timing:
                        packet_timing[current_index].update(','.join(fields.get(k, 'N/A') for k in ('pts', 'dts', 'duration')).encode() + b'\n')
                # Compact output may put data_hash on the line after packet side data.
                if current_index in hashes and 'data_hash' in fields:
                    hashes[current_index].update(fields['data_hash'].encode())
                    hashed_packets[current_index] += 1
        if [hashed_packets[s['index']] for s in streams] != result['packets']:
            raise UnsafeMedia('Packet fingerprint coverage is incomplete')
        result['packet_hashes'] = [hashes[s['index']].hexdigest() for s in streams]
        result['packet_timing_hashes'] = [packet_timing[s['index']].hexdigest() for s in streams]
        packet_file.unlink()
        if not deep:
            return result
        frame_file = self.run('decode', ['ffmpeg', '-nostdin', '-v', 'warning', '-xerror',
            '-err_detect', 'explode', '-i', str(path), '-map', '0:v?', '-map', '0:a?',
            '-fps_mode', 'passthrough', '-enc_time_base', '-1', '-f', 'framehash', '-'])
        counts = [0] * len(streams)
        frame_hashes = [hashlib.sha256() for _ in streams]
        samples = [0] * len(streams)
        timebases = {}
        first = [None] * len(streams)
        last = [None] * len(streams)
        timing_hashes = [hashlib.sha256() for _ in streams]
        types = [s['codec_type'] for s in streams]
        # Explicit mapping places all video tracks before all audio tracks.
        mapped = sorted(range(len(streams)), key=lambda i: types[i] != 'video')
        with frame_file.open() as frames:
            for line in frames:
                if line.startswith('#tb '):
                    stream, base = line[4:].split(':', 1)
                    timebases[int(stream)] = Fraction(base.strip())
                if line.startswith('#') or not line.strip():
                    continue
                fields = [field.strip() for field in line.split(',')]
                index = mapped[int(fields[0])]
                counts[index] += 1
                frame_hashes[index].update((fields[-1] + '\n').encode())
                timestamp = int(fields[2]) * timebases[int(fields[0])]
                duration = int(fields[3]) * timebases[int(fields[0])]
                samples[index] += (duration * int(streams[index]['sample_rate'])
                                   if types[index] == 'audio' else 1)
                if first[index] is None:
                    first[index] = timestamp
                if last[index] is not None and timestamp < last[index] - duration:
                    raise UnsafeMedia('Decoded timestamps move backwards')
                last[index] = timestamp + duration
                timing_hashes[index].update(f'{timestamp - first[index]},{duration}\n'.encode())
        for i, kind in enumerate(types):
            if kind == 'video' and counts[i] != result['packets'][i]:
                raise UnsafeMedia(f'Decoded {counts[i]} of {result["packets"][i]} video samples')
            if counts[i] == 0:
                raise UnsafeMedia('Track produced no decoded frames')
        result.update(frames=counts, samples=[round(value) for value in samples],
                      frame_hashes=[h.hexdigest() for h in frame_hashes],
                      timing_hashes=[h.hexdigest() for h in timing_hashes],
                      decoded_start=[str(t) for t in first],
                      decoded_duration=[str(end - begin) for begin, end in zip(first, last)],
                      _frame_file=str(frame_file))
        return result


def public(report):
    return {key: value for key, value in report.items() if not key.startswith('_')}


def compare(expected, actual, lossless=True, packet_copy=False):
    if expected['stream_types'] != actual['stream_types']:
        if sorted(expected['stream_types']) != sorted(actual['stream_types']):
            raise UnsafeMedia('Track layout changed')
        # FFmpeg maps video before audio. Compare corresponding tracks while
        # preserving the report's actual order and the order within each type.
        available = {kind: iter([i for i, value in enumerate(actual['stream_types'])
                                if value == kind])
                     for kind in set(actual['stream_types'])}
        order = [next(available[kind]) for kind in expected['stream_types']]
        actual = actual.copy()
        for key in ('signature', 'packets', 'stream_types', 'track_ids', 'start_times',
                    'packet_hashes', 'packet_timing_hashes', 'frames', 'samples',
                    'frame_hashes', 'timing_hashes', 'decoded_start', 'decoded_duration'):
            if key in actual:
                actual[key] = [actual[key][i] for i in order]
    if expected['packets'] != actual['packets'] and lossless:
        raise UnsafeMedia('Packet/sample counts changed')
    if packet_copy:
        if expected['packet_hashes'] != actual['packet_hashes']:
            raise UnsafeMedia('Published packet content changed')
        if (expected['packet_timing_hashes'] != actual['packet_timing_hashes'] or
                expected['signature'] != actual['signature']):
            raise UnsafeMedia('Published packet timing or codec settings changed')
        return
    for i, kind in enumerate(expected['stream_types']):
        if kind == 'video':
            if expected['frames'][i] != actual['frames'][i]:
                raise UnsafeMedia('Video frames dropped or duplicated')
            if lossless and expected['frame_hashes'][i] != actual['frame_hashes'][i]:
                raise UnsafeMedia('Stream copy changed decoded video content')
            if expected['timing_hashes'][i] != actual['timing_hashes'][i]:
                raise UnsafeMedia('Video presentation timing changed')
        else:
            rate = int(expected['signature'][i].get('sample_rate') or 48000)
            if abs(expected['samples'][i] - actual['samples'][i]) > 2048:
                raise UnsafeMedia('Audio sample coverage changed beyond codec priming')
            if abs(expected['start_times'][i] - min(expected['start_times']) -
                   actual['start_times'][i] + min(actual['start_times'])) > max(0.1, 2048 / rate):
                raise UnsafeMedia('Audio/video alignment changed')


def combined(reports):
    result = public(reports[0]).copy()
    result['packets'] = [sum(r['packets'][i] for r in reports) for i in range(len(result['packets']))]
    result['frames'] = [sum(r['frames'][i] for r in reports) for i in range(len(result['frames']))]
    result['samples'] = [sum(r['samples'][i] for r in reports) for i in range(len(result['samples']))]
    hashes = [hashlib.sha256() for _ in result['stream_types']]
    timing = [hashlib.sha256() for _ in hashes]
    offsets = [Fraction(0) for _ in hashes]
    mapped = sorted(range(len(hashes)), key=lambda i: result['stream_types'][i] != 'video')
    for report in reports:
        timebases = {}
        with open(report['_frame_file']) as frames:
            for line in frames:
                if line.startswith('#tb '):
                    stream, base = line[4:].split(':', 1)
                    timebases[int(stream)] = Fraction(base.strip())
                if line.startswith('#') or not line.strip():
                    continue
                fields = [f.strip() for f in line.split(',')]
                index = mapped[int(fields[0])]
                hashes[index].update((fields[-1] + '\n').encode())
                timestamp = int(fields[2]) * timebases[int(fields[0])]
                duration = int(fields[3]) * timebases[int(fields[0])]
                timing[index].update(f'{timestamp - Fraction(report["decoded_start"][index]) + offsets[index]},{duration}\n'.encode())
        offsets = [offset + Fraction(duration) for offset, duration in zip(offsets, report['decoded_duration'])]
    result['frame_hashes'] = [h.hexdigest() for h in hashes]
    result['timing_hashes'] = [h.hexdigest() for h in timing]
    result['duration'] = sum(r['duration'] for r in reports)
    return result


def normalize(job):
    job = pathlib.Path(job).resolve()
    if (job / 'uploaded').exists() or (job / 'published.sha256').exists():
        raise UnsafeMedia('Legacy success receipt requires manual reconciliation before normalization')
    source = next((job / name for name in ('capture.bin', 'media.ts', 'media.mp4')
                   if (job / name).is_file()), None)
    if source is None:
        raise UnsafeMedia('Missing recovery input')
    attempt = job / 'diagnostics' / f'normalize-{time.time_ns()}'
    tools = Tools(attempt)
    container, indexed = periods(source)
    source_hash = digest(source)
    remux = (job / 'remux-required').read_text().strip() == 'true' if (job / 'remux-required').exists() else False
    parts = job / 'parts'
    if parts.exists():
        # Only incomplete generated candidates are replaced, never the input.
        shutil.rmtree(parts)
    with tempfile.TemporaryDirectory(prefix='.normalizing.', dir=job) as temporary:
        work = pathlib.Path(temporary)
        prepared = []
        for i, period in enumerate(indexed, 1):
            path = work / f'period-{i:04d}.{ "mp4" if container == "mp4" else "ts"}'
            copy_range(source, path, period['start'], period['end'])
            report = tools.inspect(path, work)
            for track in period['tracks'].values():
                if track.get('handler') in ('vide', 'soun'):
                    try:
                        index = report['track_ids'].index(track['id'])
                    except ValueError as error:
                        raise UnsafeMedia('Declared track missing from demuxer') from error
                    if track['samples'] != report['packets'][index]:
                        raise UnsafeMedia('Declared MP4 samples were not fully demuxed')
            prepared.append((path, report, period))
        groups = []
        for item in prepared:
            if groups and groups[-1][-1][1]['signature'] == item[1]['signature']:
                groups[-1].append(item)
            else:
                groups.append([item])
        output_parts = work / 'parts'
        output_parts.mkdir()
        manifest = {'version': 1, 'capture': {'file': source.name, 'sha256': source_hash,
                    'format': container, 'size': source.stat().st_size}, 'periods': [], 'parts': []}
        for i, (path, report, period) in enumerate(prepared, 1):
            manifest['periods'].append({'number': i, 'start': period['start'], 'end': period['end'],
                'tracks': period['tracks'], 'validation': public(report)})
        pending = list(groups)
        while pending:
            group = pending.pop(0)
            number = len(manifest['parts']) + 1
            extension = 'ts' if container == 'mpegts' and not remux else 'mp4'
            candidate = output_parts / f'{number:04d}.{extension}'
            expected = combined([item[1] for item in group])
            try:
                if extension == 'ts':
                    shutil.copyfile(group[0][0], candidate)
                    actual = group[0][1]
                else:
                    command = ['ffmpeg', '-nostdin', '-v', 'warning', '-n']
                    if len(group) == 1:
                        command += ['-i', str(group[0][0])]
                    else:
                        listing = work / f'concat-{number:04d}.txt'
                        listing.write_text(''.join(f"file '{item[0].name}'\n" for item in group))
                        command += ['-f', 'concat', '-safe', '1', '-i', str(listing)]
                    tools.run('streamcopy', command + ['-map', '0:v?', '-map', '0:a?',
                        '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(candidate)])
                    actual = tools.inspect(candidate, work)
                    compare(expected, actual)
            except UnsafeMedia as error:
                if len(group) == 1:
                    raise
                # Individually verified periods remain usable when a concat
                # changes A/V timing (for example AAC priming at a boundary).
                atomic_json(attempt / f'separate-group-{number:04d}.json',
                            {'reason': str(error), 'periods': len(group)})
                candidate.unlink(missing_ok=True)
                pending[0:0] = [[item] for item in group]
                continue
            manifest['parts'].append({'id': f'{number:04d}', 'file': f'parts/{candidate.name}',
                'extension': extension, 'sha256': digest(candidate), 'validation': public(actual)})
        if digest(source) != source_hash:
            raise UnsafeMedia('Recovery input changed during normalization')
        os.replace(output_parts, parts)
        atomic_json(job / 'normalized.json', manifest)
        atomic_json(job / 'validation.json', {'status': 'verified', 'parts': len(manifest['parts']),
                    'periods': len(prepared), 'diagnostics': str(attempt.relative_to(job))})
    # Frame hashes are large for long VODs; compact digests persist in the manifest.
    for path in attempt.glob('*.stdout'):
        path.unlink()


def load_part(job, part):
    manifest = json.loads((job / 'normalized.json').read_text())
    validate_manifest(manifest)
    return next(item for item in manifest['parts'] if item['id'] == part)


def validate_manifest(manifest):
    """Reject damaged bookkeeping before it can acknowledge an incomplete job."""
    try:
        if manifest['version'] != 1:
            raise ValueError('Unsupported manifest version')
        capture = manifest['capture']
        if capture['file'] not in ('capture.bin', 'media.ts', 'media.mp4'):
            raise ValueError('Invalid recovery input path')
        if type(capture['size']) is not int or capture['size'] <= 0:
            raise ValueError('Invalid capture size')
        periods, parts = manifest['periods'], manifest['parts']
        if not 1 <= len(parts) <= len(periods) <= 4096:
            raise ValueError('Missing or excessive periods/parts')
        position = 0
        for number, period in enumerate(periods, 1):
            if (period['number'] != number or period['start'] != position or
                    type(period['end']) is not int or period['end'] <= position):
                raise ValueError('Source period coverage is incomplete or unordered')
            position = period['end']
        if position != capture['size']:
            raise ValueError('Source periods do not cover the complete capture')
        for number, part in enumerate(parts, 1):
            identity = f'{number:04d}'
            if (part['id'] != identity or part['extension'] not in ('mp4', 'ts') or
                    part['file'] != f'parts/{identity}.{part["extension"]}'):
                raise ValueError('Invalid, duplicated or unordered part identity')
        for item in [capture, *parts]:
            if not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
                raise ValueError('Invalid media checksum')

        def totals(items):
            result = {'video': 0, 'audio': 0}
            for item in items:
                report = item['validation']
                types, frames, packets = report['stream_types'], report['frames'], report['packets']
                if ('video' not in types or len(types) != len(frames) or len(types) != len(packets) or
                        any(kind not in AV_TYPES for kind in types) or
                        any(type(n) is not int or n <= 0 for n in [*frames, *packets])):
                    raise ValueError('Invalid stream/sample accounting')
                for kind, count, packet_count in zip(types, frames, packets):
                    if kind == 'video' and count != packet_count:
                        raise ValueError('Incomplete video decoding in manifest')
                    result[kind] += packet_count
            return result

        if totals(periods) != totals(parts):
            raise ValueError('Output parts do not account for every source sample')
    except (KeyError, TypeError, ValueError) as error:
        raise UnsafeMedia(f'Invalid manifest: {error}') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('normalize', 'check', 'recover'):
        command = sub.add_parser(name)
        command.add_argument('job', type=pathlib.Path)
    check = sub.add_parser('verify')
    check.add_argument('job', type=pathlib.Path)
    check.add_argument('part')
    check.add_argument('file', type=pathlib.Path)
    check.add_argument('--encoded', action='store_true')
    check.add_argument('--publication', action='store_true')
    args = parser.parse_args()
    try:
        if args.command in ('normalize', 'recover'):
            if (args.job / 'normalized.json').exists():
                raise UnsafeMedia('Already normalized; inspect existing state before recovery')
            normalize(args.job)
        elif args.command == 'check':
            manifest = json.loads((args.job / 'normalized.json').read_text())
            validate_manifest(manifest)
            if (args.job / manifest['capture']['file']).stat().st_size != manifest['capture']['size']:
                raise UnsafeMedia('Recovery input size changed')
            if digest(args.job / manifest['capture']['file']) != manifest['capture']['sha256']:
                raise UnsafeMedia('Recovery input checksum changed')
            for part in manifest['parts']:
                if not re.fullmatch(r'parts/[0-9]{4}\.(mp4|ts)', part['file']):
                    raise UnsafeMedia('Invalid normalized part path')
                if digest(args.job / part['file']) != part['sha256']:
                    raise UnsafeMedia('Normalized input checksum changed')
        else:
            part = load_part(args.job, args.part)
            expected = part['validation']
            receipt = args.job / 'parts' / f'{args.part}.encoded.json'
            if args.publication and receipt.exists():
                expected = json.loads(receipt.read_text())
            tools = Tools(args.job / 'diagnostics' / f'verify-{time.time_ns()}')
            actual = tools.inspect(args.file, args.job, deep=not args.publication)
            if args.encoded:
                log = args.job / 'diagnostics' / f'encode-{args.part}.stderr'
                if log.exists():
                    with log.open(errors='replace') as messages:
                        if any(ERRORS.search(line) for line in messages):
                            raise UnsafeMedia('Encoder reported errors despite successful exit')
            compare(expected, actual, lossless=not args.encoded,
                    packet_copy=args.publication)
            if args.encoded:
                atomic_json(receipt, public(actual))
            for path in tools.directory.glob('*.stdout'):
                path.unlink()
    except UnsafeMedia as error:
        atomic_json(args.job / 'validation.json', {'status': 'quarantine', 'reason': str(error)})
        print(f'[Media] {error}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, StopIteration) as error:
        atomic_json(args.job / 'validation.json', {'status': 'retry', 'reason': str(error)})
        print(f'[Media] {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
