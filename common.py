from contextlib import contextmanager
from urllib.request import urlopen
from urllib.parse import parse_qs
import json
import logging
import os

import yaml


log = logging.getLogger(__name__)

STATE_FILENAME = 'state.json'


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
        json.dump(state, fh, indent=4)


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


def get_video_info(video_id):
    resp = urlopen(
        f'https://www.youtube.com/get_video_info?video_id={video_id}'
    )

    video_info = {
        k: v[0]
        for k, v
        in parse_qs(resp.read().decode('utf-8')).items()
    }
    # The outer info in video_info is pretty useless.
    video_info = json.loads(video_info['player_response'])

    return video_info
