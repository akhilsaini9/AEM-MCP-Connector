# Custom AEM CRUD MCP

A learning MCP server that wraps a **local AEM Author SDK** and exposes both read and guarded write operations.

## MCP tools

### Read
- `get_page_properties(path)`
- `search_pages(root, text?, limit?, offset?)`
- `list_child_pages(root, limit?)`
- `find_component_usage(root, resource_type, limit?)`
- `list_components(page_path, max_depth=10, limit=200)`
- `get_component_properties(component_path)`
- `find_components(page_path, resource_type?, name_contains?, property_name?, property_value?, limit=100)`

### Write
- `create_page(parent_path, name, title, template, resource_type)`
- `update_page_properties(page_path, title?, description?, resource_type?)`
- `set_page_property(page_path, property_name, value)`
- `move_page(source_path, destination_path, confirm)`
- `delete_page(page_path, confirm)`
- `add_component(parent_path, node_name, resource_type, properties?)`
- `update_component_properties(component_path, properties)`

## Component tools

Components are discovered recursively below `<page_path>/jcr:content`; no
`root/responsivegrid` layout is assumed. A node is a component when it has a
non-empty `sling:resourceType`. Direct properties are retained while child objects,
common JCR audit metadata, very large strings, and oversized arrays are omitted or
bounded.

```text
# List page components
list_components("/content/sigma/us/en/example")

# Find text components
find_components("/content/sigma/us/en/example",
  resource_type="sigma/components/content/text")

# Get an exact component
get_component_properties(
  "/content/sigma/us/en/example/jcr:content/root/responsivegrid/text_123")

# Add below an existing container
add_component(
  parent_path="/content/sigma/us/en/mcp/test-page/jcr:content/root/responsivegrid",
  node_name="text_mcp",
  resource_type="sigma/components/content/text",
  properties={"text": "Created through MCP", "textIsRich": false})

# Update a component
update_component_properties(
  component_path="/content/sigma/us/en/mcp/test-page/jcr:content/root/responsivegrid/text_mcp",
  properties={"text": "Updated through MCP"})
```

Search also supports `name_contains="text"`, `property_name="text"`, and
`property_name="text", property_value="Hello"`. Results report `incomplete` and
`incompleteReasons` when `max_depth` or `limit` truncates traversal.

Component writes require `AEM_WRITE_ENABLED=true`; the owning page (the path before
the exact `/jcr:content/` boundary) must be below `AEM_WRITE_ROOTS`. Parents must
exist, adds refuse existing nodes, and updates block `jcr:*`, Sling POST controls,
and `sling:resourceType`. Exact container paths, resource types, and property names
depend on the Sigma component implementation.

Generic `properties` use a plain JSON object schema for broad MCP/model
compatibility. Runtime validation accepts scalar values and flat scalar arrays;
nested objects, binary values, and protected names are rejected.
`find_components.property_value` is intentionally a string in the MCP schema for
provider compatibility; property-existence filtering still works for every value
type.

## Safety model

Writes are disabled by default.

```env
AEM_WRITE_ENABLED=false
AEM_WRITE_ROOTS=/content/mcp-poc

# Optional add-component allowlist; empty permits any resource type.
AEM_COMPONENT_ALLOWED_RESOURCE_TYPES=
```

For your local POC, first create `/content/mcp-poc` manually or through AEM.

Then enable:

```env
AEM_WRITE_ENABLED=true
AEM_WRITE_ROOTS=/content/mcp-poc
```

This means the MCP can write to:

```text
/content/mcp-poc
/content/mcp-poc/test
/content/mcp-poc/demo/page
```

but it cannot modify:

```text
/content/wknd
/content/your-real-site
/conf
/apps
```

unless you deliberately add those roots.

`delete_page` and `move_page` also require `confirm=true`.

## Windows setup

```cmd
cd custom-aem-crud-mcp

python -m venv .venv
.venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt

copy .env.example .env
```

Typical local SDK:

```env
AEM_BASE_URL=http://localhost:4502
AEM_USERNAME=admin
AEM_PASSWORD=admin

AEM_ALLOWED_ROOTS=/content,/conf

AEM_WRITE_ENABLED=true
AEM_WRITE_ROOTS=/content/mcp-poc
```

