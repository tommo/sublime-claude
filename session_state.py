"""Session state machine for managing session lifecycle."""
from enum import Enum
from typing import Optional, Set, Callable, Dict


class SessionState(Enum):
    """States a session can be in."""
    UNINITIALIZED = "uninitialized"  # Session created but not connected to bridge
    INITIALIZING = "initializing"     # Connecting to bridge
    READY = "ready"                   # Connected and idle
    WORKING = "working"               # Processing a query
    ERROR = "error"                   # Error state


class SessionStateMachine:
    """State machine for session lifecycle management."""

    # Define valid state transitions
    VALID_TRANSITIONS: Dict[SessionState, Set[SessionState]] = {
        SessionState.UNINITIALIZED: {
            SessionState.INITIALIZING,
            SessionState.ERROR,
        },
        SessionState.INITIALIZING: {
            SessionState.READY,
            SessionState.ERROR,
        },
        SessionState.READY: {
            SessionState.WORKING,
            SessionState.ERROR,
        },
        SessionState.WORKING: {
            SessionState.READY,
            SessionState.WORKING,  # Allow re-entering for streaming
            SessionState.ERROR,
        },
        SessionState.ERROR: {
            SessionState.INITIALIZING,  # Can retry initialization
            SessionState.UNINITIALIZED,  # Can reset
        },
    }

    def __init__(self, initial_state: SessionState = SessionState.UNINITIALIZED):
        self._state = initial_state
        self._on_change_callbacks: list[Callable[[SessionState, SessionState], None]] = []

    @property
    def state(self) -> SessionState:
        """Get current state."""
        return self._state

    @property
    def is_initialized(self) -> bool:
        """Check if session is initialized (ready or working)."""
        return self._state in (SessionState.READY, SessionState.WORKING)

    @property
    def is_working(self) -> bool:
        """Check if session is currently working."""
        return self._state == SessionState.WORKING

    @property
    def is_ready(self) -> bool:
        """Check if session is ready for new queries."""
        return self._state == SessionState.READY

    @property
    def has_error(self) -> bool:
        """Check if session is in error state."""
        return self._state == SessionState.ERROR

    def can_transition(self, new_state: SessionState) -> bool:
        """Check if transition to new state is valid.

        Args:
            new_state: State to transition to

        Returns:
            True if transition is allowed
        """
        if self._state == new_state:
            return True  # Same state is always valid
        return new_state in self.VALID_TRANSITIONS.get(self._state, set())

    def transition(self, new_state: SessionState, force: bool = False) -> bool:
        """Transition to a new state.

        Args:
            new_state: State to transition to
            force: If True, skip validation (use with caution)

        Returns:
            True if transition was successful

        Raises:
            ValueError: If transition is invalid and force=False
        """
        if not force and not self.can_transition(new_state):
            raise ValueError(
                f"Invalid state transition: {self._state.value} -> {new_state.value}"
            )

        old_state = self._state
        self._state = new_state

        # Notify callbacks
        for callback in self._on_change_callbacks:
            try:
                callback(old_state, new_state)
            except Exception as e:
                print(f"[Claude] State change callback error: {e}")

        return True

    def on_change(self, callback: Callable[[SessionState, SessionState], None]) -> None:
        """Register a callback for state changes.

        Args:
            callback: Function called with (old_state, new_state)
        """
        self._on_change_callbacks.append(callback)

    def reset(self) -> None:
        """Reset to uninitialized state."""
        self.transition(SessionState.UNINITIALIZED, force=True)

    # Convenience methods for common transitions

    def start_initialization(self) -> bool:
        """Start initialization process."""
        try:
            self.transition(SessionState.INITIALIZING)
            return True
        except ValueError:
            return False

    def finish_initialization(self, success: bool = True) -> bool:
        """Finish initialization process.

        Args:
            success: True if initialization succeeded, False for error

        Returns:
            True if transition was successful
        """
        try:
            if success:
                self.transition(SessionState.READY)
            else:
                self.transition(SessionState.ERROR)
            return True
        except ValueError:
            return False

    def start_working(self) -> bool:
        """Start working on a query."""
        try:
            self.transition(SessionState.WORKING)
            return True
        except ValueError:
            return False

    def finish_working(self, success: bool = True) -> bool:
        """Finish working on a query.

        Args:
            success: True if query succeeded, False for error

        Returns:
            True if transition was successful
        """
        try:
            if success:
                self.transition(SessionState.READY)
            else:
                self.transition(SessionState.ERROR)
            return True
        except ValueError:
            return False

    def set_error(self) -> bool:
        """Set error state."""
        try:
            self.transition(SessionState.ERROR)
            return True
        except ValueError:
            return False

    def __repr__(self) -> str:
        return f"SessionStateMachine(state={self._state.value})"

    def __str__(self) -> str:
        return self._state.value


# Helper function for creating state machines with logging
def create_session_state_machine(
    initial_state: SessionState = SessionState.UNINITIALIZED,
    log_transitions: bool = False
) -> SessionStateMachine:
    """Create a session state machine with optional logging.

    Args:
        initial_state: Initial state
        log_transitions: If True, log all state transitions

    Returns:
        SessionStateMachine instance
    """
    machine = SessionStateMachine(initial_state)

    if log_transitions:
        def log_transition(old_state: SessionState, new_state: SessionState):
            print(f"[Claude] State transition: {old_state.value} -> {new_state.value}")

        machine.on_change(log_transition)

    return machine
