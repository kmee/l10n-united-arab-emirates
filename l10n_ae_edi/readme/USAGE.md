Install only in an isolated Odoo 20.0 development database. Read the architecture and QA instructions before preparing synthetic documents. In the company's UAE E-Invoicing tab, confirm identity, select a sandbox ASP binding and optionally enable queuing on posting. An explicit invoice binding overrides the company default. The controlled external process sends committed intents; API acceptance does not confirm delivery or FTA reporting. These Alpha modules are not approved for production fiscal transmission.

For a separate billing query account, assign **UAE Integration → Billing Status Reader**
to a plain active internal user with only its permitted companies. The native Odoo20
JSON2 API provides scoped status, preserved PINT XML and paginated event facts without
accounting or sending privileges. Preparing an existing posted sandbox invoice is a
separate operation requiring native Invoice/write permissions. See
[the billing API contract](https://github.com/kmee/l10n-uae/blob/20.0-add-uae-edi/docs/controlled-executor.md#billing-api)
and the PT/EN scenario manuals. Native API keys belong to Odoo; the addon adds no key
store or credential fields. Full client creation/callback integration remains pending.

**Fiscal Evidence Auditor** adds read access to transport attempts, original billing
receipts, local correction history and received documents. It inherits Billing Status
Reader and grants no accounting, provider configuration, dispatch or processing rights.
Native Data Export must be granted separately; internal worker tokens stay restricted.
Open **UAE Audit** to review issued/received documents, attempts, confirmations,
billing receipts and corrections in read-only views. **Original PINT XML** downloads
the preserved issue bytes after checking their hash; it creates no attachment or send.
For a permitted export, select the issue and use native **Actions → Export**.
Company and role revocation apply to subsequent reads, exports and downloads. Full
archive/retention and customer security acceptance remain required. See scenario21
in the PT/EN manuals and
[the audit UI evidence](https://github.com/kmee/l10n-uae/blob/20.0-add-uae-edi/docs/evidence/fiscal-audit-ui-2026-10-05.md).

For an outbound self-billed purchase, use a native self-billing purchase journal and
select a UAE Self-Billing Agreement in the invoice's UAE tab. Accounting managers open **Invoicing → Configuration → UAE Self-Billing Agreements**
to maintain buyer-role agreements for the company and commercial supplier, including reference,
supply scope, validity dates and a reference to reviewed supplier onboarding evidence.
Both parties need UAE VAT TRNs and eligible participant endpoints. Missing or expired
agreements block automatic posting/queuing. Only tax invoices/credit notes are eligible;
ordinary purchases and received documents do not emit. Invoice389 and credit261
preserve the company as buyer issuer, the partner as supplier and the selected agreement
in immutable transmission evidence. The checks do not query a live VAT or ASP registry.

For incoming buyer-issued self-billing, use a separate agreement with local role
**Supplier Receiving Self-Billing**, referencing the known buyer and reviewed onboarding
evidence. Received accounting forms display the original agreement reference and process
without enabling another outbound transmission.

For an outgoing UAE sale adjustment, select the official **UAE Credit Note Reason** in
the native **Credit Note** wizard, then use **Reverse** to create a draft. Review any
partial quantities/prices and original reference before checking and posting. The code
is also editable in the draft's UAE tab. Free-text reason alone does not satisfy the
emission check; automatic preparation blocks posting until an official code is selected.
Received documents and ordinary vendor reversals do not use this outbound wizard field.

For a buyer-issued self-billed purchase adjustment, use the same native wizard and
review partial lines. The resulting vendor credit retains the self-billing journal,
official reason and original reference. Select an eligible **UAE Self-Billing Agreement**
again for its company, commercial supplier and date: the wizard does not copy it.
Missing agreement blocks check/post with automatic preparation before UUID/transmission.
Supplier contacts use their commercial entity's agreements. Confirm the purchase fiscal
position and line VAT with the tax specialist if the expected tax is unavailable.

For a reviewed commercial sale or credit, select **Commercial Document** and the taxes
determined by the tax specialist. With the PINT addon installed, **IBR-122-AE** blocks
standard VAT at **Check UAE Data** and automatic preparation on **Confirm**, retaining
the draft without a UUID/transmission. Review the line taxes; the system does not change
their classification automatically. Formal PINT validation remains required. The native
credit wizard preserves the commercial category, which is protected after preparation.

For a correction covering several original invoices, create a separate customer credit
and use **Reviewed Credit Allocations** in its UAE tab. Select each posted local
original and enter its allocated net/VAT in the credit's currency. The original
number/date are preserved; company, customer, currency, category and process must match.
External originals require authentic number/date and **External Original Review
Evidence**, without a fabricated accounting invoice. Allocation totals must match the
credit's own net/VAT. Preparation protects the rows; XML contains separate original
references, while allocation amounts stay in internal evidence. These links do not
reconcile accounting balances. Volume discount handling remains unresolved.

If controlled batch validation stops a prepared document locally, its UAE tab shows
**Local Sending Review**, reason and recording time. Use **Validate UAE E-Invoice**
to inspect the preserved document. The document is excluded from subsequent automatic
batch selections while other eligible invoices continue. This records no remote
attempt or ASP rejection and does not modify the posted invoice or frozen evidence.
Automatic correction/unblocking and complete operational recovery remain required work.

For prerequisites, UI steps, expected results and error handling, read the repository
scenario based user manual in
[English](https://github.com/kmee/l10n-uae/blob/20.0-add-uae-edi/docs/user/scenarios-en.md)
or [Português](https://github.com/kmee/l10n-uae/blob/20.0-add-uae-edi/docs/user/scenarios-pt_BR.md).

For scheduled controlled sending, accounting managers open **UAE Provider Bindings**,
select the reviewed **Sandbox** binding and enable **Automatic Controlled Dispatch**.
Support must configure a native Odoo cron worker and protected local API credentials.
The default is off; production bindings cannot enable it. The native job checks eligible
committed invoices every minute. Queuing on posting is a separate company option.
API acceptance leaves fiscal/delivery confirmations pending. Missing scheduler
configuration leaves the invoice queued; ask support to inspect native Scheduled Actions.
Archiving prevents new submissions but retains reconciliation of existing attempts while
scheduling stays enabled. Disabling scheduling also stops automatic lookup.

Fiscal Evidence Auditor users with separate native Data Export permission can download
an issued revision's **Fiscal Evidence Package** from UAE Audit. Its versioned ZIP
preserves the snapshot/XML, attempts, original events and related billing/correction
metadata with a hash inventory. Existing preserved conventional files are included
without rendering. Corrupt originals and partial access are refused. See scenario25 in
the PT/EN manuals. This audit export does not replace backups, retention/holds or a
restored-database verification.

Accounting managers can open **Invoicing → Configuration → UAE Document Deadlines**
to review an explicit basis, inclusive due date, reminder date, responsible user and
requested fact for an invoice or credit note. Approve the reviewed basis before
requesting an activity. **Assess Deadline** is read-only and uses the current UAE day;
**Schedule Internal Reminder** creates one native To-Do when eligible. The hourly
job checks approved records and isolates records requiring renewed owner/access
review. API receipt and fiscal confirmations remain distinct. Completion of an activity
does not reset reminder history. See scenario28 in the PT/EN manuals. These internal
reminders do not determine legal timeliness or notify the FTA; full incident/calendar
and escalation requirements remain open.

With native product-manager rights, use **Customers → Products → New** and open
**UAE Classification**. A service requires a reviewed SAC and scheme version; goods
require HS, and a reviewed mixed item requires both. No classification code or SAC
version is supplied automatically. Single-variant template fields update that variant;
multiple variants must be configured individually. Ambiguous template classification
writes are refused. Select the actual product in invoice lines before preparation.
Later product edits do not rewrite the prepared payload, XML or ledger. See scenario47
in the PT/EN manuals for the service journey and its controlled-provider limits.
