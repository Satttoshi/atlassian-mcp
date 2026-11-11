# Jira & Confluence MCP Server (read-only, optional Jira writes)

A local [MCP](https://modelcontextprotocol.io) server that lets Claude Code pull Jira
ticket context (issue details, comment threads, the reference graph around a ticket,
and JQL search results) and Confluence content (pages, blog posts, footer and inline
comments, the page tree, and CQL search results), all rendered as compact Markdown.
Image attachments from either product (the screenshot on a UI bug ticket, a diagram
in a spec page) can be fetched for Claude to analyze visually, and text attachments
(logs, stack traces, JSON/HAR, CSV, source code, ...) come back as plain text.

## What it deliberately cannot do

By default this server is **strictly read-only** (Jira writes are an opt-in, see "Optional: Jira writes"; Confluence is always read-only). It exposes no tool that creates, updates,
transitions, deletes, or comments on anything. Enforcement is layered:

1. **In code:** every HTTP request (Jira and Confluence) funnels through a single
   helper that only permits `GET`, with one allowlisted exception:
   `POST /rest/api/3/search/jql`, a read operation that Atlassian requires be sent as
   POST. Confluence has no POST exception at all. Any other method raises
   `ReadOnlyViolationError`, so a future edit that adds a write call fails loudly.
2. **At the credential level:** create the API token with only read scopes (below),
   so even a bug could not write.

## Tools

### Jira

| Tool | Purpose |
|---|---|
| `get_issue(issue_key, include_comments=True)` | Full ticket detail including all non-empty custom fields (acceptance criteria, story points, ...) with their display names, plus (by default) the comment thread |
| `get_comments(issue_key, limit=100, newest_first=False)` | Just the discussion, with author/timestamp/edited/visibility |
| `get_issue_context(issue_key)` | Parent, subtasks, linked issues (with link direction), epic children, and remote links. Linked Confluence pages are shown with their page ID, ready for `get_confluence_page` |
| `search_issues(jql, limit=25)` | Compact JQL search results |
| `get_user_activity(from_date, to_date, user="me", include_confluence=False, max_issues=50)` | What a user did between two dates (`yyyy-MM-dd`, `to_date` exclusive): their comments (with mentions), status transitions, field edits and issue creations, grouped by issue and as one timeline, in the authenticated user's timezone. `user` is `"me"` or an accountId. Finds issues via `updatedBy(accountId, from, to)` JQL, then reads each issue's changelog and comments (5 in parallel). With `include_confluence`, also lists Confluence pages the user contributed to that were last modified in the range. Truncation and per-issue failures are reported in the output instead of failing the call |
| `get_attachment(attachment_id)` | Downloads an attachment (listed by `get_issue`). Images (png/jpeg/gif/webp) come back as vision input, so Claude can look at screenshots; text files (detected by mime type, or by extension when Jira reports `application/octet-stream`) come back as text. Files of unknown type and extension (e.g. `Dockerfile`, `app.log.1`) are downloaded and served as text if their content has no NUL bytes and is valid UTF-8 (or UTF-16 with BOM). Text over 20,000 characters is saved in full (as UTF-8) under `<tmp>/jira-mcp/attachments/`, and the tool returns that path plus the first and last 50 lines, so Claude can grep or page through the file instead of loading it all. Max 5 MB; videos, PDFs, archives and other binary files are rejected |

### Confluence

| Tool | Purpose |
|---|---|
| `get_confluence_page(page, include_comments=True)` | A page or blog post (by numeric ID or URL): space, version, author/owner, labels, URL, the full body (tables, task lists, macros; embedded Jira issues shown as `[Jira issue: KEY]`; embedded images labelled with their attachment ID), the attachment list, and (by default) all footer + inline comments with reply threads |
| `get_confluence_comments(page, limit=100, newest_first=False)` | Just the discussion: footer comments, then inline comments (quoting the highlighted text, marked if resolved), replies nested. Images pasted into comments are labelled with their attachment ID |
| `get_confluence_page_context(page)` | Where the page sits in the tree: ancestors (top-level first) and direct children, each with title + ID |
| `search_confluence(query, limit=25)` | Plain words run a full-text search over pages and blog posts; anything with CQL syntax (`space = ENG AND title ~ "auth"`) is sent as CQL |
| `get_confluence_attachment(attachment_id)` | Downloads an attachment of a page or comment (`att…` ID from `get_confluence_page`/`get_confluence_comments`). Same behavior as `get_attachment`: images as vision input, text files as text (long ones saved to a file with a preview), max 5 MB. Passing a page ID or URL by mistake returns that page's readable attachment IDs |

### Both

| Tool | Purpose |
|---|---|
| `whoami()` | Which account each product's token resolves to, per configured site (Jira and Confluence reported separately), and which environment variable each token came from; the first stop for auth debugging |

Every tool except `whoami` also takes an optional `site` argument; see
[Multiple sites](#multiple-sites).

## Optional: Jira writes

Off by default. To turn them on, set in `.env`:

- `JIRA_WRITE_PROJECTS`: comma-separated project keys the write tools may touch, e.g. `AC`. Required.
- `JIRA_WRITE_API_TOKEN`: optional. Without it the writes use the Jira token (`JIRA_API_TOKEN`, else `ATLASSIAN_API_TOKEN`), which then needs `write:jira-work` (an unscoped token has it). Set a separate token with `read:jira-work`, `write:jira-work` and `read:jira-user` to keep the read token read-only.

Then restart the MCP server (start a new Claude Code session). The startup log says "Jira writes enabled for projects: …".

| Tool | Purpose |
|---|---|
| `update_issue(issue_key, summary, description, assignee, labels, fields)` | Change only the passed fields. `fields` takes display names or IDs, e.g. `{"Acceptance Criteria": "...", "Epic Link": "AC-38"}`. `assignee="none"` unassigns |
| `create_issue(project, summary, description, issue_type, parent, assignee, fields)` | Create an issue, returns the key |
| `add_comment(issue_key, body)` / `edit_comment(issue_key, comment_id, body)` | Comment IDs show as `(comment 12345)` in `get_issue` |
| `transition_issue(issue_key, status)` | Move to a status; the error lists the possible ones |
| `link_issues(issue_key, relation, other_key)` | e.g. `("AC-66", "is blocked by", "AC-14")`; the link is read back to check its direction |
| `unlink_issues(link_id)` | Link IDs show as `(link 12345)` in `get_issue_context` |

Text is Jira wiki markup (REST v2), not Markdown.

With several sites configured, every write tool takes the same optional `site` argument as the read tools: issue keys and the project of `create_issue` are routed to the site that has the project, and `unlink_issues` needs `site` because a link ID does not identify a site. `JIRA_WRITE_PROJECTS` applies to every site.

Guard rails, all in `JiraWriteClient` (`client.py`):

1. Only the routes in `WRITE_ROUTES` are allowed. Nothing can be deleted except an issue link; issues, comments and attachments can't.
2. Every write is checked against `JIRA_WRITE_PROJECTS`, including the project in a create and both issues of a link.
3. The read client (`JiraClient`) is unchanged and still blocks every write. Confluence stays read-only.
4. Claude Code asks before each tool call unless you allow the tool in your permissions.

## Setup

### 1. Create Atlassian API tokens

Tokens are created at <https://id.atlassian.com/manage-profile/security/api-tokens>
and belong to your account, so the same tokens work for every site you configure.
Pick one of two setups:

| Setup | Variables | Read-only enforced by |
|---|---|---|
| **Scoped tokens** (recommended) | `JIRA_API_TOKEN` and/or `CONFLUENCE_API_TOKEN` | Atlassian and this server |
| **One unscoped token** (simplest) | `ATLASSIAN_API_TOKEN` | this server only |

A scoped token is bound to **one** app, which is why the scoped setup needs one
token per product. Setting only one of the two is fine: the other product's tools
then report that no token is configured.

**Scoped tokens:**

1. Choose **Create API token with scopes**.
2. Select the **Jira** app and pick **only** these scopes:
   - `read:jira-work`
   - `read:jira-user`
3. Copy the token immediately (it is shown only once) into `JIRA_API_TOKEN`.
4. For Confluence, repeat with the **Confluence** app and the scopes below, and put
   that token in `CONFLUENCE_API_TOKEN`.

**One unscoped token:** choose **Create API token** and put it in
`ATLASSIAN_API_TOKEN`. It works for both products, but it can do everything your
account can, so read-only then rests on this server's request guard alone.

Each product uses its own variable if set and falls back to `ATLASSIAN_API_TOKEN`
otherwise. The server handles both token kinds automatically (see below).

#### Confluence token scopes

Select only read scopes:

- `read:page:confluence`, `read:blogpost:confluence`: page and blog post bodies
- `read:comment:confluence`: footer and inline comments
- `read:attachment:confluence`: attachment lists and image downloads
- `read:space:confluence`: space names
- `read:label:confluence`: page labels
- `read:hierarchical-content:confluence`: ancestors and children
- `read:user:confluence`: author names and the auth probe
- `search:confluence`: CQL search (if search returns 403, also add
  `read:content-details:confluence`)

### 2. Configure `.env`

```bash
cp .env.example .env   # then edit
```

This is the whole configuration surface:

| Key | Value |
|---|---|
| `ATLASSIAN_EMAIL` | The email of your Atlassian account |
| `ATLASSIAN_SITE_URL` | e.g. `https://your-company.atlassian.net` (shared by Jira and Confluence). Several sites: separate them with commas; see [Multiple sites](#multiple-sites) |
| `ATLASSIAN_API_TOKEN` | One unscoped token for both products (setup 2 in step 1) |
| `JIRA_API_TOKEN` | A Jira-scoped token; wins over `ATLASSIAN_API_TOKEN` for Jira |
| `CONFLUENCE_API_TOKEN` | A Confluence-scoped token; wins over `ATLASSIAN_API_TOKEN` for Confluence |

The email, the site URL and at least one token are required.

`.env` is gitignored; never commit it. Real environment variables take precedence
over the file. The file is located relative to the project directory (not the working
directory), so the server finds it no matter where it is launched from.

### 3. Install dependencies

With [uv](https://docs.astral.sh/uv/) (preferred, since this repo has a `uv.lock`):

```bash
uv sync
```

Or with plain pip into a venv:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
```

### 4. Verify with `--check`

```bash
.venv/bin/python -m jira_mcp --check            # connectivity + auth only
.venv/bin/python -m jira_mcp --check PROJ-123   # also fetch a ticket in full
.venv/bin/python -m jira_mcp --check --page 123456   # also fetch a Confluence page
```

This prints whether `.env` was found, the configured sites, and for each site and
product: which base URL was selected (and whether the cloud-ID fallback was needed),
the authenticated account and the variable its token came from. When a key or page is
given, it also prints the content exactly as Claude would see it. `--page` accepts a
numeric page ID or a full page URL; with several sites, add `--site NAME` for a
numeric page ID.

## Multiple sites

If your account belongs to several Atlassian sites (orgs), list them all:

```
ATLASSIAN_SITE_URL=https://acme.atlassian.net,https://other-org.atlassian.net
```

Each site is named after its subdomain (`acme`, `other-org`; a custom domain keeps
its full hostname) and the first one is the default. The same email and tokens are
used for all of them. With more than one site configured:

- Every result starts with a `Site:` line, and every tool accepts `site="<name>"`.
- **Issue keys** are routed automatically: the server checks which site has the
  key's project (one request per site, cached per project). If the project exists
  on several sites, or on none, the tool returns an error asking for `site` instead
  of guessing.
- **URLs** (Confluence pages) are routed by their hostname.
- **Bare IDs** (attachment IDs, numeric page IDs) are only unique within a site, so
  they require `site`.
- **Searches** (`search_issues`, `search_confluence`) run on the default site unless
  `site` is given.
- **`get_user_activity`** reports every site, one section each, unless `site` is
  given.

`ATLASSIAN_CLOUD_ID` identifies one site, so it cannot be combined with several.

## Scoped vs. unscoped tokens: the base URL problem

- An **unscoped** token works against your site URL, `https://<site>.atlassian.net`.
- A **scoped** token against that same URL fails *silently*, returning
  anonymous-looking responses. It must call
  `https://api.atlassian.com/ex/jira/{cloudId}` (or `/ex/confluence/{cloudId}` for
  Confluence) instead.

You do not need to know which kind you hold. On first use the server probes the site
URL with `GET /rest/api/3/myself` (Confluence: `GET /wiki/rest/api/user/current`); if that does not return a real account, it fetches your
cloud ID from `{site}/_edge/tenant_info` and retries against `api.atlassian.com`. The
winner is cached for the process lifetime and logged to stderr. Jira and Confluence
are detected independently, so one can use an unscoped token and the other a scoped
one.

If detection ever fails: `_edge/tenant_info` is not part of Atlassian's formally
supported REST API (though Atlassian's own support docs point at it), so it could
change. In that case set `ATLASSIAN_CLOUD_ID` in `.env` to skip detection; the error
message will tell you when this applies. You almost never need it.

## PyCharm setup

1. **Interpreter:** Settings → Project → Python Interpreter → Add Interpreter →
   *Existing* → select `.venv/bin/python` in the project directory. (If you ran
   `uv sync`, the venv already exists with everything installed.)
2. **Run configuration for debugging:** Run → Edit Configurations → **+** → Python:
   - **Run:** module `jira_mcp` (choose "module" instead of "script path")
   - **Parameters:** `--check PROJ-123`
   - **Working directory:** the project root (anything works, but this is tidy)

   Now you can set breakpoints anywhere (e.g. in `client.py`) and debug real requests.
   Errors inside a running MCP server are otherwise invisible.

## Connect to Claude Code

Use the venv's Python **by absolute path**; a bare `python` won't resolve to the venv
when Claude Code spawns the server.

macOS/Linux:

```bash
claude mcp add jira -- /path/to/PythonProject/.venv/bin/python -m jira_mcp
```

Windows:

```bash
claude mcp add jira -- C:\path\to\PythonProject\.venv\Scripts\python.exe -m jira_mcp
```

Notes:

- Everything after `--` is the command Claude runs; everything before it is Claude's
  own options.
- Default scope is `local` (just you, just this project, stored in `~/.claude.json`).
  Add `--scope project` to share via a checked-in `.mcp.json`, or `--scope user` to use
  it across all your projects.

### Verify it's connected

Inside a Claude Code session:

- Run `/mcp`; the `jira` server should be listed as connected, with twelve tools.
- Or just ask: *"use whoami to check the jira connection"*.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| **401 Unauthorized** | Wrong email or token, or the token was revoked/expired. Recreate the token and update `.env`. Run `--check` to confirm. |
| **403 Forbidden** | Scoped token missing `read:jira-work` / `read:jira-user`, or your account lacks site access. Recreate the token with both read scopes. |
| **Confluence 401/403 but Jira works** | With scoped tokens, the Jira token cannot read Confluence. Create a Confluence-scoped token and set `CONFLUENCE_API_TOKEN`. `whoami` shows each product's status and token variable separately. |
| **"No API token configured for ..."** | That product has neither its own token variable nor `ATLASSIAN_API_TOKEN`. Set one of the two named in the error. |
| **"exists on several sites" / "is an ID, which does not identify a site"** | Several sites are configured and the input does not pin one down. Pass `site` (the name from `whoami` or from the `Site:` line of an earlier result). |
| **404 Not Found** | The issue or page doesn't exist, **or your account lacks permission to see it**. Jira reports issues you cannot view as 404, and a token never grants more access than the human it belongs to. Verify you can open the ticket in a browser while logged in as that account. |
| **Empty tool list in Claude** | The server crashed at startup. Run the exact command from `claude mcp add` yourself in a terminal; startup errors print to stderr. Usual causes: wrong Python path, or missing `.env` keys. |
| **"short link" error for a Confluence URL** | `/wiki/x/…` short links can't be resolved without a browser. Open the link and pass the full page URL (or the numeric ID from it). |
| **Server won't start** | Run `--check`. If it reports missing config, fix `.env`. If imports fail, re-run `uv sync` (or reinstall `requirements.txt`) and confirm the venv Python is ≥ 3.11. |
| **Detection failed / anonymous responses** | Startup logs (stderr) say which base URL was probed and why it was rejected. If `_edge/tenant_info` is unreachable, set `ATLASSIAN_CLOUD_ID` in `.env`. |

## Notes for developers new to Python

- **The venv** (`.venv/`) is a project-local copy of Python plus this project's
  packages: the equivalent of `node_modules`, except the interpreter itself lives
  inside it too. That's why Claude Code must be given `.venv/bin/python` by absolute
  path: there's no global install to fall back on.
- **`asyncio.run(...)`** is needed because async functions in Python don't run just by
  calling them; calling one returns a *coroutine object*, and something has to drive
  it. There's no ambient event loop like in Node; `asyncio.run()` creates a loop, runs
  one coroutine to completion, and tears the loop down. The MCP server does this
  internally via `mcp.run()`; the `--check` mode does it explicitly.
- **The decorators** (`@mcp.tool`) are functions that receive the function defined
  below them and register/wrap it, like a middleware factory applied at definition
  time. FastMCP's decorator reads the function's name, type hints, and docstring to
  generate the MCP tool schema that Claude sees; the docstring *is* the tool's API
  documentation.
- **`python -m jira_mcp`** runs the package's `__main__.py`, the closest thing Python
  has to an npm `bin` entry. It works from any directory because `uv sync` installed
  the project into the venv.
