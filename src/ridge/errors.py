"""Stable errors surfaced by Ridge backends and frontends."""


class RidgeError(Exception):
    """Base class for expected Ridge failures."""


class ConfigurationError(RidgeError):
    """The resource configuration is missing or invalid."""


class ResourceNotFoundError(RidgeError):
    """The requested resource is not registered."""


class ResourceUnavailableError(RidgeError):
    """A configured resource or its backend cannot currently be reached."""


class UnsupportedOperationError(RidgeError):
    """The resource does not implement the requested operation."""


class AuthorizationDeniedError(RidgeError):
    """Ridge policy does not permit the requested resource operation."""


class InvalidPathError(RidgeError):
    """A resource path is invalid or escapes its configured root."""


class PathNotFoundError(RidgeError):
    """A resource path does not exist."""


class PathTypeError(RidgeError):
    """A resource path has the wrong filesystem type."""


class DestinationExistsError(RidgeError):
    """A write would replace an existing destination without permission."""


class OutputLimitExceededError(RidgeError):
    """A read result exceeds its caller-provided bound."""


class ExecutionTimeoutError(RidgeError):
    """A command exceeded its execution deadline."""


class ExecutionError(RidgeError):
    """A command could not be started by a compute backend."""


class SourceChangedError(RidgeError):
    """A transfer source changed after Ridge took its snapshot."""


class TransferError(RidgeError):
    """A cross-resource transfer could not be completed."""


class ObjectNotFoundError(RidgeError):
    """A storage object does not exist."""


class StorageError(RidgeError):
    """An object-storage operation failed."""


class JobsUnavailableError(RidgeError):
    """This service instance has no durable job store configured."""


class JobNotFoundError(RidgeError):
    """A durable job identifier does not exist."""


class JobConflictError(RidgeError):
    """A durable job request conflicts with existing state."""


class LockConflictError(RidgeError):
    """Resource ownership conflicts with another session or operation."""


class LockOwnershipError(RidgeError):
    """A coordination token, scope, or recovery request is invalid."""
