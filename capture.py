#!/usr/bin/env python3
"""Supervise an unsplit Streamlink capture and stop before scratch fills."""
import os
import pathlib
import shutil
import signal
import subprocess
import sys

from chat_archive import ChatArchive


def stop(process, grace=10):
    """Allow Streamlink to flush/close files before escalating a stuck capture."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    process.wait()


def capture(directory, command, minimum_gb, interval=5, usage=shutil.disk_usage, pass_fds=(), chat=None):
    process = None

    def interrupted(signum, _frame):
        raise InterruptedError(signum)

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if chat:
            chat.start()
        process = subprocess.Popen(command, start_new_session=True, pass_fds=pass_fds)
        while True:
            if minimum_gb and usage(directory).free <= minimum_gb * 1024 ** 3:
                print('[Capture] Scratch minimum free space reached; stopping capture with SIGINT. '
                      'Original bytes retained.', file=sys.stderr)
                return 75
            try:
                result = process.wait(timeout=interval)
                return result if result >= 0 else 128 - result
            except subprocess.TimeoutExpired:
                pass
    except InterruptedError as error:
        return 128 + error.args[0]
    except OSError as error:
        print(f'[Capture] {error}; original bytes retained.', file=sys.stderr)
        return 1
    finally:
        # Prevent a second shutdown signal from interrupting child cleanup.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if process is not None and process.poll() is None:
                stop(process)
            if chat:
                chat.stop()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    directory, separator, *command = sys.argv[1:]
    if separator != '--' or not command:
        raise SystemExit('Usage: capture.py DIRECTORY -- COMMAND [ARGS...]')
    # Preserve download.sh's writer lock if the controller is killed before its child.
    try:
        os.fstat(9)
        writer_fds = (9,)
    except OSError:
        writer_fds = ()
    raise SystemExit(capture(pathlib.Path(directory), command, int(os.environ.get('MIN_FREE_SPACE_GB') or '0'),
                             pass_fds=writer_fds, chat=ChatArchive(directory)))
