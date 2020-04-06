# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 Radim Rehurek <radim@rare-technologies.com>
# Copyright (C) 2020 Nicolas Mitchell <ncls.mitchell@gmail.com>
#
# This code is distributed under the terms and conditions
# from the MIT License (MIT).
#
"""Implements file-like objects for reading and writing to/from Azure Storage Blob (ASB)."""

import io
import logging

import azure.storage.blob
import azure.core.exceptions

import smart_open.bytebuffer
import smart_open.constants

logger = logging.getLogger(__name__)

_BINARY_TYPES = (bytes, bytearray, memoryview)
"""Allowed binary buffer types for writing to the underlying Azure Storage Blob stream"""

SCHEME = "asb"
"""Supported scheme for Azure Storage Blob in smart_open endpoint URL"""

_MIN_MIN_PART_SIZE = _REQUIRED_CHUNK_MULTIPLE = 4 * 1024**2
"""Azure requires you to upload in multiples of 4MB, except for the last part."""

_DEFAULT_MIN_PART_SIZE = 64 * 1024**2
"""Default minimum part size for Azure Cloud Storage multipart uploads is 64MB"""

DEFAULT_BUFFER_SIZE = 4 * 1024**2
"""Default buffer size for working with Azure Storage Blob is 256MB
https://docs.microsoft.com/en-us/rest/api/storageservices/understanding-block-blobs--append-blobs--and-page-blobs
"""


def parse_uri(uri_as_string):
    sr = smart_open.utils.safe_urlsplit(uri_as_string)
    assert sr.scheme == SCHEME
    container_id = sr.netloc
    blob_id = sr.path.lstrip('/')
    return dict(scheme=SCHEME, container_id=container_id, blob_id=blob_id)


def open_uri(uri, mode, transport_params):
    parsed_uri = parse_uri(uri)
    kwargs = smart_open.utils.check_kwargs(open, transport_params)
    return open(parsed_uri['container_id'], parsed_uri['blob_id'], mode, **kwargs)


def open(
        container_id,
        blob_id,
        mode,
        buffer_size=DEFAULT_BUFFER_SIZE,
        client=None,  # type: azure.storage.blob.azure.storage.blob.BlobServiceClient
        ):
    """Open an Azure Storage Blob blob for reading or writing.

    Parameters
    ----------
    bucket_id: str
        The name of the bucket this object resides in.
    blob_id: str
        The name of the blob within the bucket.
    mode: str
        The mode for opening the object.  Must be either "rb" or "wb".
    buffer_size: int, optional
        The buffer size to use when performing I/O. For reading only.
    client: azure.storage.blob.azure.storage.blob.BlobServiceClient, optional
        The Azure Storage Blob client to use when working with azure-storage-blob.

    """
    if mode == smart_open.constants.READ_BINARY:
        return Reader(
            container_id,
            blob_id,
            buffer_size=buffer_size,
            line_terminator=smart_open.constants.BINARY_NEWLINE,
            client=client,
        )
    elif mode == smart_open.constants.WRITE_BINARY:
        return Writer(
            container_id,
            blob_id,
            client=client,
        )
    else:
        raise NotImplementedError('Azure Storage Blob support for mode %r not implemented' % mode)


class _RawReader(object):
    """Read an Azure Storage Blob file."""

    def __init__(self, asb_blob, size):
        # type: (azure.storage.blob.BlobClient, int) -> None
        self._blob = asb_blob
        self._size = size
        self._position = 0

    def seek(self, position):
        """Seek to the specified position (byte offset) in the Azure Storage Blob blob.

        :param int position: The byte offset from the beginning of the blob.

        Returns the position after seeking.
        """
        self._position = position
        return self._position

    def read(self, size=-1):
        if self._position >= self._size:
            return b''
        binary = self._download_blob_chunk(size)
        self._position += len(binary)
        return binary

    def _download_blob_chunk(self, size):
        position = self._position
        if position == self._size:
            #
            # When reading, we can't seek to the first byte of an empty file.
            # Similarly, we can't seek past the last byte.  Do nothing here.
            #
            return b''
        elif size == -1:
            stream = self._blob.download_blob(offset=position)
        else:
            stream = self._blob.download_blob(offset=position, length=size)
        if isinstance(stream, azure.storage.blob.StorageStreamDownloader):
            binary = stream.readall()
        else:
            binary = stream.read()
        return binary