## Test read access first

```cmd
python test_aem_connection.py /content
```

## Inspect MCP tools

```cmd
mcp dev run_server.py
```

Then use MCP Inspector.

## Recommended create-page test

You need a valid template and matching page component/resource type from your local project.

Example:

```text
create_page

parent_path = /content/mcp-poc
name = demo
title = MCP Demo Page
template = /conf/wknd/settings/wcm/templates/landing-page-template
resource_type = wknd/components/page
```

Use template/resource-type values that actually exist in your local AEM instance.

The result should create:

```text
/content/mcp-poc/demo
  jcr:primaryType = cq:Page

/content/mcp-poc/demo/jcr:content
  jcr:primaryType = cq:PageContent
  jcr:title = MCP Demo Page
  cq:template = ...
  sling:resourceType = ...
```

## Update test

```text
update_page_properties

page_path = /content/mcp-poc/demo
title = Updated MCP Demo
description = Changed through my custom MCP server
```

Or generic direct property:

```text
set_page_property

page_path = /content/mcp-poc/demo
property_name = navTitle
value = MCP Navigation Title
```

## Move test

```text
move_page

source_path = /content/mcp-poc/demo
destination_path = /content/mcp-poc/demo-moved
confirm = true
```

## Delete test

```text
delete_page

page_path = /content/mcp-poc/demo-moved
confirm = true
```

## How writes work

The server uses the Sling POST servlet over HTTP.

Creation/modification uses POST form properties.

Move sends:

```text
:operation=move
:dest=<destination>
```

Delete sends:

```text
:operation=delete
```

The client also tries to obtain a Granite CSRF token before POST calls, which makes local AEM POST behavior more robust.

## Run from an MCP client

Use an stdio configuration equivalent to:

```text
command = F:\custom-aem-crud-mcp\.venv\Scripts\python.exe

args =
-m
mcp
run
F:\custom-aem-crud-mcp\run_server.py
```

Use the actual absolute path on your machine.

## Suggested prompts

Component discovery prompts for Codex or n8n:

```text
Use list_components to discover components below /content/sigma/us/en/example.
Report component paths, resource types, and useful authorable properties.
```

```text
Find all sigma/components/content/text components on
/content/sigma/us/en/example and show their text properties. Do not write anything.
```

```text
Use get_component_properties for
/content/sigma/us/en/example/jcr:content/root/responsivegrid/text_123.
```

```text
Use my custom AEM MCP to create a page called mcp-test under
/content/mcp-poc using the WKND page template and page component.
```

```text
Change the title of /content/mcp-poc/mcp-test to "Created through MCP".
```

```text
Get the properties of /content/mcp-poc/mcp-test and verify the change.
```

```text
Move /content/mcp-poc/mcp-test to /content/mcp-poc/mcp-test-moved.
```

```text
Delete /content/mcp-poc/mcp-test-moved.
```

For move/delete, the MCP tool itself still requires `confirm=true`.

## Important

This project is intended for a **local AEM SDK learning POC**.

Do not point the write-enabled configuration at production or shared environments without replacing local Basic Auth and the simple guardrails with proper authentication, authorization, approval, logging, and environment restrictions.

## Transport modes

The tools are defined once in `aem_mcp/server.py`. Two launchers expose that same
server over different transports.

### Local Codex stdio

From a Windows command prompt in this repository:

```cmd
.venv\Scripts\activate
mcp run run_server.py
```

The existing Codex configuration remains valid:

```toml
[mcp_servers.aem_local_crud]
command = "F:\\custom-aem-crud-mcp\\.venv\\Scripts\\python.exe"
args = ["-m", "mcp", "run", "F:\\custom-aem-crud-mcp\\run_server.py"]
```

`run_server.py` intentionally remains a minimal SDK-discoverable stdio entry point.

### Remote Streamable HTTP

Choose a strong random bearer token, configure the allowed public hostname/origin,
and start the separate HTTP launcher:

```cmd
.venv\Scripts\activate
python run_http_server.py
```

Local endpoints:

