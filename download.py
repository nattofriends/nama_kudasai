from datetime import datetime
from datetime import timedelta
from pathlib import Path
from urllib.request import Request
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata

from streamlink_cli.main import main as streamlink_main
from tzlocal import get_localzone

from common import check_pid
from common import get_current_firefox_version
from common import get_opener
from common import get_video_info
from common import INNOCUOUS_UA
from common import load_config
from common import open_state
from common import setup_logging
from common import VideoInfoError
from notification import notify
from upload_dropbox import upload

POLL_THRESHOLD_SECS = 120

WORKDIR = Path('work')
LOGDIR = Path('logs')

HEARTBEAT_FIXED = {
    'context': {
        'client': {
            'browserName': 'Firefox',
            'clientName': 'WEB',
            'deviceMake': 'www',
            'deviceModel': 'www',
            'gl': 'US',
            'hl': 'en',
            'osName': 'Windows',
            'osVersion': '10.0',
        },
        'request': {},
    },
    'heartbeatRequestParams': {
        'heartbeatChecks': ['HEARTBEAT_CHECK_TYPE_LIVE_STREAM_STATUS']
    },
}

# This should probably log elsewhere too?
log = logging.getLogger(__name__)


def wait(video_info, player_response, config):
    # If it hasn't started yet, we wait until a short amount of time before
    # the scheduled start time, and then start polling. This will probably
    # be less disruptive than constantly polling for hours and hours.

    # microformat.playerMicroformatRenderer.liveBroadcastDetails.startTimestamp is less deep in there but would require us to parse a ISO8601 datetime (oh no)
    scheduled_start = int(
        player_response['playabilityStatus']['liveStreamability']['liveStreamabilityRenderer']['offlineSlate']['liveStreamOfflineSlateRenderer']['scheduledStartTime']
    )

    total_wait = scheduled_start - time.time()
    log.info(f'Stream is scheduled to start at {scheduled_start} (in {timedelta(seconds=total_wait)})')

    if total_wait > POLL_THRESHOLD_SECS:
        long_sleep = total_wait - POLL_THRESHOLD_SECS
        log.info(f'Going to sleep for {timedelta(seconds=long_sleep)} before polling')
        time.sleep(long_sleep)

    while True:
        # Use heartbeat endpoint like a real client because of rate limits
        # on get_video_info

        localzone = get_localzone()
        offset = int(localzone.utcoffset(datetime.now()).total_seconds() / 60)
        heartbeat_payload = HEARTBEAT_FIXED.copy()
        heartbeat_payload['videoId'] = player_response['videoDetails']['videoId']
        heartbeat_payload['context']['client'].update({
            'browserVersion': f'{get_current_firefox_version()}.0',
            'clientVersion': video_info['innertube_context_client_version'],
            'timeZone': localzone.zone,
            'utcOffsetMinutes': offset,
        })

        with get_opener() as opener:
            resp = opener.open(
                Request(
                    "https://www.youtube.com/youtubei/v1/player/heartbeat?alt=json&key={}".format(video_info['innertube_api_key']),
                    data=json.dumps(heartbeat_payload).encode('utf-8'),
                    headers={
                        "Content-Type": 'application/json',
                        "Host": "www.youtube.com",
                        'User-Agent': INNOCUOUS_UA.format(version=get_current_firefox_version()),
                    },
                )
            )

        heartbeat = json.loads(resp.read().decode('utf-8'))

        if 'offlineSlate' in heartbeat['playabilityStatus']['liveStreamability']['liveStreamabilityRenderer']:
            scheduled_start = int(
                heartbeat['playabilityStatus']['liveStreamability']['liveStreamabilityRenderer']['offlineSlate']['liveStreamOfflineSlateRenderer']['scheduledStartTime']
            )
            total_wait = scheduled_start - time.time()

            if total_wait > config['ignore_wait_greater_than_s']:
                log.info(f"{player_response['videoDetails']['videoId']} starts too far in the future, at {scheduled_start} (in {timedelta(seconds=total_wait)})")
                sys.exit(1)
            elif total_wait < -config['ignore_past_scheduled_start_greater_than_s']:
                log.info(f"{player_response['videoDetails']['videoId']} starts too far in the past, at {scheduled_start} ({timedelta(seconds=-total_wait)} ago)")
                sys.exit(1)

        status = heartbeat['playabilityStatus']['status']

        if status == 'OK':
            log.info(f'Video is no longer upcoming, time to go')
            return
        elif status == 'UNPLAYABLE':
            log.info(f'Video not playable: {heartbeat["playabilityStatus"]}, giving up')
            sys.exit(1)
        elif status == 'LIVE_STREAM_OFFLINE':
            poll_delay = int(heartbeat['playabilityStatus']['liveStreamability']['liveStreamabilityRenderer']['pollDelayMs']) / 1000.0
            log.info(f'Still offline, will sleep {poll_delay}s')
            time.sleep(poll_delay)
        else:
            raise NotImplementedError(f"Don't know what to do with playability status {status}: {heartbeat['playabilityStatus']}")

    # Should be unreachable...?
    return


