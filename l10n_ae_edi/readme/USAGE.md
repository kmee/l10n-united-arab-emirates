Install only in an isolated Odoo 20.0 development database. Read the architecture and QA instructions before preparing synthetic documents. In the company's UAE E-Invoicing tab, confirm identity, select a sandbox ASP binding and optionally enable queuing on posting. An explicit invoice binding overrides the company default. The controlled external process sends committed intents; API acceptance does not confirm delivery or FTA reporting. These Alpha modules are not approved for production fiscal transmission.

For a separate billing query account, assign **UAE Integration → Billing Status Reader**
to a plain active internal user with only its permitted companies. The native Odoo20
JSON2 API provides scoped status, preserved PINT XML and paginated event facts without
accounting or sending privileges. Preparing an existing posted sandbox invoice is a
separate operation requiring native Invoice/write permissions. See
[the billing API contract](https://github.com/kmee/l10n-eua/blob/20.0-add-uae-edi/docs/controlled-executor.md#billing-api)
and the PT/EN scenario manuals. Native API keys belong to Odoo; the addon adds no key
store or credential fields. Full client creation/callback integration remains pending.

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
[English](https://github.com/kmee/l10n-eua/blob/20.0-add-uae-edi/docs/user/scenarios-en.md)
or [Português](https://github.com/kmee/l10n-eua/blob/20.0-add-uae-edi/docs/user/scenarios-pt_BR.md).

For scheduled controlled sending, accounting managers open **UAE Provider Bindings**,
select the reviewed **Sandbox** binding and enable **Automatic Controlled Dispatch**.
Support must configure a native Odoo cron worker and protected local API credentials.
The default is off; production bindings cannot enable it. The native job checks eligible
committed invoices every minute. Queuing on posting is a separate company option.
API acceptance leaves fiscal/delivery confirmations pending. Missing scheduler
configuration leaves the invoice queued; ask support to inspect native Scheduled Actions.
Archiving prevents new submissions but retains reconciliation of existing attempts while
scheduling stays enabled. Disabling scheduling also stops automatic lookup.
