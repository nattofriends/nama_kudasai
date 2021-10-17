from argparse import ArgumentParser
from datetime import timedelta
from enum import IntEnum
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen
from urllib import parse
import xml.etree.ElementTree as ET
import logging
import os
import subprocess
import sys
import time

import html5lib
import yaml

from common import INNOCUOUS_UA
from common import check_pid
from common import get_current_firefox_version
from common import get_opener
from common import get_video_info
from common import load_config
from common import load_state
from common import open_state
from common import setup_logging


# Sockets...
READ_TIMEOUT_S = 60.0

# ElementTree doesn't want to parse these for us or let us read xmlns tags...
FEED_NS = {
    'default': 'http://www.w3.org/2005/Atom',
    'media': 'http://search.yahoo.com/mrss/',
    'yt': 'http://www.youtube.com/xml/schemas/2015',
    # Live page
    'html': 'http://www.w3.org/1999/xhtml',
}

log = logging.getLogger(__name__)


class VideoState(IntEnum):
    AVAILABLE = 0
    NOT_LIVESTREAM = 1
    FINISHED = 2
    # Looks like the channel listing is only eventually consistent,
    # and may still show removed or unavailable videos for a while.
    # XXX: Should this just be called UNAVAILABLE or UNPLAYABLE?
    REMOVED = 3
    # Haven't seen these from the feed yet, only from /live.
    TOO_FAR_IN_FUTURE = 4
    NOT_SCHEDULED = 5
    UNKNOWN_RATE_LIMITED = 6
    TOO_FAR_IN_PAST = 7

    # From playabilityStatus
    PRIVATE = 8
    # Only known reason is membership only stream
    MEMBERS_ONLY = 9


def check_channel(config, args, channel, video_liveness_cache):
    log.info(f'Working on {channel}')
    # Flip this flag when we catch something in order to continue
    # but it's important enough to exit 1 overall.
    failed = False

    log.info(f'Fetching channel feed')
    video_ids = set()

    try:
        # This endpoint doesn't seem to honor If-Modified-Since, so I hope
        # they dont mind serving us data all the time
        with get_opener() as opener:
            resp = opener.open(
                Request(
                    f'https://www.youtube.com/feeds/videos.xml?channel_id={channel}',
                    headers={
                        'User-Agent': INNOCUOUS_UA.format(version=get_current_firefox_version()),
                    },
                ),
                timeout=READ_TIMEOUT_S,
            )

        # XML documents are not poorly-formed HTML documents!
        tree = ET.parse(resp).getroot()

        title = tree.find('default:title', FEED_NS).text
        log.info(f'{channel} is {title}')

        videos = tree.findall('default:entry', FEED_NS)
        video_liveness = {}

        video_ids = {
            video.find('yt:videoId', FEED_NS).text
            for video in videos
        }
    except HTTPError as e:
        log.error(f'Got HTTPError {e} while fetching channel feed')
        failed = True

    if args.skip_live_endpoint:
        log.info('Skipping /live endpoint')
    else:
        # Also check if there is anything happening at {channel}/live
        # XXX: Is this needed? It takes a while and is more janky than looking
        # at videos.xml... but testing doesn't really show how fast or slow
        # videos.xml updates. Polling videos.xml did pick up a video which
        # was scheduled less than 30 minutes in advance, but... who knows.
        log.info(f'Checking channel live endpoint')
        with get_opener() as opener:
            resp = opener.open(
                Request(
                    f'https://www.youtube.com/channel/{channel}/live',
                    headers={
                        # Too much JS if we pretend we're a modern browser
                        'User-Agent': '',
                    },
                ),
                timeout=READ_TIMEOUT_S,
            )

        tree = html5lib.parse(resp)
        canonical_link = tree.find(".//html:link[@rel='canonical']", FEED_NS)

        # Sometimes Youtube delivers a completely blank page, and there is no canonical link
        if canonical_link is not None:
            canonical_url = canonical_link.attrib['href']
            query = dict(parse.parse_qsl(parse.urlsplit(canonical_url).query))
            live_video_id = query.get('v')
            if live_video_id:
                log.info(f'Live endpoint shows {live_video_id} is active')
                if live_video_id in video_ids:
                    log.debug('Not adding because it was already in the feed')
                else:
                    log.info('Adding it to the list of videos')
                    video_ids.add(live_video_id)
            else:
                log.info('Canonical link is not a video, doing nothing')
        else:
            log.info('Canonical link not found in page, no video added from live endpoint')

    for video_id in video_ids:
        video_liveness[video_id] = check_video(
            config, video_id, video_liveness_cache.get(video_id)
        )

    # XXX: Probably can do better than updating this file
    # once per channel
    with open_state() as state:
        state_channel_videos = state.get('channel_videos', {})
        state_channel_videos[channel] = video_liveness
        state['channel_videos'] = state_channel_videos

    log.info(f'Done with {channel}')
    return failed


