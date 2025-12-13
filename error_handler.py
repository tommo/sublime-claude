"""Error handling utilities and decorators."""
import json
from typing import Callable, Any, Dict, TypeVar, Optional
from functools import wraps

T = TypeVar('T')


def handle_json_error(default: Any = None) -> Callable:
    """Decorator to handle JSON parsing errors.

    Args:
        default: Default value to return on error (or None to return error dict)

    Returns:
        Decorated function that handles JSON errors
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except json.JSONDecodeError as e:
                if default is not None:
                    return default
                return {"error": f"Invalid JSON: {e}"}
            except Exception as e:
                if default is not None:
                    return default
                return {"error": str(e)}
        return wrapper
    return decorator


def handle_file_error(default: Any = None) -> Callable:
    """Decorator to handle file operation errors.

    Args:
        default: Default value to return on error (or None to return error dict)

    Returns:
        Decorated function that handles file errors
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except FileNotFoundError:
                if default is not None:
                    return default
                return {"error": "File not found"}
            except PermissionError:
                if default is not None:
                    return default
                return {"error": "Permission denied"}
            except IsADirectoryError:
                if default is not None:
                    return default
                return {"error": "Expected file, got directory"}
            except Exception as e:
                if default is not None:
                    return default
                return {"error": str(e)}
        return wrapper
    return decorator


def handle_sublime_error(default: Any = None, log: bool = True) -> Callable:
    """Decorator to handle Sublime Text API errors.

    Args:
        default: Default value to return on error
        log: Whether to log errors to console

    Returns:
        Decorated function that handles Sublime errors
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if log:
                    print(f"[Claude] Error in {func.__name__}: {e}")
                if default is not None:
                    return default
                return {"error": str(e)}
        return wrapper
    return decorator


def safe_json_load(file_path: str, default: Any = None) -> Any:
    """Safely load JSON from a file.

    Args:
        file_path: Path to JSON file
        default: Default value to return on error (empty dict if None)

    Returns:
        Loaded JSON data or default value
    """
    if default is None:
        default = {}

    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return default
    except Exception:
        return default


def safe_json_dump(data: Any, file_path: str) -> bool:
    """Safely dump JSON to a file.

    Args:
        data: Data to serialize
        file_path: Path to write to

    Returns:
        True if successful, False otherwise
    """
    try:
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


def with_error_dict(func: Callable) -> Callable:
    """Decorator that ensures function returns dict with 'error' key on exception.

    Returns:
        Decorated function that always returns a dict
    """
    @wraps(func)
    def wrapper(*args, **kwargs) -> Dict:
        try:
            result = func(*args, **kwargs)
            # If result is not a dict, wrap it
            if not isinstance(result, dict):
                return {"result": result}
            return result
        except Exception as e:
            return {"error": str(e)}
    return wrapper


class ErrorContext:
    """Context manager for error handling with cleanup."""

    def __init__(
        self,
        error_message: str = "An error occurred",
        cleanup: Optional[Callable] = None,
        log: bool = True
    ):
        self.error_message = error_message
        self.cleanup = cleanup
        self.log = log
        self.error: Optional[Exception] = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.error = exc_val
            if self.log:
                print(f"[Claude] {self.error_message}: {exc_val}")

            if self.cleanup:
                try:
                    self.cleanup()
                except Exception as e:
                    if self.log:
                        print(f"[Claude] Cleanup error: {e}")

            # Suppress the exception
            return True
        return False

    @property
    def has_error(self) -> bool:
        """Check if an error occurred."""
        return self.error is not None


def safe_call(func: Callable, *args, default: Any = None, **kwargs) -> Any:
    """Safely call a function and return default on error.

    Args:
        func: Function to call
        *args: Positional arguments
        default: Default value to return on error
        **kwargs: Keyword arguments

    Returns:
        Function result or default value
    """
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


# Common error messages
class ErrorMessages:
    """Common error messages."""
    FILE_NOT_FOUND = "File not found"
    PERMISSION_DENIED = "Permission denied"
    INVALID_JSON = "Invalid JSON"
    NO_ACTIVE_SESSION = "No active session"
    NO_WINDOW = "No active window"
    NO_VIEW = "No active view"
    SESSION_NOT_INITIALIZED = "Session not initialized"
    TOOL_EXECUTION_FAILED = "Tool execution failed"
    UNKNOWN_TOOL = "Unknown tool"
