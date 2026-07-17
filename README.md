# Assistant-MCP

An MCP (Model Context Protocol) server that provides OAuth 2.0-protected tools to agentic clients. Built with FastMCP and currently integrates with Salesforce (CRM) and Outreach (SEP).

Intended use: creating and modifying prospects, sequences, sequence tasks, and related fixtures directly from an agent, so software changes can be tested against sandbox-style Salesforce orgs and Outreach workspaces without brittle one-off scripting — with a single, consolidated authentication experience.

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| FastMCP Server | `src/main.py` | Entry point, tool definitions, OAuth routes |
| OAuth Gate | `src/auth/oauth_gate.py` | Token validation and OAuth flow initiation |
| OAuthToolApp | `src/mcp_tools/auth_tool_app.py` | Base class for OAuth-protected tools |
| Salesforce Provider | `src/auth/providers/salesforce_provider.py` | Salesforce OAuth 2.0 web server flow |
| Outreach Provider | `src/auth/providers/outreach_provider.py` | Outreach OAuth 2.0 authorization code flow |
| Bearer Tokens | `src/auth/tokens/` | File-backed tokens with expiry + refresh logic |
| Salesforce Tools | `src/mcp_tools/salesforce/salesforce.py` | Salesforce REST API wrapper (SOQL, sObject CRUD) |
| Outreach Tools | `src/mcp_tools/outreach/outreach.py` | Outreach JSON:API wrapper (prospects, sequences, tasks) |
| Decorators | `src/utils/decorators.py` | Scopes, retry, OAuth error handling |

## Object Mapping

| Provider | Object | Nooks equivalent |
|----------|--------|------------------|
| Salesforce | `Account` | Account |
| Salesforce | `Contact` | Account-linked prospect (via `Contact.AccountId`) |
| Salesforce | `Lead` | Standalone prospect (uses `Lead.Company`, no AccountId) |
| Outreach | `account` | Account |
| Outreach | `prospect` | Prospect (linked to account via JSON:API relationship) |

## OAuth Flow Steps

| Step | Description |
|------|-------------|
| 1 | Client calls MCP tool (e.g., `salesforce_query`) |
| 2 | `@mcp_oauth_handler` decorator wraps tool function |
| 3 | `OAuthToolApp.run_method` passes to `ensure_auth` |
| 4 | `ensure_auth` checks for valid token via provider (refreshing stale tokens) |
| 5 | If no token, raises `OAuthRequiredError` with `elicitation_id` |
| 6 | Decorator converts to `UrlElicitationRequiredError` with auth URL |
| 7 | Client redirects user to `/auth/connect/{elicitation_id}` |
| 8 | Server redirects to the provider's OAuth consent screen |
| 9 | After consent, provider redirects to `/auth/callback?state={elicitation_id}&code=...` |
| 10 | Server exchanges code for token and stores it |
| 11 | Client retries original tool call with valid token |

## Project Structure

```
assistant-mcp/
├── .env                       # Environment configuration (see .env.example)
├── .python-version            # Python 3.13
├── pyproject.toml             # Dependencies and metadata
├── uv.lock                    # Dependency lock file
├── README.md
├── test_client.py             # Rough MCP client for manual testing
│
└── src/
    ├── main.py                # FastMCP server entry point, tool definitions
    │
    ├── auth/
    │   ├── oauth_gate.py      # OAuth flow management, token elicitation
    │   ├── providers/
    │   │   ├── provider.py               # Abstract OAuthProvider interface
    │   │   ├── salesforce_provider.py    # Salesforce OAuth implementation
    │   │   ├── outreach_provider.py      # Outreach OAuth implementation
    │   │   └── provider_registry.py      # Provider lookup by name
    │   └── tokens/
    │       ├── auth_token.py        # Abstract token interface
    │       ├── bearer_token.py      # Shared file-backed bearer token base
    │       ├── salesforce_token.py  # Salesforce token (TTL-based refresh)
    │       └── outreach_token.py    # Outreach token (rotating refresh tokens)
    │
    ├── mcp_tools/
    │   ├── auth_tool_app.py   # Base class for OAuth-protected tools
    │   ├── salesforce/
    │   │   └── salesforce.py  # Salesforce tool implementations
    │   └── outreach/
    │       └── outreach.py    # Outreach tool implementations
    │
    ├── utils/
    │   ├── decorators.py      # @tool_scope_factory, @tool_retry_factory, @mcp_oauth_handler
    │   └── errors.py          # Custom exceptions (OAuthRequiredError, RetryableApiError, ...)
    │
    └── db/
        └── db.py              # Database utilities (placeholder)
```

