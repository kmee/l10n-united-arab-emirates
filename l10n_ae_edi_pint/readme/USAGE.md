Install only in an isolated Odoo 20.0 development database. Read the repository architecture and QA instructions before preparing synthetic documents. These Alpha modules are not approved for production fiscal transmission.

Accounting managers can open **Received UAE Documents**, inspect the preserved XML and
select **Process Received Document**. A supported ordinary AED tax invoice creates a
supplier bill in draft; **Open Accounting Document** opens it for accounting review. Unknown
suppliers/taxes, invalid XML and unsupported scenarios remain in review with diagnostics.
The original evidence cannot be edited, and a received document cannot be emitted again.

This inbox currently receives from the separately configured KMEE loopback mock worker.
It does not connect to an accredited provider sandbox or constitute FTA acceptance.

For periodic controlled receipt, enable **Automatic Controlled Receiving** on a sandbox
binding and assign the native **UAE: Controlled Document Receiving** scheduled action
to a dedicated **Controlled Document Receiver** account. Support configures the
loopback service outside ordinary binding fields. Each invocation preserves one bounded
queue page without importing or posting it. The binding displays a protected receiving
cursor/route and any configuration or retrieval review code. Failed pages remain
retrievable; the cursor cannot be reset through ordinary editing. Accounting managers
review each original separately in **Received UAE Documents**. See scenario22 in the
PT/EN user manuals for the tested workflow and suspension semantics.

An administrator may assign **UAE Integration / Controlled Document Receiver** to a
dedicated internal service account with only the required companies. The controlled
worker uses `process=False` for that account and preserves original XML without creating
an accounting draft. An active accounting manager then processes the inbox through the
native form. Archiving the service or removing its role/company access stops subsequent
retrieval and receipt; support can resume lookup of the original after reviewed
restoration. The role grants no accounting, sending, confirmation-event or binding
configuration permissions.

Received tax credit notes in AED can become draft supplier refunds when their single
preceding invoice reference matches an existing supplier bill. The declared credit
reason, prepaid amount and payable amount remain available for review. Processing does
not post, reconcile or import payments. Missing or ambiguous references stay in the
received-document review inbox with their original XML.

Ordinary AED commercial invoices and credits can also be processed. A supplier without
a VAT identifier must already have the matching routing endpoint, legal registration
number/type and issuing authority. Unknown identities and tax mappings stay in review.
The received category and original currency are protected; declared amounts keep the
XML currency even if the accounting draft is reviewed in another currency.

Buyer-issued self-billed invoices and credits in AED can become draft customer invoices
and refunds when the local company is the XML supplier. A manager must maintain one
valid UAE agreement with local role **Supplier Receiving Self-Billing** for the known
buyer. Existing matching billing drafts are reused without replacing their lines or
origin identifiers; differences remain in review. A received self-billed credit needs
one or more uniquely matching original self-billed sales. The agreement reference at receipt remains
available after later agreement changes. Posting received sales never emits them again.

When the sender declares a prepaid amount, the accounting history identifies it as a
remote declaration for review. Receiving does not create a local payment. Confirm any
local settlement through the usual accounting review and posting process.

The repository [scenario based user manual](https://github.com/kmee/l10n-eua/blob/20.0-add-uae-edi/docs/user/scenarios-pt_BR.md)
distinguishes tested user journeys from workflows still to implement.

For foreign-currency emission, enter the reviewed AED-per-document-unit quote, its
date and source in **UAE E-Invoicing** before checking or posting. The quote must be
positive, use a decimal point and have at most six decimal places. The frozen evidence
preserves it and the converted AED line/header values. A mismatch against an AED ledger
blocks preparation for accounting review. The rate source/date are human references;
automatic CBUAE acquisition and full tax-point/rounding policy remain pending.

For a received foreign-currency bill, review **Amounts Declared by the Sender** in the
UAE tab. Document currency, declared exchange rate, fiscal AED VAT and gross amounts
remain protected. They do not set the local reviewed quote or create a payment. Currency
must already be active; inconsistent fiscal values stay in the inbox for review. Worked
USD invoice/partial-credit vectors are tested; full combinations remain pending.

The received-document form retains **Processing History**. Failed processing keeps
its operator, time, diagnostic and original XML hash even after reviewed master-data
correction allows a draft bill. Use **Process Received Document** again after the
correction; do not replace XML. A successful replay does not create another draft or
history entry. Fiscal auditors see read-only history without accounting access.

For a received combined credit, process or review its original invoices first, then
inspect **Received Invoice References** in the inbox and accounting UAE tab. Authentic
numbers and optional XML dates are preserved. The draft keeps the credit's own lines
and totals; receipt creates no allocation, reconciliation or payment. Multiple
originals leave the singular native reversal relation empty. Missing or ambiguous
originals keep the whole credit in review. A reviewed legacy bill can be entered through
normal supplier accounting before retrying the same original XML. Never fabricate an
original or rewrite receipt evidence to bypass review.