def sanitize_filename(name):
    return re.sub(
        r'[\\\|\*\?/:"<>#]',
        '_',
        unicodedata.normalize('NFC', name),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-download', action='store_true')
    parser.add_argument('--no-remux', action='store_true')
    parser.add_argument('--no-upload', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--no-delete', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--force-log-to-file', action='store_true')
    parser.add_argument('--override-channel-name')
    parser.add_argument('--override-video-name')
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

    if args.override_channel_name and args.override_video_name:
        log.info('Using overridden channel and video name, setting is_upcoming to false')
        channel_name = args.override_channel_name
        video_name = args.override_video_name
        # There's no reason to use these overrides for an upcoming video
        is_upcoming = False
    else:
        video_info, player_response = get_video_info(args.video_id)
        if 'videoDetails' not in player_response:
            log.error(
                f'{args.video_id} has no details, cannot proceed '
                '(playability: {}, {})'.format(
                player_response["playabilityStatus"]["status"],
                player_response["playabilityStatus"]["reason"],
            ))
            sys.exit(1)
        else:
            channel_name = player_response['videoDetails']['author']
            video_name = player_response['videoDetails']['title']
            is_upcoming = player_response['videoDetails'].get('isUpcoming', False)

    log.info(f'Channel: {channel_name}')
    log.info(f'Title: {video_name}')
    log.info(f'Upcoming: {is_upcoming}')

    if is_upcoming:
        wait(video_info, player_response, config)

    filename_base = sanitize_filename(video_name)
    log.info(f'Filename base: {filename_base}')

    # Copy youtube-dl's naming scheme
    filepath_streamlink = WORKDIR / f'{filename_base}-{args.video_id}.ts'

    # TODO: If file already exists, rename it and concatenate it later?

    # XXX: youtube-dl used to be less reliable than streamlink for downloading
    # streams - that may no longer be the case.

    # XXX: Invoke this in a less hacky manner
    # The reason for doing this is that I wanted to use streamlink
    # inside the venv but in a separate process,
    # without hardcoding the path of the venv.
    streamlink_args = [
        '--force',  # Overwrite any existing file
        '--hls-timeout', '60',
        # XXX: This doesn't work right now!
        # See https://github.com/streamlink/streamlink/issues/2936
        '--hls-live-restart',
        '--retry-streams', '10',
        '--retry-max', '10',
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
        '-metadata', f'title={video_name}',
        '-metadata', f'artist={channel_name}',
        '-metadata', f'comment=https://www.youtube.com/watch?v={args.video_id}',
        filepath_output,
    )
    if not args.no_remux:
        log.info(f'Remuxing to mp4')
        subprocess.run(ffmpeg_args, stdout=log_file)
    else:
        log.info('Skipping remux')

    # Upload
    if not args.no_upload:
        link_url, thumbnail = upload(
            sanitize_filename(channel_name),
            # This argument duplication is kind of silly...
            filename_output,
            filepath_output,
        )

        # We won't have link and thumb if not uploading without
        # going through a bunch more effort.
        if not args.no_notify:
            notify(
                channel_name,
                video_name,
                link_url,
                thumbnail,
            )
        else:
            log.info('Skipping notify')
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