def check_video(config, video_id, cached_liveness):
    # Hilariously, we have already written all this parsing code once
    # before already...
    log.debug(f'Checking video {video_id}...')

    if cached_liveness == VideoState.NOT_LIVESTREAM:
        log.debug(f'{video_id} is not a live stream, skipping (cached)')
        return VideoState.NOT_LIVESTREAM
    elif cached_liveness == VideoState.FINISHED:
        log.debug(f'{video_id} appears to be finished, skipping (cached)')
        return VideoState.FINISHED
    elif cached_liveness == VideoState.NOT_SCHEDULED:
        log.debug(f'{video_id} does not have a scheduled start time, skipping (cached)')
        return VideoState.NOT_SCHEDULED
    # XXX: We should additionally store the scheduled start time
    # in order to be able to use the cached VideoState.TOO_FAR_IN_FUTURE.

    try:
        # `video_info` here is playerResponse
        video_info = get_video_info(video_id)
    except HTTPError as e:
        if e.code == 429:
            log.warning(f'Rate limited while getting video info for {video_id}, skipping (no backoff)')
            return VideoState.UNKNOWN_RATE_LIMITED
        raise e

    playability = video_info['playabilityStatus']

    log.warning('(playability: {}, {}, {})'.format(
        video_info["playabilityStatus"]["status"],
        video_info["playabilityStatus"].get("reason", '(no reason provided'),
        video_info["playabilityStatus"].get("messages", '(no messages provided)'),
    ))
    if playability == 'LOGIN_REQUIRED':
        # Read: Private video
        return VideoState.PRIVATE
    elif playability == 'ERROR':
        return VideoState.REMOVED
    elif playability == 'UNPLAYABLE':
        # Read: membership required
        return VideoState.MEMBERS_ONLY

    video_details = video_info['videoDetails']

    if not video_details['isLiveContent']:
        log.debug(f'{video_id} is not a live stream, skipping')
        return VideoState.NOT_LIVESTREAM

    # XXX: We aren't going to archive finished live content,
    # because we presumably already saved it while it was live.
    # For now.
    if video_details['lengthSeconds'] != "0":
        log.debug(f'{video_id} appears to be finished, skipping')
        return VideoState.FINISHED

    is_upcoming = video_info['videoDetails'].get('isUpcoming', False)
    if is_upcoming:
        scheduled_start = video_info['playabilityStatus']['liveStreamability']['liveStreamabilityRenderer']['offlineSlate']['liveStreamOfflineSlateRenderer'].get('scheduledStartTime')
        if scheduled_start is None:
            # It's not going to start any time soon. This shows up due to hitting
            # the persistent stream from the {channel}/live endpoint,
            # I don't think it shows up in the feed.
            log.info(f'{video_id} does not have a scheduled start time, skipping')
            return VideoState.NOT_SCHEDULED
        else:
            scheduled_start = int(scheduled_start)

            now = time.time()
            total_wait = scheduled_start - now

            if total_wait > config['ignore_wait_greater_than_s']:
                log.info(f'{video_id} starts too far in the future, at {scheduled_start} (in {timedelta(seconds=total_wait)})')
                return VideoState.TOO_FAR_IN_FUTURE
            elif total_wait < -config['ignore_past_scheduled_start_greater_than_s']:
                log.info(f'{video_id} starts too far in the past, at {scheduled_start} ({timedelta(seconds=-total_wait)} ago)')
                return VideoState.TOO_FAR_IN_PAST

    # Informational
    if not video_details.get('isUpcoming', False):
        log.info(f'{video_id} appears to have to started already')

    pid_exists, _ = check_pid(video_id)

    if pid_exists:
        log.info('Downloader is still active, not doing anything')
        return VideoState.AVAILABLE

    # Time to run!
    log.info(f'Starting downloader for {video_id} ({video_details["title"]}))')
    subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), 'download.py'), '--', video_id],
    )

    return VideoState.AVAILABLE


def main():
    parser = ArgumentParser()
    parser.add_argument('--skip-live-endpoint', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    retcode = 0

    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)

    config = load_config()

    state = load_state()
    cached_channel_state = state.get('channel_videos', {})

    for channel in config['channels']:
        check_result = check_channel(config, args, channel, cached_channel_state.get(channel, {}))
        retcode |= int(check_result)

    # XXX: Probably want to clean up old active_downloaders at some point


if __name__ == '__main__':
    sys.exit(main())
