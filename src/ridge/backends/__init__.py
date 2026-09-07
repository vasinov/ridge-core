"""Built-in Ridge resource backends."""

from ridge.backends.docker import DockerResource
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.backends.ssh import SshResource

__all__ = ["DockerResource", "LocalResource", "S3Resource", "SshResource"]