class Reader(io.BufferedIOBase):
    """Reads bytes from Azure Blob Storage.

    Implements the io.BufferedIOBase interface of the standard library.

    :raises azure.core.exceptions.ResourceNotFoundError: Raised when the blob to read from does not exist.

    """
    def __init__(
            self,
            container,
            blob,
            buffer_size=DEFAULT_BUFFER_SIZE,
            line_terminator=smart_open.constants.BINARY_NEWLINE,
            client=None,  # type: azure.storage.blob.BlobServiceClient
    ):
        if client is None:
            client = azure.storage.blob.BlobServiceClient()
        container_client = client.get_container_client(container)  # type: azure.storage.blob.ContainerClient

        self._blob = container_client.get_blob_client(blob)
        if self._blob is None:
            raise azure.core.exceptions.ResourceNotFoundError('blob {} not found in {}'.format(blob, container))
        try:
            self._size = self._blob.get_blob_properties()['size']
        except:
            self._size = 0

        self._raw_reader = _RawReader(self._blob, self._size)
        self._current_pos = 0
        self._current_part_size = buffer_size
        self._current_part = smart_open.bytebuffer.ByteBuffer(buffer_size)
        self._eof = False
        self._line_terminator = line_terminator

        #
        # This member is part of the io.BufferedIOBase interface.
        #
        self.raw = None

    #
    # Override some methods from io.IOBase.
    #
    def close(self):
        """Flush and close this stream."""
        logger.debug("close: called")
        self._blob = None
        self._raw_reader = None

    def readable(self):
        """Return True if the stream can be read from."""
        return True

    def seekable(self):
        """If False, seek(), tell() and truncate() will raise IOError.

        We offer only seek support, and no truncate support."""
        return True

    #
    # io.BufferedIOBase methods.
    #
    def detach(self):
        """Unsupported."""
        raise io.UnsupportedOperation

    def seek(self, offset, whence=smart_open.constants.WHENCE_START):
        """Seek to the specified position.

        :param int offset: The offset in bytes.
        :param int whence: Where the offset is from.

        Returns the position after seeking."""
        logger.debug('seeking to offset: %r whence: %r', offset, whence)
        if whence not in smart_open.constants.WHENCE_CHOICES:
            raise ValueError('invalid whence, expected one of %r' % smart_open.constants.WHENCE_CHOICES)

        if whence == smart_open.constants.WHENCE_START:
            new_position = offset
        elif whence == smart_open.constants.WHENCE_CURRENT:
            new_position = self._current_pos + offset
        else:
            new_position = self._size + offset
        self._current_pos = new_position
        self._raw_reader.seek(new_position)
        logger.debug('current_pos: %r', self._current_pos)

        self._current_part.empty()
        self._eof = self._current_pos == self._size
        return self._current_pos

    def tell(self):
        """Return the current position within the file."""
        return self._current_pos

    def truncate(self, size=None):
        """Unsupported."""
        raise io.UnsupportedOperation

    def read(self, size=-1):
        """Read up to size bytes from the object and return them."""
        if size == 0:
            return b''
        elif size < 0:
            self._current_pos = self._size
            return self._read_from_buffer() + self._raw_reader.read()

        #
        # Return unused data first
        #
        if len(self._current_part) >= size:
            return self._read_from_buffer(size)

        #
        # If the stream is finished, return what we have.
        #
        if self._eof:
            return self._read_from_buffer()

        #
        # Fill our buffer to the required size.
        #
        self._fill_buffer(size)
        return self._read_from_buffer(size)

    def read1(self, size=-1):
        """This is the same as read()."""
        return self.read(size=size)

    def readinto(self, b):
        """Read up to len(b) bytes into b, and return the number of bytes
        read."""
        data = self.read(len(b))
        if not data:
            return 0
        b[:len(data)] = data
        return len(data)

    def readline(self, limit=-1):
        """Read up to and including the next newline.  Returns the bytes read."""
        if limit != -1:
            raise NotImplementedError('limits other than -1 not implemented yet')
        the_line = io.BytesIO()
        while not (self._eof and len(self._current_part) == 0):
            #
            # In the worst case, we're reading the unread part of self._current_part
            # twice here, once in the if condition and once when calling index.
            #
            # This is sub-optimal, but better than the alternative: wrapping
            # .index in a try..except, because that is slower.
            #
            remaining_buffer = self._current_part.peek()
            if self._line_terminator in remaining_buffer:
                next_newline = remaining_buffer.index(self._line_terminator)
                the_line.write(self._read_from_buffer(next_newline + 1))
                break
            else:
                the_line.write(self._read_from_buffer())
                self._fill_buffer()
        return the_line.getvalue()

    #
    # Internal methods.
    #
    def _read_from_buffer(self, size=-1):
        """Remove at most size bytes from our buffer and return them."""
        # logger.debug('reading %r bytes from %r byte-long buffer', size, len(self._current_part))
        size = size if size >= 0 else len(self._current_part)
        part = self._current_part.read(size)
        self._current_pos += len(part)
        # logger.debug('part: %r', part)
        return part

    def _fill_buffer(self, size=-1):
        size = size if size >= 0 else self._current_part._chunk_size
        while len(self._current_part) < size and not self._eof:
            bytes_read = self._current_part.fill(self._raw_reader)
            if bytes_read == 0:
                logger.debug('reached EOF while filling buffer')
                self._eof = True

    def __str__(self):
        return "(%s, %r, %r)" % (self.__class__.__name__, self._container.container_name, self._blob.blob_name)

    def __repr__(self):
        return "%s(container=%r, blob=%r, buffer_size=%r)" % (
            self.__class__.__name__, self._container.container_name, self._blob.blob_name, self._current_part_size,
        )


