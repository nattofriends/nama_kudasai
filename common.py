from contextlib import contextmanager
from urllib.request import Request
from urllib.request import urlopen
from urllib.parse import parse_qs
import json
import logging
import os

import yaml


log = logging.getLogger(__name__)

STATE_FILENAME = 'state.json'
INNOCUOUS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0"


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


def get_video_info(video_id):
    resp = urlopen(
        Request(
            f'https://www.youtube.com/get_video_info?video_id={video_id}',
            headers={
                'User-Agent': INNOCUOUS_UA,
            },
        ),
    )

    video_info = {
        k: v[0]
        for k, v
        in parse_qs(resp.read().decode('utf-8')).items()
    }

    # This tends to happen right around when the livestream starts, not sure why
    if video_info['status'] != 'ok':
        raise VideoInfoError(video_info)

    try:
        # The outer info in video_info is pretty useless.
        video_info = json.loads(video_info['player_response'])
    except KeyError as e:
        # This shouldn't happen, but looks like it does.
        print("Dumping video_info:")
        print(json.dumps(video_info, indent=2))
        raise e

    return video_info
