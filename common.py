from contextlib import contextmanager
from http.cookiejar import LWPCookieJar
from urllib.request import HTTPCookieProcessor
from urllib.request import Request
from urllib.request import build_opener
from urllib.parse import parse_qs
import datetime
import json
import logging
import os

import yaml


log = logging.getLogger(__name__)

STATE_FILENAME = 'state.json'
COOKIES_FILENAME = 'cookies.txt'
# Firefox first began being released in a 4 week cycle in Q1 2020: https://hacks.mozilla.org/2019/09/moving-firefox-to-a-faster-4-week-release-cycle/
FIREFOX_RELEASE_BASE = 74
# Increase the version 3 weeks into the release cycle.
FIREFOX_RELEASE_CYCLE_BASE = datetime.date(2020, 2, 11) + datetime.timedelta(weeks=3)
INNOCUOUS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"


def get_current_firefox_version():
    interval = datetime.date.today() - FIREFOX_RELEASE_CYCLE_BASE
    weeks = interval.days / 7
    # 4 week release cycle
    version_increases = int(weeks / 4)
    current_version = FIREFOX_RELEASE_BASE + version_increases
    return current_version


# TODO: Actually increase logging levels when -v's are passed
def setup_logging(filename=None, level=logging.INFO):
    logging.basicConfig(
        format='[%(asctime)s] [%(levelname)s] %(message)s',
        level=level,
        filename=filename,
    )


def load_config():
    with open('config.yaml') as fh:
        return yaml.safe_load(fh)


def load_state():
    state = {}
    if os.path.exists('state.json'):
        with open('state.json') as fh:
            try:
                state = json.load(fh)
            except json.JSONDecodeError:
                return {}
    return state


@contextmanager
def open_state():
    state = load_state()

    yield state

    with open(STATE_FILENAME, 'w') as fh:
        json.dump(state, fh, indent=2)


def check_pid(video_id):
    state = load_state()
    active_downloaders = state.get('active_downloaders', {})

    if video_id in active_downloaders:
        other_pid = active_downloaders[video_id]
        log.info(f'{video_id} is being downloaded by pid {other_pid}')
        try:
            os.kill(other_pid, 0)
            log.info(f'Downloader {other_pid} from state is still alive')
            return (True, active_downloaders)
        except OSError:
            log.info(f'Downloader pid {other_pid} from state is dead')
            return (False, active_downloaders)
    else:
        return (False, active_downloaders)


class VideoInfoError(Exception):
    pass


@contextmanager
def get_opener():
    jar = LWPCookieJar(COOKIES_FILENAME)
    if os.path.exists(COOKIES_FILENAME):
        jar.load(ignore_discard=True)

    opener = build_opener(HTTPCookieProcessor(jar))
    yield opener
    jar.save(ignore_discard=True)

def get_video_info(video_id):
    # XXX: This request was getting blocked with 429 Too Many Requests after a period of time accessing
    # over IPv6.

    with get_opener() as opener:
        resp = opener.open(
            Request(
                f'https://www.youtube.com/get_video_info?video_id={video_id}',
                headers={
                    "Accept": "text/html",
                    "Dnt": "1",
                    "Host": "www.youtube.com",
                    "Upgrade-Insecure-Requests": "1",
                    'User-Agent': INNOCUOUS_UA.format(version=get_current_firefox_version()),
                },
            ),
        )

    resp_bytes = resp.read()

    video_info = {
        k: v[0]
        for k, v
        in parse_qs(resp_bytes.decode('utf-8')).items()
    }

    # This tends to happen right around when the livestream starts, not sure why
    if video_info['status'] != 'ok':
        raise VideoInfoError(video_info)

    try:
        player_response = json.loads(video_info['player_response'])
    except KeyError as e:
        # This shouldn't happen, but looks like it does.
        print("Dumping video_info:")
        print(json.dumps(video_info, indent=2))
        raise e

    return video_info, player_response