```text
Health: http://localhost:8000/health
MCP:    http://localhost:8000/mcp
```

The health endpoint is intentionally unauthenticated. Every request to `/mcp`
requires:

```http
Authorization: Bearer <token>
```

Example remote client configuration after placing an HTTPS reverse proxy or secure
tunnel in front of the service:

```text
Transport: Streamable HTTP
URL: https://mcp.example.com/mcp
Header: Authorization: Bearer <the value of MCP_HTTP_BEARER_TOKEN>
```

Never expose the plain HTTP listener directly to the Internet. Terminate TLS at a
trusted reverse proxy or tunnel, restrict ingress where possible, and add the
external host and browser origin to `MCP_HTTP_ALLOWED_HOSTS` and
`MCP_HTTP_ALLOWED_ORIGINS`. Origin checks matter primarily for browser clients;
non-browser clients commonly omit `Origin`.

## Complete configuration example

```env
AEM_BASE_URL=http://localhost:4502
AEM_USERNAME=admin
AEM_PASSWORD=admin
AEM_ALLOWED_ROOTS=/content,/conf
AEM_WRITE_ENABLED=true
AEM_WRITE_ROOTS=/content/sigma/us/en/mcp
AEM_COMPONENT_ALLOWED_RESOURCE_TYPES=
AEM_TIMEOUT_SECONDS=20
AEM_VERIFY_SSL=false

MCP_TRANSPORT=stdio
MCP_HOST=127.0.0.1
MCP_PORT=8000
MCP_PATH=/mcp
MCP_HTTP_AUTH_ENABLED=true
MCP_HTTP_BEARER_TOKEN=replace-with-a-long-random-secret
MCP_HTTP_ALLOWED_HOSTS=127.0.0.1:*,localhost:*,mcp.example.com
MCP_HTTP_ALLOWED_ORIGINS=http://127.0.0.1:*,http://localhost:*,https://mcp.example.com
MCP_HTTP_MAX_BODY_BYTES=1048576
MCP_HTTP_LOG_LEVEL=INFO
```

Older `.env` files remain valid for stdio. HTTP mode fails closed when authentication
is enabled but its token is missing or still set to `change-me`.

Remote request logs are structured JSON and contain request metadata only. Request
bodies, query strings, passwords, bearer tokens, and Authorization headers are not
logged. The MCP SDK also enforces the configured request body limit and validates
Host, Origin, and JSON Content-Type headers.

## Architecture

```text
Remote n8n / hosted agent
          |
        HTTPS
          |
secure tunnel / reverse proxy
          |
custom AEM MCP HTTP server on this laptop
          |
 AEM Author SDK localhost:4502
```

Only the MCP service should be exposed. Do not publish AEM port 4502 to the Internet.

## Docker HTTP mode

The image runs as a non-root user, does not copy `.env`, and includes an HTTP health
check:

```cmd
docker compose up --build
```

When AEM runs on the Windows host, `localhost:4502` inside the container refers to
the container, not Windows. The supplied Compose file therefore overrides
`AEM_BASE_URL` with `http://host.docker.internal:4502`. Keep the bearer token and AEM
credentials in the local `.env`; do not bake them into an image.

## Tests

```cmd
.venv\Scripts\python.exe -m pytest
```

The automated suite mocks the AEM read call used by the HTTP transport test. It does
not create, move, or delete real AEM content. Any future destructive integration
tests should be opt-in and use a separately configured disposable root such as
`/content/mcp-poc`.

## Remaining security considerations

- A static bearer token is appropriate for a tightly controlled personal service,
  but multi-user deployments should use short-lived OAuth tokens and per-client
  authorization.
- Rotate the bearer token and AEM credentials periodically and after suspected
  disclosure.
- Keep `AEM_WRITE_ROOTS` narrow. `move_page` and `delete_page` still require
  `confirm=true`, but any holder of the bearer token can invoke all exposed tools.
- Apply reverse-proxy rate limits, connection timeouts, TLS, and network allowlists.
- The service is stateless for compatibility and simple horizontal operation;
  server-initiated MCP requests that require a persistent back-channel are not
  supported in this mode.

## AEM MCP V2

