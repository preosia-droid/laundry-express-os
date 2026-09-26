# Pay at Shop pre-orders — first backend slice

Implemented locally on `codex/preorders-20260926`, based on `df8ed83`. Not deployed.

## API contract

All routes use the existing same-origin JSON POST boundary.

- `/bookings/settings`: owner bearer session plus `businessId`, `branchId`; optional boolean `enabled`. Returns `enabled` and public `shopCode`. Disabled by default. Re-enabling preserves the link. This code identifies a public booking destination; it never grants access to customer records.
- `/bookings/shop`: `shopCode`. Returns business/branch display names, timezone and `PAY_AT_SHOP`. Disabled or unknown links return 404.
- `/bookings/submit`: `shopCode`, `requestId`, `customerName`, `phone`, optional `email`, `items`, optional `instructions`, `dropOffDate` (YYYY-MM-DD), `dropOffTime` (HH:mm in branch timezone), `termsAccepted: true`, `paymentChoice: PAY_AT_SHOP`.
- `/bookings/list`: owner bearer session plus business/branch scope, optional `limit` (1–100) and `before` from the preceding `nextCursor`. Returns a private inbox of requests, including contact details. STAFF, ADMIN, anonymous and other owners cannot access it.

Each item has a requested `service`, integer `quantity` (1–1000), optional `description` and optional integer `estimatedWeightGrams`. Up to 30 lines are accepted. New requests must select services from the branch's published list. A retry of an already accepted request remains valid after a service is removed. Preferred drop-off is a request, not a guaranteed appointment. The first version allows dates from today to 90 days ahead in the branch timezone.

## Service catalog

The customer page is `/book.html?shop=<code>`. Owner booking settings accept `services` (up to 100 unique names, 80 characters each) for an explicit online list, or `usePosCatalog: true` to clear the override. With no override, exactly one still-paired device catalog is required. Multiple catalogs fail closed to an empty list until the owner saves an explicit list. Empty lists disable new bookings. Public shop responses include only service names, never catalog prices or internal settings.

Sync advertises `serviceCatalogSupported: true`. Updated Android clients then capture active service names as a durable `catalog` event with id `services`; unchanged catalogs do not add revisions. Catalog events do not affect financial summaries. Older servers do not trigger catalog capture. Deploy the backend before updating operational tablets. Owner lists remain authoritative until explicitly switched to POS services.

The Android catalog implementation is built and tested separately from the operational tablet app. This does not implement POS booking conversion or customer tracking.

The client must create `requestId` using 32 cryptographically random bytes encoded as unpadded base64url (43 characters). Keep it across network retries, including the exact original payload. A retry returns the same reference; changing the payload with the same ID returns 409. Generate another ID only for an intentionally new booking. The server stores its hash. The booking response contains no contact details. It is not a tracking/claim credential.

The result is `SUBMITTED`, `PAY_AT_SHOP`, `UNPAID`. No payment, sales summary, inventory change or POS order is created. No client-supplied business identity, price, payment amount, acceptance flag or paid status is accepted. Public submissions and shop lookups have separate per-address limits. Rate limits also count malformed attempts.

## Storage and deployment

Three additive tables (`booking_shops`, `booking_services`, `preorders`) use the existing backend database adapter. Production initialization creates them inside the private `laundry` schema before the existing table grants are revoked and RLS is enabled. They are never queried directly through the public Supabase Data API. No new dependencies or paid services.

SQLite and HTTP tests cover this slice. PostgreSQL runtime behavior and production grants still need disposable-environment verification. Confirm how the deployment supplies `REMOTE_ADDR` before public release; an unconfigured reverse proxy may group customers under one address. Do not trust arbitrary forwarded headers.

## Next integration work

1. Customer form, owner enable/pause controls, private inbox and persistent retry handling are implemented locally; complete owner/browser acceptance and consent-version work before release.
2. Android authenticated POS inbox, staff verification of actual weight/quantity, server-validated catalogue pricing and atomic one-time conversion. Add device permissions separately from owner financial-report permissions.
3. Pre-order QR/reference; receipt/claim-stub QR; separate random tracking capability exposing only customer-safe fields. Never use a sequential order number or public shop code as authority.
4. Pay at Shop collection and reconciliation, then provider-confirmed online payments when a provider is selected and merchant credentials are configured. No online payment is active in this slice.
5. Define cancellation, expiry, retention and consent requirements, add abuse controls as needed, verify PostgreSQL and staging end to end, then deploy.

This is the backend foundation, not a finished customer booking feature. Existing POS records and live services were not modified.
