from datetime import timedelta
from pathlib import Path
import argparse
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata

from streamlink_cli.main import main as streamlink_main

from common import check_pid
from common import get_video_info
from common import load_config
from common import open_state
from common import setup_logging
from upload_dropbox import upload

POLL_THRESHOLD_SECS = 300
POLL_SLEEP_SECS = 10

WORKDIR = Path('work')
LOGDIR = Path('logs')


# This should probably log elsewhere too?
log = logging.getLogger(__name__)


def wait(video_info):
    # If it hasn't started yet, we wait until a short amount of time before
    # the scheduled start time, and then start polling. This will probably
    # be less disruptive than constantly polling for hours and hours.

    # microformat.playerMicroformatRenderer.liveBroadcastDetails.startTimestamp is less deep in there but would require us to parse a ISO8601 datetime (oh no)
    scheduled_start = int(video_info['playabilityStatus']['liveStreamability']['liveStreamabilityRenderer']['offlineSlate']['liveStreamOfflineSlateRenderer']['scheduledStartTime'])

    now = time.time()
    total_wait = scheduled_start - now
    log.info(f'Stream is scheduled to start at {scheduled_start} (in {timedelta(seconds=total_wait)})')

    if total_wait > POLL_THRESHOLD_SECS:
        long_sleep = total_wait - POLL_THRESHOLD_SECS
        log.info(f'Going to sleep for {timedelta(seconds=long_sleep)} before polling')
        time.sleep(long_sleep)

    while True:
        log.info(f'Sleeping for {POLL_SLEEP_SECS}s')
        time.sleep(POLL_SLEEP_SECS)

        video_info = get_video_info(video_info['videoDetails']['videoId'])
        if 'videoDetails' not in video_info:
            log.error(
                f'{args.video_id} has no details, cannot proceed '
                '(playability: {}, {})'.format(
                video_info["playabilityStatus"]["status"],
                video_info["playabilityStatus"]["reason"],
            ))
            sys.exit(1)

        if not video_info['videoDetails'].get('isUpcoming', False):
            log.info(f'Video is no longer upcoming, time to go')
            return

    # Should be unreachable...?
    return


def sanitize_filename(name):
    return re.sub(
        r'[\\\|\*\?/:"<>]',
        '_',
        unicodedata.normalize('NFC', name),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-download', action='store_true')
    parser.add_argument('--no-remux', action='store_true')
    parser.add_argument('--no-upload', action='store_true')
    parser.add_argument('--no-delete', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--force-log-to-file', action='store_true')
    parser.add_argument('video_id')

    args = parser.parse_args()
    config = load_config()

    log_filename = LOGDIR / f'{args.video_id}-{os.getpid()}.log' if not sys.stdout.isatty() or args.force_log_to_file else None
    # For subprocess
    log_file = log_filename.open('a') if log_filename else None
    if log_file:
        sys.stderr = sys.stdout = log_file

    setup_logging(filename=log_filename)

    log.info(f'Starting download for {args.video_id}')

    pid_exists, active_downloaders = check_pid(args.video_id)

    if pid_exists and not args.force:
        raise ValueError('Another downloader is still alive, exiting')
    else:
        active_downloaders[args.video_id] = os.getpid()
        with open_state() as state:
            state['active_downloaders'] = active_downloaders

    video_info = get_video_info(args.video_id)
    if 'videoDetails' not in video_info:
        log.error(
            f'{args.video_id} has no details, cannot proceed '
            '(playability: {}, {})'.format(
            video_info["playabilityStatus"]["status"],
            video_info["playabilityStatus"]["reason"],
        ))
        sys.exit(1)

    is_upcoming = video_info['videoDetails'].get('isUpcoming', False)

    log.info(f'Channel: {video_info["videoDetails"]["author"]}')
    log.info(f'Title: {video_info["videoDetails"]["title"]}')
    log.info(f'Upcoming: {is_upcoming}')

    if is_upcoming:
        # XXX: Also apply config ignore_wait_greater_than_seconds here?
        wait(video_info)

    filename_base = sanitize_filename(video_info['videoDetails']['title'])
    log.info(f'Filename base: {filename_base}')

    # Copy youtube-dl's naming scheme
    filepath_streamlink = WORKDIR / f'{filename_base}-{args.video_id}.ts'

    # XXX: If file already exists, rename it and concatenate it later?

    # XXX: youtube-dl used to be less reliable than streamlink for downloading
    # streams - that may no longer be the case.

    # XXX: Invoke this in a less hacky manner
    # The reason for doing this is that I wanted to use streamlink
    # inside the venv but in a separate process,
    # without hardcoding the path of the venv.
    streamlink_args = [
        '--force',  # Overwrite any existing file
        '--hls-timeout', '600',
        # XXX: This doesn't work right now!
        # See https://github.com/streamlink/streamlink/issues/2936
        '--hls-live-restart',
        '--retry-streams', '10',
        '--verbose',
        '-o', str(filepath_streamlink),
        f'https://www.youtube.com/watch?v={args.video_id}',
        'best',
    ]

    if not args.no_download:
        log.info(f'Starting streamlink with args: {streamlink_args}')
        fork_return = os.fork()
        if fork_return == 0:
            sys.argv = streamlink_args
            streamlink_main()
        else:
            os.wait()
    else:
        log.info('Skipping download')

    filename_output = f'{filename_base}-{args.video_id}.mp4'
    filepath_output = WORKDIR / filename_output
    ffmpeg_args = (
        'ffmpeg',
        '-y',
        '-i', filepath_streamlink,
        '-c', 'copy',
        '-movflags', 'faststart',
        filepath_output,
    )
    if not args.no_remux:
        log.info(f'Remuxing to mp4')
        subprocess.run(ffmpeg_args, stdout=log_file)
    else:
        log.info('Skipping remux')

    # Upload
    if not args.no_upload:
        upload(
            config['dropbox_api_access_token'],
            sanitize_filename(video_info['videoDetails']['author']),
            # This argument duplication is kind of silly...
            filename_output,
            filepath_output,
        )
    else:
        log.info('Skipping upload')

    if not args.no_delete:
        log.info('Deleting work files')
        filepath_streamlink.unlink()
        filepath_output.unlink()

        log.info('Cleaning up state')
        with open_state() as state:
            active_downloaders = state.get('active_downloaders', {})
            active_downloaders.pop(args.video_id, None)
    else:
        log.info('Skipping cleanup')

    log.info('All done!')


if __name__ == '__main__':
    main()
