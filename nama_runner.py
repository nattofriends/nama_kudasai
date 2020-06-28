#!/usr/bin/env python3
import os
import subprocess
import sys
import time

RUN_INTERVAL_SEC = 60 * 5

if __name__ == "__main__":
    while True:
        subprocess.call(
            [sys.executable, os.path.join(os.path.dirname(__file__), 'nama_kudasai.py')],
        )
        time.sleep(RUN_INTERVAL_SEC)
