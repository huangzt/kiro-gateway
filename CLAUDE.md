# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kiro Gateway is a FastAPI-based proxy server that provides OpenAI-compatible and Anthropic-compatible APIs for Kiro (Amazon Q Developer / AWS CodeWhisperer). It translates requests between different API formats and handles authentication, streaming, model resolution, and error handling.

**Core Philosophy**: Transparent proxy with minimal, purposeful modifications. We fix API-level issues while preserving user intent. Build systems that handle entire classes of issues, not one-off patches.

## Essential Commands

### Running the Server

```bash
# Default (host: 0.0.0.0, port: 8000)
python main.py

# Custom port
python main.py --port 9000

# Custom host and port
python main.py --host 127.0.0.1 --port 9000
```

### Testing

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/unit/test_auth_manager.py -v

# Run specific test
pytest tests/unit/test_auth_manager.py::TestKiroAuthManagerInitialization::test_initialization_stores_credentials -v

# Run only unit tests
pytest tests/unit/ -v

# Stop on first failure
pytest -x

# Run with coverage
pytest --cov=kiro --cov-report=html
```

### Docker

```bash
# Run with docker-compose (recommended)
docker-compose up -d

# View logs
docker-compose logs -f

# Stop container
docker-compose down

# Rebuild after code changes
docker-compose up -d --build
```

## Architecture

### Request Flow

1. **Entry Point** (`main.py`): FastAPI app initialization, route registration, middleware setup
2. **Routes** (`routes_openai.py`, `routes_anthropic.py`, `routes_admin.py`): API endpoint handlers
3. **Account Pool** (`account_pool.py`): Multi-account queue-based request management with quota checking
4. **Converters** (3-layer architecture):
   - `converters_core.py`: Shared logic, unified message format (`UnifiedMessage`, `UnifiedTool`)
   - `converters_openai.py`: OpenAI Chat API → Kiro format
   - `converters_anthropic.py`: Anthropic Messages API → Kiro format
5. **Authentication** (`auth.py`): Token lifecycle management, supports both Kiro Desktop Auth and AWS SSO OIDC
6. **HTTP Client** (`http_client.py`): Retry logic, error handling, VPN/proxy support
7. **Streaming** (3-layer architecture):
   - `streaming_core.py`: Shared streaming logic
   - `streaming_openai.py`: Kiro SSE → OpenAI streaming format
   - `streaming_anthropic.py`: Kiro SSE → Anthropic streaming format
8. **Parsers** (`parsers.py`): AWS SSE event stream parsing, JSON truncation diagnostics
9. **Admin System**:
   - `admin_config.py`: Hot-reload configuration via `gateway.yml`
   - `log_broadcaster.py`: Real-time log streaming via SSE
   - `quota_checker.py`: Proactive quota monitoring via Kiro Web Portal API

### Key Components

**AccountPool** (`account_pool.py`):
- Multi-account queue-based request management
- Each account serialized (concurrency=1) to avoid 429 rate limiting
- Total concurrency = number of accounts
- Proactive quota checking via Kiro Web Portal API
- Automatic cooldown on rate limit (429) responses
- Hot-add/remove accounts without restart
- Account switching: copy credentials to host cache directory

**KiroAuthManager** (`auth.py`):
- Manages access token lifecycle with automatic refresh
- Supports 4 authentication methods: .env variables, JSON credentials file, AWS SSO, kiro-cli SQLite database
- Thread-safe token refresh using asyncio.Lock
- Detects auth type automatically (Kiro Desktop vs AWS SSO OIDC)

**AdminConfig** (`admin_config.py`):
- Hot-reload configuration via `gateway.yml`
- Overrides .env settings without restart
- Supports pool, timeout, reasoning, logging, and account settings
- Persists disabled account list

**LogBroadcaster** (`log_broadcaster.py`):
- Real-time log streaming via SSE
- Ring-buffer history (default 200 entries, configurable)
- Multiple concurrent subscribers
- Integrates with loguru as a sink

**QuotaChecker** (`quota_checker.py`):
- Queries Kiro Web Portal API for account quota
- Returns usage, limit, plan, and next reset time
- Marks accounts as exhausted when quota reached
- Background refresh at configurable intervals

**ModelResolver** (`model_resolver.py`):
- Dynamic model resolution system
- Normalizes model names (e.g., `claude-sonnet-4-5` → `claude-sonnet-4.5`)
- Maps user-facing names to Kiro API model IDs
- Handles model aliases and fallbacks

**Converters** (`converters_*.py`):
- Three-layer architecture: core (shared) + API-specific adapters (OpenAI/Anthropic)
- `UnifiedMessage` format as canonical representation
- Tool processing: sanitization, long description handling, validation
- Message merging: combines adjacent same-role messages
- Image handling: converts to Kiro format
- Thinking tags injection (optional feature)

**Streaming** (`streaming_*.py`):
- Parses AWS SSE events from Kiro API
- Transforms to OpenAI or Anthropic streaming format
- Handles thinking blocks (extended reasoning)
- Truncation recovery system (synthetic message generation)

**HTTP Client** (`http_client.py`):
- Automatic retry on transient errors (403, 429, 5xx)
- Exponential backoff with jitter
- VPN/proxy support (HTTP/SOCKS5)
- Request/response logging for debugging

### Configuration System

All configuration is centralized in `config.py`:
- Environment variable loading with `.env` support
- Type-safe access to settings
- Constants for API URLs, timeouts, model mappings
- Special handling for Windows paths (avoids escape sequence issues)

**Hot-reload via gateway.yml**:
- `gateway.yml` overrides `.env` settings without restart
- Supports pool settings (cooldown, queue timeout, quota check interval)
- Timeout configuration (first token, streaming read timeout)
- Reasoning settings (fake reasoning toggle, max tokens, handling mode)
- Logging settings (level, debug mode, history size)
- Account settings (multi_creds_dir, disabled accounts list)
- Changes applied immediately via Admin API PATCH /admin/config

### Testing Philosophy

**100% network isolation** - All tests use mocks, no real API calls. Global fixture `block_all_network_calls` in `conftest.py` enforces this.

**Paranoid testing** - Every commit must include comprehensive tests covering:
- Happy path
- Edge cases
- Error scenarios
- Boundary conditions
- Malformed inputs

**Test structure**: Arrange-Act-Assert pattern with descriptive class names (`Test*Success`, `Test*Errors`, `Test*EdgeCases`)

## Development Guidelines

### Code Quality Standards

1. **Type hints are mandatory** - Every function parameter and return value must be typed
2. **Comprehensive docstrings** - Google style with Args/Returns/Raises sections
3. **Logging at key decision points** - Use loguru (INFO for business logic, DEBUG for technical details, ERROR for failures)
4. **Specific exception handling** - Never use bare `except:` or `except Exception:`, catch specific exceptions and add context
5. **Proactive tech debt cleanup** - Extract hardcoded values and duplicated code immediately (constants, functions, modules)
6. **No placeholders** - Every function must be complete and production-ready when committed

### Error Handling

**User-friendly error messages** - Errors must be actionable, not technical jargon. When something fails, explain what went wrong and how to fix it.

**"Improperly formed request" errors** - Kiro API's most common error is notoriously vague due to poor Amazon documentation. It can indicate:
- Message structure problems (wrong role order, missing fields)
- Tool definition issues (invalid schemas, name length violations)
- Content format problems (malformed JSON, unsupported types)
- Authentication or permission issues
- Undocumented API constraints

When debugging this error, systematic testing is required to identify the actual cause.

### Systems Over Patches

When solving a problem, build systems that handle entire classes of issues, not one-off fixes. Even if a quick if-else would work, invest time in creating proper abstractions and dedicated modules. Solutions should be easily extensible without modifying core logic.

### Transparency First

The gateway preserves the user's original intent and request structure. Modifications are made only when necessary to work around Kiro API limitations or to add opt-in enhancements. We fix API quirks, not user decisions.

**Clear boundaries**:
- ✅ We fix: API validation quirks, format incompatibilities, authentication flows
- ✅ We add (optionally): Enhanced features that Kiro API doesn't provide natively
- ❌ We don't modify: User's conversation content, context decisions, message priorities
- ❌ We don't decide: What messages to keep, what to trim, what's "important"

## Important Files

- **AGENTS.md**: Comprehensive guide for AI agents with detailed project philosophy, architecture, and development guidelines
- **README.md**: User-facing documentation with setup instructions and API reference
- **CONTRIBUTING.md**: Contribution guidelines and standards
- **tests/README.md**: Testing philosophy, structure, and commands
- **.env.example**: Template for environment configuration
- **gateway.yml**: Hot-reload configuration file (overrides .env without restart)
- **kiro/static/**: Admin dashboard UI files (admin.html, admin.css, admin.js)

## Authentication Methods

The gateway supports 5 authentication methods (auto-detected):

1. **Multi-account directory** (highest priority): `KIRO_MULTI_CREDS_DIR` pointing to a directory with account subdirectories
2. **JSON credentials file**: `KIRO_CREDS_FILE` pointing to Kiro IDE credentials
3. **kiro-cli SQLite database**: `KIRO_CLI_DB_FILE` pointing to `~/.local/share/kiro-cli/data.sqlite3`
4. **AWS SSO**: Automatic detection via `clientId`/`clientSecret` in credentials file
5. **Environment variables** (`.env` file): `REFRESH_TOKEN`, `PROFILE_ARN`, `KIRO_REGION`

Authentication type is detected automatically in `KiroAuthManager` based on available credentials.

### Multi-Account Pool

When `KIRO_MULTI_CREDS_DIR` is configured, the gateway operates in multi-account mode:

**Directory structure**:
```
kiro-accounts/
  account-1/
    kiro-auth-token.json          (required)
    {clientIdHash}.json           (optional, for Enterprise SSO)
  account-2/
    kiro-auth-token.json
    {clientIdHash}.json
