# A10. Embedded importer SaaS (Flatfile, OneSchema, CSVBox, Dromo-style products)

> **Verdict:** the fastest way to get a polished mapping and cleanup UI, with fuzzy header matching, inline fixes and validation hooks. It's a business decision more than a technical one: **customer PII passes through a third party**, pricing is usage-based, and there may be row or file limits. We would still own the server-side merge into `contacts`.

## How it works

```
our page ─ embeds vendor widget (JS SDK / iframe)
   user drops file ─▶ vendor parses (in browser or vendor cloud)
                   ─▶ vendor UI: column matching, inline edits, our validation rules/hooks
                   ─▶ on submit: vendor delivers clean records
                         ├─ to our webhook in batches (JSON), or
                         └─ we pull via vendor API / file export
our server ─▶ batch endpoint ─▶ same COPY stage ─▶ merge ─▶ checkpoint (idempotent per batch)
```

## Pros

- **Best-in-class mapping UX with little effort:**
  - fuzzy and learned header matching
  - per-cell error highlighting
  - the user fixes rows *before* submitting, which our rejected-rows CSV only approximates
- **Handles messy formats:** XLSX, multiple sheets, and encodings (including UTF-16/cp1252) are the vendor's problem.
- **Fast to launch.** It's days of integration instead of weeks of UI work.
- **Validation hooks** let us run our phone rules (or call our API) inside their flow.

## Cons

- **PII leaves our infrastructure.**
  - Names and phone numbers are processed by, and possibly stored at, the vendor.
  - This needs a DPA, a data-residency review (India's DPDP Act, customer contracts), and security review.
  - Some vendors offer in-browser-only processing or self-hosting on enterprise tiers. Verify before assuming.
- **Usage-based pricing** (per import, per row or per seat). It's cheap at pilot scale and material at volume. Get quotes for 1M-row files specifically.
- **Plan limits.** Many plans cap rows per file or total rows. Browser-side processing can struggle at around 1M rows.
- **Lock-in.** The mapping configuration, validation hooks and UX are vendor-specific.
- **Availability dependency.** Their outage is our outage for imports.
- **We still need the backend:** batch ingestion, idempotency, dedupe against existing contacts, counters, keyset fetch and exports. That's most of the chosen design's server side.

## Cost

- A subscription plus usage fees, plus integration and security review time.
- Our infrastructure cost for the ingestion endpoint is the same as the chosen design without file storage.

## When to choose it

- Mapping and cleanup UX is the product differentiator and we can't staff the UI work.
- Customers mostly upload XLSX with messy, varied schemas.
- Legal has approved the vendor as a sub-processor for contact PII.

## Migration from the chosen design

It's additive:
1. Keep our upload path.
2. Add a vendor-webhook batch endpoint that reuses `flush()`, with a `(import_id, batch_seq)` idempotency guard.

The `contacts` table, dedupe, UI progress and fetch endpoints are unchanged. The two paths can run side by side for an A/B comparison.
