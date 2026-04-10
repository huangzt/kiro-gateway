# -*- coding: utf-8 -*-

"""
Tests for LogBroadcaster admin log filtering functionality.

Ensures that admin route logs are filtered from SSE streams while
still being stored in the full history buffer.
"""

import asyncio
import pytest
from datetime import datetime
from kiro.log_broadcaster import LogBroadcaster


class MockRecord:
    """Mock loguru record for testing."""

    def __init__(self, level_name: str, message: str, name: str, function: str, line: int):
        self.level = type('Level', (), {'name': level_name})()
        self.message = message
        self.name = name
        self.function = function
        self.line = line
        self.time = datetime.now()

    def __getitem__(self, key):
        """Support dict-style access for loguru compatibility."""
        return getattr(self, key)


class MockMessage:
    """Mock loguru message for testing."""

    def __init__(self, record: MockRecord):
        self.record = record


class TestLogBroadcasterAdminFiltering:
    """Test admin log filtering in LogBroadcaster."""

    def test_admin_logs_filtered_from_sse_stream(self):
        """Admin logs should be filtered from SSE stream but kept in history."""
        broadcaster = LogBroadcaster(history_size=10)

        # Create test messages
        admin_msg = MockMessage(MockRecord('INFO', 'GET /admin/status HTTP/1.1', 'uvicorn.access', 'log', 123))
        api_msg = MockMessage(MockRecord('INFO', 'GET /v1/chat/completions HTTP/1.1', 'uvicorn.access', 'log', 124))

        # Write messages to broadcaster
        broadcaster.write(admin_msg)
        broadcaster.write(api_msg)

        # Get full history (should contain both)
        full_history = broadcaster.get_history()
        assert len(full_history) == 2

        # Check messages are in full history
        admin_in_full = any('admin/status' in entry.get('message', '') for entry in full_history)
        api_in_full = any('chat/completions' in entry.get('message', '') for entry in full_history)
        assert admin_in_full, "Admin message should be in full history"
        assert api_in_full, "API message should be in full history"

    @pytest.mark.asyncio
    async def test_subscribe_filters_admin_from_history_snapshot(self):
        """Subscribe should return filtered history snapshot without admin logs."""
        broadcaster = LogBroadcaster(history_size=10)

        # Create test messages
        admin_msg = MockMessage(MockRecord('INFO', 'POST /admin/accounts HTTP/1.1', 'uvicorn.access', 'log', 123))
        api_msg = MockMessage(MockRecord('INFO', 'POST /v1/messages HTTP/1.1', 'uvicorn.access', 'log', 124))

        # Write messages
        broadcaster.write(admin_msg)
        broadcaster.write(api_msg)

        # Subscribe and get filtered history
        queue, filtered_history = await broadcaster.subscribe()

        try:
            # Should only contain API message, not admin message
            assert len(filtered_history) == 1
            assert 'v1/messages' in filtered_history[0]['message']
            assert not any('admin/accounts' in entry.get('message', '') for entry in filtered_history)
        finally:
            await broadcaster.unsubscribe(queue)

    def test_admin_route_module_logs_filtered(self):
        """Logs from kiro.routes_admin module should be filtered (except errors)."""
        broadcaster = LogBroadcaster(history_size=10)

        # Test INFO level admin module log (should be filtered)
        admin_info_msg = MockMessage(MockRecord('INFO', 'Account pool status requested', 'kiro.routes_admin', 'get_status', 123))
        broadcaster.write(admin_info_msg)

        # Test ERROR level admin module log (should NOT be filtered)
        admin_error_msg = MockMessage(MockRecord('ERROR', 'Authentication failed', 'kiro.routes_admin', 'verify_admin_key', 124))
        broadcaster.write(admin_error_msg)

        # Check filtering logic directly
        entry_info = {
            'message': 'Account pool status requested',
            'module': 'kiro.routes_admin',
            'function': 'get_status',
            'level': 'INFO'
        }
        entry_error = {
            'message': 'Authentication failed',
            'module': 'kiro.routes_admin',
            'function': 'verify_admin_key',
            'level': 'ERROR'
        }

        assert broadcaster._is_admin_log(entry_info) == True, "INFO admin logs should be filtered"
        assert broadcaster._is_admin_log(entry_error) == False, "ERROR admin logs should NOT be filtered"

    def test_admin_static_files_filtered(self):
        """Admin static file requests should be filtered."""
        broadcaster = LogBroadcaster(history_size=10)

        test_cases = [
            'GET /admin-static/admin.css HTTP/1.1',
            'GET /admin-static/admin.js HTTP/1.1',
            'Serving admin.html file',
        ]

        for message in test_cases:
            entry = {
                'message': message,
                'module': 'uvicorn.access',
                'function': 'log',
                'level': 'INFO'
            }
            assert broadcaster._is_admin_log(entry) == True, f"Should filter: {message}"

    def test_regular_api_logs_not_filtered(self):
        """Regular API logs should not be filtered."""
        broadcaster = LogBroadcaster(history_size=10)

        test_cases = [
            'GET /v1/models HTTP/1.1',
            'POST /v1/chat/completions HTTP/1.1',
            'POST /v1/messages HTTP/1.1',
            'Request processed successfully',
        ]

        for message in test_cases:
            entry = {
                'message': message,
                'module': 'kiro.routes_openai',
                'function': 'chat_completions',
                'level': 'INFO'
            }
            assert broadcaster._is_admin_log(entry) == False, f"Should NOT filter: {message}"

    def test_uvicorn_format_admin_logs_filtered(self):
        """Uvicorn access log format admin requests should be filtered."""
        broadcaster = LogBroadcaster(history_size=10)

        test_cases = [
            '184.73.228.247:33348 - "GET /admin HTTP/1.1" 200',
            '127.0.0.1:12345 - "GET /admin/status HTTP/1.1" 200',
            '192.168.1.1:54321 - "POST /admin/accounts HTTP/1.1" 201',
            '10.0.0.1:8080 - "PATCH /admin/config HTTP/1.1" 200',
            '172.16.0.1:9000 - "DELETE /admin/accounts/account-1 HTTP/1.1" 200',
        ]

        for message in test_cases:
            entry = {
                'message': message,
                'module': 'uvicorn.access',
                'function': 'log',
                'level': 'INFO'
            }
            assert broadcaster._is_admin_log(entry) == True, f"Should filter: {message}"

    def test_uvicorn_format_api_logs_not_filtered(self):
        """Uvicorn access log format API requests should not be filtered."""
        broadcaster = LogBroadcaster(history_size=10)

        test_cases = [
            '127.0.0.1:9999 - "GET /v1/models HTTP/1.1" 200',
            '192.168.1.100:5000 - "POST /v1/chat/completions HTTP/1.1" 200',
            '10.0.0.5:8888 - "POST /v1/messages HTTP/1.1" 200',
        ]

        for message in test_cases:
            entry = {
                'message': message,
                'module': 'uvicorn.access',
                'function': 'log',
                'level': 'INFO'
            }
            assert broadcaster._is_admin_log(entry) == False, f"Should NOT filter: {message}"

    def test_admin_auth_failures_not_filtered(self):
        """Admin authentication failures should not be filtered (security relevant)."""
        broadcaster = LogBroadcaster(history_size=10)

        entry = {
            'message': 'Invalid admin key provided',
            'module': 'kiro.routes_admin',
            'function': 'verify_admin_key',
            'level': 'WARNING'
        }

        assert broadcaster._is_admin_log(entry) == False, "Auth failures should NOT be filtered"