```

**Features**:
- Queue-based fair scheduling across accounts
- Each account handles one request at a time (prevents 429 rate limiting)
- Total concurrency = number of accounts
- Proactive quota checking (configurable interval, default 5 minutes)
- Automatic cooldown on 429 responses
- Hot-add/remove accounts via Admin API
- Disable/enable accounts without restart
- Account switching: copy credentials to host cache directory (requires `KIRO_HOST_CACHE_DIR` volume mapping)

## VPN/Proxy Support

For users in restricted networks (China, corporate proxies), the gateway supports routing all Kiro API requests through a proxy:

```env
VPN_PROXY_URL=http://127.0.0.1:7890
VPN_PROXY_URL=socks5://127.0.0.1:1080
VPN_PROXY_URL=http://user:password@proxy.company.com:8080
```

Supports HTTP, HTTPS, and SOCKS5 protocols with optional authentication.

**Implementation**: Proxy is configured globally via environment variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`) before any httpx clients are created. Localhost is automatically excluded from proxy routing.

## Host Account Switching

The gateway supports switching the active Kiro account on the host machine (where Kiro IDE/CLI runs):

**Setup**:
1. Configure `KIRO_HOST_CACHE_DIR` in `.env` (e.g., `~/.aws/sso/cache`)
2. Mount this directory as a Docker volume: `~/.aws/sso/cache:/app/host_aws_cache:rw`
3. Use Admin UI "Switch" button or `POST /admin/accounts/{name}/switch` endpoint

