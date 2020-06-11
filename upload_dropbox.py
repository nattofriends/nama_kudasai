from argparse import ArgumentParser
from pathlib import Path
import hashlib
import logging

import dropbox

from common import load_config
from common import setup_logging

# DropboxContentHasher and StreamHasherWrapper are from
# https://github.com/dropbox/dropbox-api-content-hasher/blob/master/python/dropbox_content_hasher.py


DROPBOX_ROOT = Path('/nama_kudasai')


log = logging.getLogger(__name__)


class DropboxContentHasher(object):
    """
    Computes a hash using the same algorithm that the Dropbox API uses for the
    the "content_hash" metadata field.

    The digest() method returns a raw binary representation of the hash.  The
    hexdigest() convenience method returns a hexadecimal-encoded version, which
    is what the "content_hash" metadata field uses.

    This class has the same interface as the hashers in the standard 'hashlib'
    package.

    Example:

        hasher = DropboxContentHasher()
        with open('some-file', 'rb') as f:
            while True:
                chunk = f.read(1024)  # or whatever chunk size you want
                if len(chunk) == 0:
                    break
                hasher.update(chunk)
        print(hasher.hexdigest())
    """

    BLOCK_SIZE = 4 * 1024 * 1024

    def __init__(self):
        self._overall_hasher = hashlib.sha256()
        self._block_hasher = hashlib.sha256()
        self._block_pos = 0

        self.digest_size = self._overall_hasher.digest_size
        # hashlib classes also define 'block_size', but I don't know how people use that value

    def update(self, new_data):
        if self._overall_hasher is None:
            raise AssertionError(
                "can't use this object anymore; you already called digest()")

        assert isinstance(new_data, bytes), (
            "Expecting a byte string, got {!r}".format(new_data))

        new_data_pos = 0
        while new_data_pos < len(new_data):
            if self._block_pos == self.BLOCK_SIZE:
                self._overall_hasher.update(self._block_hasher.digest())
                self._block_hasher = hashlib.sha256()
                self._block_pos = 0

            space_in_block = self.BLOCK_SIZE - self._block_pos
            part = new_data[new_data_pos:(new_data_pos+space_in_block)]
            self._block_hasher.update(part)

            self._block_pos += len(part)
            new_data_pos += len(part)

    def _finish(self):
        if self._overall_hasher is None:
            raise AssertionError(
                "can't use this object anymore; you already called digest() or hexdigest()")

        if self._block_pos > 0:
            self._overall_hasher.update(self._block_hasher.digest())
            self._block_hasher = None
        h = self._overall_hasher
        self._overall_hasher = None  # Make sure we can't use this object anymore.
        return h

    def digest(self):
        return self._finish().digest()

    def hexdigest(self):
        return self._finish().hexdigest()

    def copy(self):
        c = DropboxContentHasher.__new__(DropboxContentHasher)
        c._overall_hasher = self._overall_hasher.copy()
        c._block_hasher = self._block_hasher.copy()
        c._block_pos = self._block_pos
        return c


class StreamHasherWrapper(object):
    """
    A wrapper around a file-like object (either for reading or writing)
    that hashes everything that passes through it.  Can be used with
    DropboxContentHasher or any 'hashlib' hasher.

    Example:

        hasher = DropboxContentHasher()
        with open('some-file', 'rb') as f:
            wrapped_f = StreamHasher(f, hasher)
            response = some_api_client.upload(wrapped_f)

        locally_computed = hasher.hexdigest()
        assert response.content_hash == locally_computed
    """

    def __init__(self, f, hasher):
        self._f = f
        self._hasher = hasher

    def close(self):
        return self._f.close()

    def flush(self):
        return self._f.flush()

    def fileno(self):
        return self._f.fileno()

    def tell(self):
        return self._f.tell()

    def read(self, *args):
        b = self._f.read(*args)
        self._hasher.update(b)
        return b

    def write(self, b):
        self._hasher.update(b)
        return self._f.write(b)

    def next(self):
        b = self._f.next()
        self._hasher.update(b)
        return b

    def readline(self, *args):
        b = self._f.readline(*args)
        self._hasher.update(b)
        return b

    def readlines(self, *args):
        bs = self._f.readlines(*args)
        for b in bs:
            self._hasher.update(b)
        return b


def upload(channel_directory, filename, filepath):
    config = load_config()

    dbx = dropbox.Dropbox(config['dropbox_api_access_token'])

    upload_chunk_size = config['dropbox_chunk_size_mb'] * 1024 * 1024

    full_path = DROPBOX_ROOT / channel_directory / filename

    log.info(f'Full upload path is {full_path}')

    total_size = filepath.stat().st_size
    total_chunks = (total_size // upload_chunk_size) + 1

    log.info(f'Uploading in {total_chunks} chunks')

    log.info(f'Starting session')
    session = dbx.files_upload_session_start(b'')

    uploaded = 0
    hasher = DropboxContentHasher()
    for chunk_num in range(total_chunks):
        log.info(f'Uploading chunk {chunk_num}')
        is_last_chunk = chunk_num == total_chunks - 1
        cursor = dropbox.files.UploadSessionCursor(
            session_id=session.session_id,
            offset=uploaded,
        )
        with open(filepath, 'rb') as fh:
            fh.seek(chunk_num * upload_chunk_size)
            wrapped_fh = StreamHasherWrapper(fh, hasher)
            data = wrapped_fh.read(upload_chunk_size)

            dbx.files_upload_session_append_v2(
                data,
                cursor,
                close=is_last_chunk,
            )

        uploaded += total_size % upload_chunk_size if is_last_chunk else upload_chunk_size

    log.info('Finishing session')
    file_metadata = dbx.files_upload_session_finish(
        b'',
        dropbox.files.UploadSessionCursor(
            session_id=session.session_id,
            offset=uploaded,
        ),
        dropbox.files.CommitInfo(
            path=str(full_path),
        ),
    )

    local_hash = hasher.hexdigest()
    remote_hash = file_metadata.content_hash

    # TODO: Actually take some sort of action based on this,
    # especially considering that downloaders log to some file
    # no one will ever see. Just retry reuploading it maybe?

    assert local_hash == remote_hash, f'Local hash {local_hash} and remote hash {remote_hash} do not match'

    # Get the shared link and thumbnail
    # XXX: Maybe we should fetch the Youtube thumbnail way earlier?
    shared_link = dbx.sharing_create_shared_link(str(full_path))
    _, thumbnail_resp = dbx.files_get_thumbnail(
        str(full_path),
        format=dropbox.files.ThumbnailFormat.png,
        size=dropbox.files.ThumbnailSize.w1024h768,
    )

    # This is probably pretty brittle
    url = shared_link.url.replace("www.dropbox", "dl.dropboxusercontent")
    return (url, thumbnail_resp.content)


def main():
    setup_logging()

    parser = ArgumentParser()
    parser.add_argument('channel_directory')
    parser.add_argument('local_path')
    args = parser.parse_args()

    local_path = Path(args.local_path)

    upload(
        args.channel_directory,
        local_path.name,
        local_path,
    )


if __name__ == '__main__':
    main()
