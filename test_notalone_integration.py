#!/usr/bin/env python3
"""Integration test for sublime-claude bridge with notalone notification system."""

import sys
import asyncio

# Add paths
sys.path.insert(0, 'bridge')
sys.path.insert(0, '.')


async def test_notalone_imports():
    """Test that notalone modules can be imported."""
    try:
        from notalone.hub import NotificationHub
        from notalone.backends.sublime import SublimeNotificationBackend
        from notalone.types import NotificationType, NotificationParams, Notification
        print("✓ Notalone modules imported successfully")
        return True
    except ImportError as e:
        print(f"✗ Failed to import notalone modules: {e}")
        return False


async def test_sublime_backend_initializes():
    """Test that SublimeNotificationBackend can be initialized."""
    from notalone.hub import NotificationHub
    from notalone.backends.sublime import SublimeNotificationBackend

    # Create a mock send_notification function
    def mock_send_notification(method: str, params: dict):
        """Mock send_notification - signature: (method, params)"""
        pass

    backend = SublimeNotificationBackend(
        send_notification=mock_send_notification,
        session_id="test-session"
    )
    hub = NotificationHub(backend)
    await hub.start()

    print("✓ SublimeNotificationBackend initialized")

    await hub.stop()
    return True


async def test_notification_types():
    """Test that all notification types are available."""
    from notalone.types import NotificationType

    expected_types = {
        NotificationType.TIMER,
        NotificationType.SUBSESSION_COMPLETE,
        NotificationType.AGENT_COMPLETE,
        NotificationType.TICKET_UPDATE,
        NotificationType.BROADCAST,
        NotificationType.CHANNEL,
    }

    print(f"✓ All {len(expected_types)} notification types available")
    return True


async def test_timer_notification():
    """Test setting a timer notification."""
    from notalone.hub import NotificationHub
    from notalone.backends.sublime import SublimeNotificationBackend
    from notalone.types import NotificationType

    notifications_sent = []

    def mock_send_notification(method: str, params: dict):
        """Mock send_notification - signature: (method, params)"""
        notifications_sent.append({'method': method, 'params': params})

    backend = SublimeNotificationBackend(
        send_notification=mock_send_notification,
        session_id="test-session"
    )
    hub = NotificationHub(backend)
    await hub.start()

    # Set a very short timer
    result = await hub.set_timer(
        seconds=0.1,
        wake_prompt="Test timer fired"
    )

    # Wait for timer to fire
    await asyncio.sleep(0.2)

    await hub.stop()

    if len(notifications_sent) > 0:
        sent = notifications_sent[0]
        if sent['method'] == 'alarm_wake' and sent['params'].get('event_type') == 'timer':
            print("✓ Timer notification fired correctly")
            return True

    print(f"✗ Timer notification did not fire (sent: {len(notifications_sent)})")
    return False


async def test_session_complete_signal():
    """Test signaling session complete."""
    from notalone.hub import NotificationHub
    from notalone.backends.sublime import SublimeNotificationBackend

    notifications_sent = []

    def mock_send_notification(method: str, params: dict):
        """Mock send_notification - signature: (method, params)"""
        notifications_sent.append({'method': method, 'params': params})

    backend = SublimeNotificationBackend(
        send_notification=mock_send_notification,
        session_id="test-session"
    )
    hub = NotificationHub(backend)
    await hub.start()

    # Wait for a subsession
    await hub.wait_for_session(
        subsession_id="test-subsession",
        wake_prompt="Subsession completed"
    )

    # Give the monitor task a moment to start and register the event
    await asyncio.sleep(0.05)

    # Signal completion (this will trigger the notification asynchronously)
    count = await backend.signal_session_complete("test-subsession")

    # Give it a moment to fire
    await asyncio.sleep(0.1)

    await hub.stop()

    if count == 1 and len(notifications_sent) > 0:
        print("✓ Session complete signal triggered notification")
        return True

    print(f"✗ Session complete signal failed (triggered: {count}, sent: {len(notifications_sent)})")
    return False


async def run_all_tests():
    """Run all integration tests."""
    print("\n=== Sublime-Claude Notalone Integration Tests ===\n")

    tests = [
        ("Notalone Imports", test_notalone_imports),
        ("Sublime Backend Initializes", test_sublime_backend_initializes),
        ("Notification Types", test_notification_types),
        ("Timer Notification", test_timer_notification),
        ("Session Complete Signal", test_session_complete_signal),
    ]

    results = []
    for name, test_fn in tests:
        try:
            result = await test_fn()
            results.append((name, result))
        except Exception as e:
            print(f"✗ {name} failed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print("\n=== Test Results ===")
    passed = sum(1 for _, result in results if result)
    total = len(results)
    print(f"{passed}/{total} tests passed")

    if passed == total:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_all_tests())
    sys.exit(exit_code)