**How it works**: Copies account credentials from the pool to the host cache directory, allowing Kiro IDE/CLI to use that account immediately without manual file copying.

**Use case**: Quickly test different accounts in your local IDE without manually swapping credential files.

## Debug Logging

Debug logging is disabled by default. Enable in `.env`:

```env
DEBUG_MODE=errors  # Save logs only for failed requests (recommended)
DEBUG_MODE=all     # Save logs for every request
```

Debug files are saved to `debug_logs/` directory with request/response details.

## Admin Dashboard

The gateway includes a modern web-based admin dashboard at `/admin`:

**Features**:
- Real-time account pool status monitoring
- Per-account quota display (used/limit, plan, next reset)
- Active/busy/exhausted/disabled account indicators
- Account management: add, remove, disable, enable accounts
- Bulk quota refresh for all accounts
- Manual cooldown clearing
- Account switching (copy credentials to host)
- Download account credential files
- Hot-reload configuration editor (gateway.yml)
- Real-time log streaming via SSE
- Statistics cards with hover effects

**Security**: All admin endpoints require `X-Admin-Key` header matching `PROXY_API_KEY`.

**Admin API Endpoints**:
- `GET /admin/status` - Pool status with quota info
- `POST /admin/accounts` - Add new account (upload credentials)
- `POST /admin/accounts/{name}/quota-refresh` - Refresh single account quota
- `POST /admin/accounts/quota-refresh-all` - Bulk refresh all quotas
- `POST /admin/accounts/{name}/cooldown-clear` - Clear cooldown
- `POST /admin/accounts/{name}/disable` - Disable account
- `POST /admin/accounts/{name}/enable` - Enable account
- `POST /admin/accounts/{name}/switch` - Switch host to this account
- `DELETE /admin/accounts/{name}` - Delete account
- `GET /admin/accounts/{name}/files` - List credential files
- `GET /admin/accounts/{name}/files/{filename}` - Download credential file
- `GET /admin/config` - Get current configuration
- `PATCH /admin/config` - Update configuration (hot-reload)
- `GET /admin/logs/stream` - Real-time log stream (SSE)
