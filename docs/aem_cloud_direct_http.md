# AEM Cloud Direct HTTP POC

This mode reuses the repository-oriented `AEMClient` and services against an AEM as a Cloud Service Author origin. It uses Sling JSON, QueryBuilder, Sling POST, Granite CSRF, DAM repository paths, and the reachable Package Manager service. It does not use Page Management or Assets Author OpenAPI for normal operations.

## Configuration

```dotenv
AEM_RUNTIME_MODE=cloud
AEM_CLOUD_PROVIDER_MODE=direct_http
AEM_CLOUD_DIRECT_AUTH_MODE=local_token
AEM_CLOUD_AUTHOR_URL=https://author-pXXXXX-eYYYYY.adobeaemcloud.com
AEM_CLOUD_LOCAL_TOKEN=<secret>
```

Local Development Tokens expire and are for development/testing only. Never commit, print, log, or share them. Production will use AEM Technical Account / Service Credentials to replace only token acquisition; the direct HTTP operation layer remains unchanged.

The outer MCP boundary still authenticates with Google OAuth. In this POC, every Google-authenticated MCP user operates through the same configured AEM Local Development Token, so AEM records the Adobe user who generated that token. This is not a production multi-user identity model. The `connect_aem_cloud` family remains exclusively for OpenAPI/per-user Adobe OAuth and is not required in direct mode.

Cloud direct mode enforces HTTPS, TLS verification, rejected redirects, Bearer-only downstream authentication, and strict per-write Granite CSRF acquisition. There is no localhost or OpenAPI fallback.

## Manual verification checklist

Use a disposable path allowed by the configured read/write roots. Substitute placeholders locally; do not paste secrets into scripts, shell history, screenshots, or issue reports.

- Page read: `GET <AUTHOR_ORIGIN>/content/<SITE>/<PAGE>.json`
- Direct content read: `GET <AUTHOR_ORIGIN>/content/<SITE>/<PAGE>/jcr:content.json`
- QueryBuilder page search: `GET <AUTHOR_ORIGIN>/bin/querybuilder.json?path=/content/<SITE>&type=cq:Page&p.limit=10`
- Component read: `GET <AUTHOR_ORIGIN>/content/<SITE>/<PAGE>/jcr:content/<COMPONENT>.json`
- Asset search: `GET <AUTHOR_ORIGIN>/bin/querybuilder.json?path=/content/dam/<ROOT>&type=dam:Asset&p.limit=10`
- Asset metadata: `GET <AUTHOR_ORIGIN>/content/dam/<ROOT>/<ASSET>/jcr:content/metadata.json`
- Page property write and cleanup: fetch `/libs/granite/csrf/token.json`, POST a harmless temporary property to the page `jcr:content`, verify it, then remove that exact temporary property.
- Component property write and cleanup: repeat the CSRF/write/verify/remove sequence on a disposable component.
- Package Manager reachability only: `GET <AUTHOR_ORIGIN>/crx/packmgr/service.jsp?cmd=ls`. Do not infer create/build support from this check.

Automated tests must use mocked HTTP and must never contact an AEM environment.

## Operations awaiting live/documentation proof

`publish_page`, `unpublish_page`, `publish_asset`, `unpublish_asset`, and `upload_asset` fail closed in direct mode. Each needs confirmation from current AEMaaCS documentation of the exact supported endpoint, authentication/CSRF requirements, payload, permissions, and success evidence, followed by a disposable-environment test. Package creation/build also remains execution-guarded until its create, filter persistence, build, and cleanup workflow is live-proven; dry-run remains available.