All original page/component CRUD signatures remain available. New tools:

```text
publish_page(page_path, include_references=false, dry_run=true, confirm=false)
unpublish_page(page_path, dry_run=true, confirm=false)
get_page_dependencies(page_path, limit=500)
validate_page(page_path)
search_assets(root="/content/dam", text=null, mime_type=null, limit=50, offset=0)
get_asset_metadata(asset_path)
get_asset_preview(asset_path, rendition=null, max_bytes=null)
upload_asset(dam_folder, file_name, content_base64, mime_type=null, metadata=null,
             overwrite=false, dry_run=true, confirm=false)
find_asset_usage(asset_path, root=null, limit=100)
update_asset_metadata(asset_path, properties, dry_run=true, confirm=false)
publish_asset(asset_path, dry_run=true, confirm=false)
unpublish_asset(asset_path, dry_run=true, confirm=false)
get_component_authoring_schema(resource_type)
get_component_definition(resource_type)
list_allowed_components(container_path, limit=200)
```

### Safety, impact, and audit

Every new consequential operation defaults to `dry_run=true`. Its preview contains
`operation`, `dry_run`, `affected_paths`, `planned_changes`, `warnings`, and
`requires_confirmation`, and never calls a mutation method. Execution requires
`dry_run=false`, `confirm=true`, `AEM_WRITE_ENABLED=true`, and the applicable
publication/DAM root. A missing confirmation returns a structured rejection.

Publication previews contain bounded page dependencies. Asset updates and asset
unpublication contain bounded usage information. Metadata-only JSON audit events
are emitted through `aem_mcp.audit`; credentials, request bodies, binary content,
Authorization headers, and CSRF tokens are never included.

Local publication verifies a fresh matching Author-side
`cq:lastReplicationAction`/`cq:lastReplicated` update after the replication request.
Responses distinguish `author_status_verified` from
`delivery_to_publish_verified`; the latter remains false unless a Publish tier or
reliable replication-agent delivery result is checked separately.

`get_page_dependencies` reports only reliably classifiable path references. Content
Fragment references remain DAM assets unless repository properties distinguish them;
the server does not guess from filenames. `validate_page` performs deterministic
checks for missing image references/resources, missing alt on non-decorative images,
empty text, and detectable broken internal paths.

### DAM and upload

DAM reads and writes are restricted separately. Search and usage use bounded
QueryBuilder calls; metadata is sanitized and rendition discovery is bounded.

The installed MCP SDK exposes JSON tool arguments rather than binary attachments,
so `upload_asset` accepts strict `content_base64` and never a host filesystem path.
Decoded size, MIME type, filename, destination root, and existing assets are checked.
Overwrite is off by default. The AEM 6.5 Local strategy sends raw binary to the
full `/api/assets/<folder>/<filename>` path: `POST` creates and explicit `PUT`
replaces an existing asset. This is isolated behind `LocalAssetUploadStrategy`
so a future Cloud strategy need not change the tool.

For Streamable HTTP, `MCP_HTTP_MAX_BODY_BYTES` must be configured above the complete
JSON request size. Base64 is roughly 4/3 the binary size, plus JSON/MCP framing, so
the default 1 MiB HTTP body limit cannot carry the default 25 MiB binary allowance.
Stdio and HTTP uploads both enforce the decoded binary limit independently.

Writable metadata is initially limited to `dc:title`, `dc:description`, `dc:subject`,
`cq:tags`, `xmp:Title`, and `xmp:Description`. Structural, binary, `jcr:*`, `sling:*`,
and Sling POST control properties are rejected.

### DAM preview

`get_asset_preview(asset_path, rendition=null, max_bytes=null)` retrieves a private
DAM asset through the MCP server's configured AEM authentication. It never returns
an AEM Author URL, credentials, access tokens, Authorization headers, or CSRF tokens.
Paths must be below both `/content/dam` and `AEM_DAM_READ_ROOTS`; traversal, encoded
separators, backslashes, malformed double slashes, external URLs, and all other
repository roots are rejected.

