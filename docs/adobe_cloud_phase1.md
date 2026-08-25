# Adobe Cloud provider — Phase 1

`AEM_RUNTIME_MODE=local` remains the default. In `cloud` mode the ten existing
read tools below use the current MCP user's direct Adobe IMS OAuth Web App session.
There is no Adobe MCP, technical account, local token, global token, or local AEM
fallback.

| Existing tool | Adobe operation | API status | Headers |
|---|---|---|---|
| `get_page_properties` | List Pages by exact `authorPath`, Get Page Properties | Page Management 2026.06/07 experimental | `Authorization: Bearer` |
| `list_child_pages` | List Pages by `parentPageId` | Page Management experimental | `Authorization: Bearer` |
| `find_component_usage` | Advanced Page Search, exact `componentType`, scoped by `parentPageId` | Page Management experimental | `Authorization: Bearer` |
| `list_components` | Get Page Content | Page Management experimental | `Authorization: Bearer` |
| `get_component_properties` | Get Page Content plus deterministic tree lookup | Page Management experimental | `Authorization: Bearer` |
| `get_component_authoring_schema` | Advanced Page Search plus Get Page Content Definition | Page Management experimental | `Authorization: Bearer` |
| `list_allowed_components` | Get Page Content Definition placement rules | Page Management experimental | `Authorization: Bearer` |
| `search_assets` | Search Assets | Assets Author 2026.07 experimental | `Authorization: Bearer` |
| `get_asset_metadata` | Get Asset Metadata | Assets Author stable | `Authorization: Bearer`, `X-Api-Key` |
| `get_asset_preview` | Get Asset (web-optimized binary) | Assets Author 2026.07 experimental | `Authorization: Bearer`, `X-Api-Key` |

No `X-Adobe-Accept-Experimental` header appears in the current referenced
specifications, so the client does not invent it. API roots are isolated in
`AEMCloudApiRegistry` and configurable only by deployment settings, never callers.

## Resolution and parity

Page repository paths are resolved with List Pages' exact `authorPath` filter; the
opaque page ID remains internal and is never cached globally. Component paths in
cloud output are deterministic authored locations assembled from Page Content node
IDs below `<page>/jcr:content`. They may differ from raw JCR node names returned by
local Sling JSON.

DAM paths are resolved with Assets Search on `repositoryMetadata.repo:path`, then
checked for one exact matching result before its asset URN is used. Asset search has
no documented path-prefix operator in the current specification, so results are
also filtered against `AEM_DAM_READ_ROOTS` and the requested root locally. This can
produce fewer results but cannot widen access.

Page properties expose supported structured Page metadata, normalized to familiar
keys where possible; arbitrary `jcr:content` properties are not fabricated. Content
definition schemas are effective cloud definitions, not raw `cq:dialog` inheritance.
Named renditions are unavailable through the verified Get Asset operation; images
use its web-optimized binary and PDFs use its returned binary only when MIME and
size checks pass.

## Deployment prerequisites

The Adobe Developer Console project must include the relevant Page Management and
Assets Author APIs on the OAuth Web App credential, with scopes offered by that
project (commonly `AdobeID`, `openid`, `aem.assets.author`, and `aem.folders` for
Assets; Page scopes must be selected from the Console rather than guessed). The ADC
client must be registered with the target AEM environment. For AEM project-based
registration this commonly requires the documented `api.yaml` configuration and
`allowedClientIDs`, followed by deployment before live calls succeed.

A `403` is deliberately reported as ambiguous: user ACLs/Product Profiles, API
enablement, or client/environment registration may be responsible.

Phase 2 remains unsupported in cloud mode: `search_pages`, all writes, publication,
package management, dependency/validation reads not listed above, and direct
QueryBuilder/Sling fallbacks. They fail closed and never contact local AEM.