class Writer(io.BufferedIOBase):
    """Writes bytes to Azure Storage Blob.

    Implements the io.BufferedIOBase interface of the standard library."""

    def __init__(
            self,
            container,
            blob,
            client=None,  # type: azure.storage.blob.BlobServiceClient
    ):
        if client is None:
            client = azure.storage.blob.BlobServiceClient()
        self._client = client
        self._container_client = self._client.get_container_client(container)  # type: azure.storage.blob.ContainerClient
        self._blob = self._container_client.get_blob_client(blob)  # type: azure.storage.blob.BlobClient
        self._total_size = 0

        #
        # This member is part of the io.BufferedIOBase interface.
        #
        self.raw = None

    def flush(self):
        pass

    #
    # Override some methods from io.IOBase.
    #
    def close(self):
        logger.debug("closing")
        if not self.closed:
            self._client = None
        logger.debug("successfully closed")

    @property
    def closed(self):
        return self._client is None

    def writable(self):
        """Return True if the stream supports writing."""
        return True

    def tell(self):
        """Return the current stream position."""
        return self._total_size

    #
    # io.BufferedIOBase methods.
    #
    def detach(self):
        raise io.UnsupportedOperation("detach() not supported")

    def write(self, b):
        """Write the given bytes (binary string) to the Azure Storage Blob file.

        There's buffering happening under the covers, so this may not actually
        do any HTTP transfer right away."""

        if not isinstance(b, _BINARY_TYPES):
            raise TypeError("input must be one of %r, got: %r" % (_BINARY_TYPES, type(b)))
        self._total_size += len(b)
        self._blob.upload_blob(b)

        return len(b)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __str__(self):
        return "(%s, %r, %r)" % (self.__class__.__name__, self._container_client.container_name, self._blob.blob_name)

    def __repr__(self):
        return "%s(container=%r, blob=%r, min_part_size=%r)" % (
            self.__class__.__name__, self._container_client.container_name, self._blob.blob_name,
        )