JPEG, PNG, WebP, and GIF previews are returned as protocol-native MCP
`ImageContent`, together with bounded metadata. The protocol represents image bytes
as base64 inside that native content block; they are not placed in a normal JSON
property. Selection uses an exact caller-requested rendition when supplied, then
prefers discovered web renditions, thumbnails, other renditions, and finally the
original only when it fits the configured limit. Rendition names are discovered
below `jcr:content/renditions`, not assumed to be identical across projects.

PDFs are returned as an MCP embedded `BlobResourceContents` resource with
`application/pdf` and a credential-free `aem-dam://` identifier. When AEM exposes a
safe image rendition, a native image thumbnail is returned as an additional content
block. Page count is included only when reliable DAM metadata already provides it;
the server does not parse large PDFs. Plain MCP clients decide how embedded PDF
resources are opened or rendered. Native image blocks are directly renderable by
clients that support MCP image output, but client UI support still varies. A custom
Apps SDK widget may be needed for a consistent rich PDF viewer; this server does not
claim that every ChatGPT surface renders PDFs inline.

`AEM_MAX_PREVIEW_BYTES` defaults to 5 MiB. A caller may request a smaller
`max_bytes`, but cannot raise the server cap. The download rejects an oversized
`Content-Length` before reading the body and also stops streamed responses when the
limit is crossed. `AEM_PREVIEW_ALLOWED_MIME_TYPES` is checked against trusted AEM
metadata and response MIME types. HTML, SVG/XML, missing, and mismatched content
types are not emitted as renderable previews. Preview audit records contain only the
asset path, selected rendition, MIME type, byte count, duration, and outcome.

Example prompts:

```text
Show me /content/dam/site/images/buddha.jpg using get_asset_preview.
Preview /content/dam/site/images/logo.png using get_asset_preview.
Open /content/dam/documents/product-guide.pdf securely through the AEM MCP.
Search DAM for Buddha statue images, then preview the top five selected results one at a time.
```

### Authoring intelligence

Dialog inspection resolves `/apps` with `/libs` fallback, follows
`sling:resourceSuperType` with loop/depth protection, traverses nested Granite UI,
normalizes `./name`, distinguishes local/inherited fields, preserves multifields,
and warns on unsupported widgets. Allowed components are returned only from an
explicit resolvable content policy; unavailable policy information produces partial
results and warnings, never an unrestricted `/apps` listing.

### V2 environment

```env
AEM_PUBLISH_ALLOWED_ROOTS=/content/mcp-poc
AEM_DAM_READ_ROOTS=/content/dam
AEM_DAM_WRITE_ROOTS=/content/dam
AEM_MAX_ASSET_SEARCH_LIMIT=200
AEM_MAX_ASSET_USAGE_LIMIT=500
AEM_MAX_ASSET_UPLOAD_BYTES=26214400
AEM_ALLOWED_ASSET_MIME_TYPES=image/jpeg,image/png,image/webp,image/gif,application/pdf
AEM_PREVIEW_ALLOWED_MIME_TYPES=image/jpeg,image/png,image/webp,image/gif,application/pdf
AEM_MAX_PREVIEW_BYTES=5242880
AEM_COMPONENT_DIALOG_MAX_INHERITANCE_DEPTH=10
MCP_AUDIT_LOG_ENABLED=true
MCP_AUDIT_LOG_LEVEL=INFO
```

An empty `AEM_PUBLISH_ALLOWED_ROOTS` falls back to `AEM_WRITE_ROOTS`, preserving old
`.env` compatibility.

### Local SDK and Cloud status

- **Works in unit tests / designed for Local:** bounded reads, validation,
  QueryBuilder operations, safety gates, audit, dialog inheritance, local replication,
  and the local multipart upload strategy.
- **Needs real AEM Local verification:** `/bin/replicate.json`, `/api/assets`
  permissions/responses, project dialog overlays, editable-template policy layout,
  direct rendition binary paths/content types, and project-specific rendition names.
- **Not implemented for Cloud:** direct-binary-upload orchestration, Cloud publication
  workflows, and asset-processing polling. Authenticated preview uses ordinary AEM
  reads but still needs Cloud permission, rendition, and response verification. Cloud
  preview compatibility is not claimed until that is tested.

Example prompts:

```text
Validate this page before I publish it: /content/mcp-poc/demo.
Show what would happen if I publish /content/mcp-poc/demo with its references.
Publish /content/mcp-poc/demo using dry_run=false and confirm=true.
Find Buddha statue images in DAM with MIME type image/jpeg.
Where is /content/dam/library/buddha.jpg being used?
Preview updating dc:title on /content/dam/library/buddha.jpg.
What fields can an author set on sigma/components/image?
What components are allowed in /content/mcp-poc/demo/jcr:content/root/container?
```

## Adobe AEM MCP downstream authentication POC

This optional integration makes this process both an MCP server and an MCP client:

```text
ChatGPT --session A--> this custom MCP --session B--> Adobe AEM MCP --> AEM Cloud
```

Session A authenticates ChatGPT to this server. Session B is a separate OAuth grant
for Adobe's MCP resource. The ChatGPT bearer token is never forwarded to Adobe.
Adobe tokens, OAuth client registration data, callbacks, and downstream sessions are
keyed by the trusted MCP subject when one is available.

The current HTTP middleware uses one static bearer token and does not produce a
trusted per-user subject. For that reason, downstream Adobe functionality refuses to
run by default. `ADOBE_MCP_SINGLE_DEVELOPER_MODE=true` provides an explicit local POC
escape hatch. **It is development-only and is not multi-user safe.** Production use
requires replacing the static inbound bearer mechanism with MCP OAuth validation that
provides a stable per-user subject.

Adobe's Cloud Manager MCP endpoint is:

```text
https://mcp.adobeaemcloud.com/adobe/mcp/cloudmanager
```

Adobe documents browser login with OAuth PKCE and requires custom MCP clients to be
allowlisted. Contact `aemcs-mcp-feedback@adobe.com` before live testing. This project
uses the Python MCP SDK's protected-resource/authorization-server discovery and does
not hardcode Adobe IMS endpoints or scopes. The redirect URI must exactly match the
URI accepted by Adobe. The callback route supplied by the HTTP server is:

```text
/adobe-mcp/oauth/callback
```

Configuration:

```env
ADOBE_MCP_ENABLED=false
ADOBE_MCP_SERVER_URL=https://mcp.adobeaemcloud.com/adobe/mcp/cloudmanager
ADOBE_MCP_ALLOWED_TOOLS=
ADOBE_MCP_SESSION_STORE=memory
ADOBE_MCP_OAUTH_REDIRECT_URI=
ADOBE_MCP_SINGLE_DEVELOPER_MODE=false
ADOBE_MCP_ENVIRONMENTS_TOOL=
ADOBE_MCP_CONNECT_TIMEOUT_SECONDS=30
```

`ADOBE_MCP_ALLOWED_TOOLS` uses comma-separated exact names. Empty means that every
downstream `call_tool` is denied; `tools/list` is still available. There is no
wildcard and no public arbitrary-tool proxy. After a real authenticated `tools/list`,
put one verified read-only environment tool in both `ADOBE_MCP_ALLOWED_TOOLS` and
`ADOBE_MCP_ENVIRONMENTS_TOOL`. Upstream names are deliberately not guessed.

The public POC tools are:

```text
get_adobe_mcp_connection_status()
connect_adobe_mcp()
disconnect_adobe_mcp()
list_adobe_mcp_tools()
list_aem_cloud_environments()
```

`connect_adobe_mcp` returns a browser authorization URL when login is required. Open
it, complete Adobe IMS login, and allow Adobe to redirect to the configured callback.
The SDK validates PKCE, state, and the authorization-response issuer and automatically
uses a refresh token when the authorization server issues one. Callback state is
one-use and routed to exactly one pending session. Disconnect closes only that
session and clears its local token/client-registration record. It does not claim
remote token revocation.

The memory token store is development-only, disappears at process restart, and is
not suitable for multiple replicas. Production requires an encrypted Redis/database
implementation with the same per-subject isolation. Tokens, authorization codes,
PKCE verifiers, cookies, and Authorization headers are excluded from tool responses
and audit logs.

Manual read-only proof:

1. Obtain Adobe custom-client allowlisting and an approved HTTPS redirect URI.
2. Enable Streamable HTTP and set the variables above. For a single local developer,
   explicitly set `ADOBE_MCP_SINGLE_DEVELOPER_MODE=true`.
3. Start with `python run_http_server.py` and connect ChatGPT to this MCP as usual.
4. Ask `Check my Adobe MCP connection status.`
5. Ask `Connect my Adobe MCP account.` and open the returned authorization URL.
6. Complete Adobe IMS/SSO/MFA and return to the chat.
7. Ask `List the tools exposed by the Adobe AEM MCP.`
8. Configure one discovered read-only environment tool in the exact-name mapping,
   restart, and ask `List the AEM Cloud environments available to me.`

If Adobe rejects the custom client, live verification stops with
`ADOBE_MCP_CLIENT_NOT_ALLOWLISTED`; do not reuse ChatGPT, Cursor, or another product's
client identity. Browser callback handling is implemented by the HTTP application.
Local stdio remains fully functional, but downstream browser OAuth is intentionally
documented as an HTTP POC because a remotely launched stdio process cannot reliably
present a callback URL to an end user.

No existing Local AEM tool is routed through Adobe MCP, and no Cloud write tool is
exposed by this POC.
# ChatGPT-facing Google OIDC authentication

HTTP authentication is selected with `MCP_AUTH_MODE=none|static_bearer|oauth`.
When `MCP_AUTH_MODE` is omitted, the legacy `MCP_HTTP_AUTH_ENABLED` switch is
still honored. In OAuth mode `/health` and OAuth discovery remain public while
`/mcp` requires a Google bearer access or ID token.

Production example (values are configuration, never commit secrets):

```dotenv
MCP_AUTH_MODE=oauth
MCP_OAUTH_ENABLED=true
MCP_PUBLIC_BASE_URL=https://aem-mcp-connector.onrender.com
MCP_OAUTH_ISSUER=https://accounts.google.com
MCP_OAUTH_CLIENT_ID=YOUR_GOOGLE_WEB_CLIENT_ID
MCP_OAUTH_CLIENT_SECRET=YOUR_GOOGLE_WEB_CLIENT_SECRET
MCP_OAUTH_AUDIENCE=YOUR_GOOGLE_WEB_CLIENT_ID
MCP_OAUTH_REQUIRED_SCOPES=openid,email,profile
```

Protected-resource metadata is published at both the requested root location
`/.well-known/oauth-protected-resource` and RFC 9728's path-aware location
`/.well-known/oauth-protected-resource/mcp`. Advertised URLs are derived from
`MCP_PUBLIC_BASE_URL` and `MCP_PATH`.

Signed ID/JWT tokens are verified with Google's OIDC discovery document and
rotating JWKS, including issuer, audience, time, subject, authorized-party,
scope/claim, and verified-email checks. Google's normal opaque access tokens are
validated online using Google's token-info and OIDC userinfo endpoints, including
audience, expiry, scopes, subject, and verified email. The raw Google bearer is
not retained or forwarded. Only the validated subject is placed in MCP request
context, keeping the downstream Adobe OAuth token store independent and
per-subject.

## Guarded AEM package creation

`create_package` creates an AEM 6.5 CRX package definition for exactly one
repository filter root and optionally builds it. It defaults to dry-run.
Execution requires `AEM_WRITE_ENABLED=true`, `confirm=true`, and a path allowed
by both `AEM_WRITE_ROOTS` and `AEM_PACKAGE_ALLOWED_ROOTS`.

```dotenv
AEM_PACKAGE_ALLOWED_ROOTS=/content,/content/dam,/conf
```

Dry-run example:

```json
{"path":"/content/mcp-poc/site","package_name":"site-backup","group":"mcp","version":"1.0.0","build":true,"dry_run":true,"confirm":false}
```

Confirmed example:

```json
{"path":"/content/mcp-poc/site","package_name":"site-backup","group":"mcp","version":"1.0.0","build":true,"dry_run":false,"confirm":true}
```

The AEM user configured by `AEM_USERNAME`/`AEM_PASSWORD` must have Package
Manager permissions on `/etc/packages` and read access to the filter root.
