# Copyright 2020 University of California Regents. All Rights Reserved.
# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import print_function

import contextlib
import os
import re
import shutil
import tempfile

import pyarrow as pa

from .base import FilesystemStore


class HDFSStore(FilesystemStore):
    """Uses HDFS as a store of intermediate data and training artifacts.

    Initialized from a `prefix_path` that can take one of the following forms:

    1. "hdfs://namenode01:8020/user/test/Cerebro"
    2. "hdfs:///user/test/Cerebro"
    3. "/user/test/Cerebro"

    The full path (including prefix, host, and port) will be used for all reads and writes to HDFS through Spark. If
    host and port are not provided, they will be omitted. If prefix is not provided (case 3), it will be prefixed to
    the full path regardless.

    The localized path (without prefix, host, and port) will be used for interaction with PyArrow. Parsed host and port
    information will be used to initialize PyArrow `HadoopFilesystem` if they are not provided through the `host` and
    `port` arguments to this initializer. These parameters will default to `default` and `0` if neither the path URL
    nor the arguments provide this information.
    """

    FS_PREFIX = 'hdfs://'
    URL_PATTERN = '^(?:(.+://))?(?:([^/:]+))?(?:[:]([0-9]+))?(?:(.+))?$'

    def __init__(self, prefix_path,
                 host=None, port=None, user=None, kerb_ticket=None,
                 extra_conf=None, temp_dir=None, *args, **kwargs):
        self._temp_dir = temp_dir

        prefix, url_host, url_port, path, path_offset = self.parse_url(prefix_path)
        self._check_url(prefix_path, prefix, path)
        self._url_prefix = prefix_path[:path_offset] if prefix else self.FS_PREFIX

        host = host or url_host or 'default'
        port = port or url_port or 0
        self._hdfs_kwargs = dict(host=host,
                                 port=port,
                                 user=user,
                                 kerb_ticket=kerb_ticket,
                                 extra_conf=extra_conf)
        self._hdfs = self._get_filesystem_fn()()

        super(HDFSStore, self).__init__(prefix_path, *args, **kwargs)

    def parse_url(self, url):
        match = re.search(self.URL_PATTERN, url)
        prefix = match.group(1)
        host = match.group(2)

        port = match.group(3)
        if port is not None:
            port = int(port)

        path = match.group(4)
        path_offset = match.start(4)
        return prefix, host, port, path, path_offset

    def path_prefix(self):
        return self._url_prefix

    def get_filesystem(self):
        return self._hdfs

    def get_local_output_dir_fn(self, run_id):
        temp_dir = self._temp_dir

        @contextlib.contextmanager
        def local_run_path():
            dirpath = tempfile.mkdtemp(dir=temp_dir)
            try:
                yield dirpath
            finally:
                shutil.rmtree(dirpath)

        return local_run_path

    def sync_fn(self, run_id):
        class SyncState(object):
            def __init__(self):
                self.fs = None
                self.uploaded = {}

        state = SyncState()
        get_filesystem = self._get_filesystem_fn()
        hdfs_root_path = self.get_run_path(run_id)

        def fn(local_run_path):
            if state.fs is None:
                state.fs = get_filesystem()

            hdfs = state.fs
            uploaded = state.uploaded

            # We need to swap this prefix from the local path with the absolute path, +1 due to
            # including the trailing slash
            prefix = len(local_run_path) + 1

            for local_dir, dirs, files in os.walk(local_run_path):
                hdfs_dir = os.path.join(hdfs_root_path, local_dir[prefix:])
                for file in files:
                    local_path = os.path.join(local_dir, file)
                    modified_ts = int(os.path.getmtime(local_path))

                    if local_path in uploaded:
                        last_modified_ts = uploaded.get(local_path)
                        if modified_ts <= last_modified_ts:
                            continue

                    hdfs_path = os.path.join(hdfs_dir, file)
                    with open(local_path, 'rb') as f:
                        hdfs.upload(hdfs_path, f)
                    uploaded[local_path] = modified_ts

        return fn

    def _get_filesystem_fn(self):
        hdfs_kwargs = self._hdfs_kwargs

        def fn():
            return pa.hdfs.connect(**hdfs_kwargs)
        return fn

    def _check_url(self, url, prefix, path):
        print('_check_url: {}'.format(prefix))
        if prefix is not None and prefix != self.FS_PREFIX:
            raise ValueError('Mismatched HDFS namespace for URL: {}. Found {} but expected {}'
                             .format(url, prefix, self.FS_PREFIX))

        if not path:
            raise ValueError('Failed to parse path from URL: {}'.format(url))

    @classmethod
    def filesystem_prefix(cls):
        return cls.FS_PREFIX