## Local Setup

Everything below is one-time setup. At the end you'll have a locally running MCP server
that authenticates to your own Salesforce org and Outreach workspace, with tokens that
refresh themselves.

### 1. Install dependencies

```bash
git clone <repo-url>
cd assistant-mcp
uv sync
```

### 2. Set up an ngrok tunnel (https origin for OAuth redirects)

OAuth providers redirect the browser back to this server after consent. Outreach requires
that redirect URI to be **https**, so the server needs a public https origin that forwards
to your local port. ngrok's free tier covers this:

1. Sign up at [dashboard.ngrok.com](https://dashboard.ngrok.com) and install the agent:
   ```bash
   brew install ngrok
   ngrok config add-authtoken <token from dashboard>
   ```
2. Claim your **free static domain** (Dashboard → Domains) — one per account, e.g.
   `your-name-abc123.ngrok-free.dev`. Without it the tunnel URL changes every restart and
   you'd have to re-edit both provider apps each time.
3. Run the tunnel, pointed at the server port from `.env`:
   ```bash
   ngrok http --url=<your-domain>.ngrok-free.dev 3999
   ```

Notes:
- The free tier shows a browser interstitial on first visit — click **Visit Site** during
  the OAuth flow and everything proceeds normally.
- The tunnel is only needed **during auth flows** (`/auth/connect` and `/auth/callback` are
  the only inbound routes). Tool calls and token refreshes are outbound, so you can stop
  ngrok once tokens are stored.
- While the tunnel is up, `/mcp` is publicly reachable and uses your stored tokens — keep
  tunnel sessions short, or gate it with `ngrok http --basic-auth "user:pass" ...`.

### 3. Create your Salesforce app

In your org: **Setup → App Manager → New Connected App** (or an External Client App — both
work; External Client Apps enforce PKCE, which this server implements):

1. Enable OAuth settings.
2. **Callback URL**: `https://<your-domain>.ngrok-free.dev/auth/callback`. Optionally also
   add `http://localhost:3999/auth/callback` — Salesforce allows http for `localhost` only
   (not `127.0.0.1`), and it lets you auth Salesforce without the tunnel running.
3. **Selected OAuth Scopes** — exactly two:
   - Manage user data via APIs (`api`)
   - Perform requests at any time (`refresh_token`, `offline_access`)
4. Under policies, leave the refresh token policy at "valid until revoked".
5. Copy the **Consumer Key** → `SALESFORCE_CLIENT_ID` and **Consumer Secret** →
   `SALESFORCE_CLIENT_SECRET` (Manage Consumer Details; requires identity verification).

App config changes can take a few minutes to propagate — an immediate `invalid_client_id`
usually just means "wait and retry".

### 4. Create your Outreach app

At [developers.outreach.io](https://developers.outreach.io) (needs Outreach admin access —
if you can't create apps, ask an admin):

1. Create a new app and add the **Outreach API (OAuth)** feature. The app can stay
   private/unlisted; it works for your own org without marketplace publishing.
2. **Redirect URI**: `https://<your-domain>.ngrok-free.dev/auth/callback` (https mandatory).
3. Enable exactly the scopes the server requests (see `SCOPES` in
   `src/mcp_tools/outreach/outreach.py`): `accounts.all`, `prospects.all`, `sequences.all`,
   `sequenceSteps.all`, `sequenceStates.all`, `tasks.all`, `mailboxes.read`, `users.read`.
   A missing scope fails the authorize request before any consent screen appears.
4. Copy the application ID → `OUTREACH_CLIENT_ID` and secret → `OUTREACH_CLIENT_SECRET`.

### 5. Fill out .env

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `SERVER_HOST` | Server host address (default: 127.0.0.1) |
| `SERVER_PORT` | Server port number — must match the port ngrok forwards to |
| `SERVER_ORIGIN_PROXY` | Your tunnel origin, e.g. `https://<your-domain>.ngrok-free.dev` (no trailing slash) |
| `SALESFORCE_CLIENT_ID` | Connected app consumer key |
| `SALESFORCE_CLIENT_SECRET` | Connected app consumer secret |
| `SALESFORCE_LOGIN_HOST` | `https://login.salesforce.com` (prod), `https://test.salesforce.com` (sandbox), or a My Domain URL |
| `SALESFORCE_API_VERSION` | REST API version (default: v56.0, matching the Nooks wrapper) |
| `SALESFORCE_LOCAL_TOKEN_PATH` | Token storage path (default: ./secrets/salesforce_token.json) |
| `SALESFORCE_TOKEN_TTL_SECONDS` | Proactive refresh window; Salesforce doesn't report token lifetimes (default: 1800) |
| `OUTREACH_CLIENT_ID` | Outreach OAuth app client ID |
| `OUTREACH_CLIENT_SECRET` | Outreach OAuth app client secret (quote it if it contains shell-special characters) |
| `OUTREACH_OAUTH_BASE_URL` | OAuth endpoint base (default: https://api.outreach.io/oauth) |
| `OUTREACH_API_BASE_URL` | API base (default: https://api.outreach.io/api/v2) |
| `OUTREACH_LOCAL_TOKEN_PATH` | Token storage path (default: ./secrets/outreach_token.json) |

The origin in `SERVER_ORIGIN_PROXY` must match the registered redirect URIs **exactly**,
character for character — `localhost` vs `127.0.0.1` counts as a mismatch
(`redirect_uri_mismatch` / "redirect_uri must match configuration").

### 6. Run and authenticate

```bash
uv run python src/main.py
```

The server runs at `http://{SERVER_HOST}:{SERVER_PORT}/mcp` (streamable HTTP transport).
Point an MCP client at it, e.g.:

```bash
claude mcp add --transport http sf-outreach http://localhost:3999/mcp
```

Then, with the ngrok tunnel running, call any provider tool (or run
`uv run python test_client.py`). The first call returns an authorization URL — open it,
click through the ngrok interstitial, log in, and consent. When you see "You may close
this tab", the token is stored under `./secrets/` and that provider works from then on:
tokens refresh proactively, and Outreach's rotating refresh tokens are persisted on every
refresh. Repeat once per provider.

Troubleshooting:
- **`.env` edits seem ignored**: `load_dotenv()` never overrides variables already exported
  in your shell. Check `env | grep -E 'SERVER_|SALESFORCE_|OUTREACH_'` and unset stale
  values (or launch with the changed var set explicitly).
- **Force a re-auth / switch orgs or users**: delete the provider's token file under
  `./secrets/` — the next tool call starts a fresh consent flow.
- **`missing required code challenge`**: your Salesforce app enforces PKCE; this server
  sends it (S256) — make sure you're running the current code.

## MCP Tools

### Salesforce Tools

| Tool | Parameters | Purpose |
|------|------------|---------|
| `salesforce_query` | `soql`, `max_pages` | Run SOQL with pagination |
| `salesforce_get_record` | `sobject`, `record_id`, `fields?` | Fetch one record |
| `salesforce_create_account` | `name`, `website?`, `extra_fields?` | Create Account |
| `salesforce_create_contact` | `last_name`, `first_name?`, `email?`, `account_id?`, `title?`, `phone?`, `extra_fields?` | Create account-linked prospect |
| `salesforce_create_lead` | `last_name`, `company`, `first_name?`, `email?`, `title?`, `phone?`, `extra_fields?` | Create standalone prospect |
| `salesforce_update_record` | `sobject`, `record_id`, `fields` | Modify any record's fields |

### Outreach Tools

| Tool | Parameters | Purpose |
|------|------------|---------|
| `outreach_find_prospects` | `emails?`, `prospect_ids?`, `account_id?`, `limit` | Look up prospects |
| `outreach_get_prospect` | `prospect_id` | Fetch one prospect |
| `outreach_create_prospect` | `emails`, `first_name?`, `last_name?`, `title?`, `company?`, `tags?`, `account_id?`, `owner_id?`, `extra_attributes?` | Create prospect |
| `outreach_update_prospect` | `prospect_id`, `attributes?`, `account_id?`, `owner_id?` | Modify prospect data |
| `outreach_create_account` | `name`, `domain?`, `website_url?`, `extra_attributes?` | Create account |
| `outreach_list_sequences` | `name?`, `limit` | Find sequence IDs |
| `outreach_create_sequence` | `name`, `description?`, `share_type` | Create sequence |
| `outreach_create_sequence_step` | `sequence_id`, `step_type`, `order?`, `interval_minutes?`, `task_note?` | Add step (incl. task steps) |
| `outreach_add_prospect_to_sequence` | `sequence_id`, `prospect_id`, `mailbox_id?` | Enroll prospect (sequenceState) |
| `outreach_create_task` | `prospect_id`, `action`, `note?`, `due_at?`, `owner_id?` | Create one-off task |
| `outreach_list_mailboxes` | `limit` | Mailbox IDs for enrollment with email steps |
| `outreach_list_users` | `email?`, `limit` | User IDs for owner assignment |

### Typical Fixture Flows

Salesforce (mirrors `~/scripts/salesforce/setup-account-prospects.ts`):

1. `salesforce_create_account` → account ID
2. `salesforce_create_contact` with `account_id` (repeat per prospect)
3. `salesforce_create_lead` for standalone prospects
4. `salesforce_query` to read back / verify

Outreach sequence testing:

1. `outreach_create_account` → account ID
2. `outreach_create_prospect` with `account_id`
3. `outreach_create_sequence` → sequence ID
4. `outreach_create_sequence_step` (e.g. `step_type='task'` with `task_note`)
5. `outreach_add_prospect_to_sequence` (pass `mailbox_id` if the sequence has email steps)
6. `outreach_create_task` for one-off tasks

## Key Design Patterns

### Factory Pattern (Decorators)

Decorators create specialized behavior for tool methods:

```python
@tool_scope_factory(scopes=["api", "refresh_token"])
@tool_retry_factory(error_message="Salesforce error (soql_query)", retry_on=(RetryableApiError,))
def soql_query(self, *, token: SalesforceToken, ctx: Dict[str, Any], soql: str, max_pages: int = 10):
    ...
```

Only `RetryableApiError` (network failures, 429s, 5xxs) is retried; other API errors
surface immediately with response text capped at 2,000 characters.

### Strategy Pattern (OAuthProvider Interface)

Abstract provider interface allows multiple OAuth implementations:

```python
class OAuthProvider(ABC):
    @abstractmethod
    def get_access_token(self, principal_id: str, scopes: Sequence[str]) -> Optional[OAuthToken]:
        ...

    @abstractmethod
    def generate_auth_url(self, scopes: Sequence[str], elicitation_id: str, ...) -> dict:
        ...
```

### Decorator Composition

Tool functions compose multiple decorators:

```python
@mcp.tool()
@mcp_oauth_handler("Authorization is required to access your Salesforce org.")
def salesforce_query(ctx: Context, soql: str, max_pages: int = 10):
    return salesforce_tools.run_method('soql_query', ctx=ctx, soql=soql, max_pages=max_pages)
```

- `@mcp.tool()`: Registers function as MCP tool
- `@mcp_oauth_handler`: Handles OAuth errors, converts to URL elicitation

### Token Refresh Behavior

- Tokens are stored as JSON files under `./secrets/` and refreshed proactively when stale
  (5-minute buffer before expiry).
- Salesforce doesn't report access-token lifetimes, so a local TTL
  (`SALESFORCE_TOKEN_TTL_SECONDS`) drives proactive refresh; a 401 mid-session marks the
  token stale so the next call refreshes it.
- Outreach reports `expires_in`/`created_at` and rotates refresh tokens; each refresh
  persists the new refresh token immediately.

## Tool Implementation

To add a new OAuth-protected tool:

```python
# In src/main.py
@mcp.tool()
@mcp_oauth_handler("Authorization message")
def my_tool(ctx: Context, param: str):
    """Docstring becomes tool description"""
    return tool_app.run_method('method_name', ctx=ctx, param=param)
```

```python
# In tool app class
@tool_scope_factory(scopes=[...])
@tool_retry_factory(error_message=..., retry_on=(RetryableApiError,))
def method_name(self, *, token: OAuthToken, ctx: Dict, param: str):
    # Implementation using token.access_token
    ...
```

## Related

- **msg-agent**: Companion MCP client project that consumes these tools using LangGraph
- **test_client**: A rough test client is provided to test the MCP server
