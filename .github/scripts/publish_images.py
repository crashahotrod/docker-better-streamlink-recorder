"""Stage tested images, then assemble release tags from immutable digest receipts."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

PLATFORMS = {'amd64': ('linux', 'amd64', ''), 'arm64': ('linux', 'arm64', ''),
             'armv7': ('linux', 'arm', 'v7')}
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
COMPONENT = r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
REPOSITORY = re.compile(r'(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?' + COMPONENT + r'(?:/' + COMPONENT + r')*')


def repositories(metadata):
    result = {}
    for tag in metadata.get('tags', []):
        repository, separator, version = tag.rpartition(':')
        if (not separator or not REPOSITORY.fullmatch(repository)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', version)):
            raise ValueError('Invalid registry tag in image metadata')
        if tag not in result.setdefault(repository, []):
            result[repository].append(tag)
    if not result:
        raise ValueError('No publication tags in image metadata')
    return result


def stage(metadata, arch, directory, image='streamlink-recorder:test', environ=os.environ, run=subprocess.run):
    targets = repositories(metadata)
    suffix = f"ci-{environ['GITHUB_RUN_ID']}-{environ['GITHUB_RUN_ATTEMPT']}-{arch}"
    if arch not in PLATFORMS or not re.fullmatch(r'ci-[0-9]+-[0-9]+-(amd64|arm64|armv7)', suffix):
        raise ValueError('Invalid build run or architecture')
    configuration, = json.loads(run(['docker', 'image', 'inspect', image], check=True,
                                  capture_output=True, text=True).stdout)
    system, architecture, variant = PLATFORMS[arch]
    if (configuration['Os'] != system or configuration['Architecture'] != architecture
            or (variant and configuration.get('Variant') != variant)):
        raise ValueError('Loaded image does not match the tested platform')
    images = {}
    for repository in targets:
        temporary_tag = repository + ':' + suffix
        run(['docker', 'tag', image, temporary_tag], check=True)
        run(['docker', 'push', temporary_tag], check=True)
        configuration, = json.loads(run(['docker', 'image', 'inspect', temporary_tag],
            check=True, capture_output=True, text=True).stdout)
        for reference in configuration.get('RepoDigests', []):
            owner, separator, digest = reference.partition('@')
            if owner.removeprefix('docker.io/') == repository.removeprefix('docker.io/') and separator and DIGEST.fullmatch(digest):
                images[repository] = repository + '@' + digest
                break
        else:
            raise ValueError('Pushed image has no matching immutable registry digest')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {'arch': arch, 'run_id': environ['GITHUB_RUN_ID'],
               'run_attempt': environ['GITHUB_RUN_ATTEMPT'], 'commit': environ['GITHUB_SHA'], 'images': images}
    temporary = directory / (arch + '.tmp')
    temporary.write_text(json.dumps(receipt), encoding='utf-8')
    temporary.replace(directory / (arch + '.json'))


def merge(metadata, directory, environ=os.environ, run=subprocess.run):
    targets = repositories(metadata)
    sources = {repository: [] for repository in targets}
    # Validate every receipt before changing any release tag.
    for arch in PLATFORMS:
        receipt = json.loads((Path(directory) / (arch + '.json')).read_text(encoding='utf-8'))
        attempt = str(receipt.get('run_attempt', ''))
        if (receipt.get('arch') != arch or receipt.get('run_id') != environ['GITHUB_RUN_ID']
                or receipt.get('commit') != environ['GITHUB_SHA']
                or not re.fullmatch(r'[1-9][0-9]*', attempt)
                or int(attempt) > int(environ['GITHUB_RUN_ATTEMPT'])
                or set(receipt.get('images', {})) != set(targets)):
            raise ValueError('Missing or mismatched verified platform receipt')
        for repository, reference in receipt['images'].items():
            owner, separator, digest = reference.partition('@')
            if owner != repository or not separator or not DIGEST.fullmatch(digest):
                raise ValueError('Invalid platform digest receipt')
            sources[repository].append(reference)
    for repository, tags in targets.items():
        arguments = ['docker', 'buildx', 'imagetools', 'create']
        for tag in tags:
            arguments += ['--tag', tag]
        run([*arguments, *sources[repository]], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('stage', 'merge'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--arch', choices=PLATFORMS)
    args = parser.parse_args()
    metadata = json.loads(os.environ['IMAGE_METADATA'])
    if args.operation == 'stage':
        if args.arch is None:
            parser.error('stage requires --arch')
        stage(metadata, args.arch, args.directory)
    else:
        merge(metadata, args.directory)


if __name__ == '__main__':
    main()
