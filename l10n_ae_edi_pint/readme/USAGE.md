Install only in an isolated Odoo 20.0 development database. Read the repository architecture and QA instructions before preparing synthetic documents. These Alpha modules are not approved for production fiscal transmission.

**Check UAE Data** includes the native XML builder's returned constraints for tax,
commercial and eligible self-billed documents. A missing buyer country produces
IBR-011; a missing supplier country produces IBR-009. Review the corresponding Contact
or authorized company address, save and repeat Check on the same draft. Check does not
post or freeze a fiscal issue. Unprepared Preview uses the same checked build once;
prepared Preview verifies and reads the original frozen XML. Local formal validation
and actual provider/FTA confirmations are separate operations. Native Send may still
export again after Check; no cross-request cache is introduced.

Native UAE fallback XML downloads refuse a returned builder error before releasing the
preview. A missing buyer country displays IBR-011 through Check and HTTP 422 through
that download. A valid unprepared draft preview does not post or create an issue;
prepared downloads retain the frozen original access/integrity checks. Known category,
reverse-charge nature, payment and AED consistency errors return per-document native
constraints. Direct integrations must honor `(content, errors)`; bytes with errors are
not usable invoices. This is focused local qualification, separate from formal PINT
validation and fiscal acceptance. See scenario 2 and its execution limits.

Accounting managers can open **Received UAE Documents**, inspect the preserved XML and
select **Process Received Document**. A supported ordinary AED tax invoice creates a
supplier bill in draft; **Open Accounting Document** opens it for accounting review. Unknown
suppliers/taxes, invalid XML and unsupported scenarios remain in review with diagnostics.
The original evidence cannot be edited, and a received document cannot be emitted again.

Native accounting branches can use eligible shared or parent-owned supplier, tax and
economic-role journal records under the receiving user's ordinary access rules. Keep
the receiving branch selected. Unrelated-company records remain excluded and ambiguity
requires review. Fiscal identities, credit references and self-billing agreements remain
specific to the receiving company; the parent's agreement does not authorize its branch.
Scenario23 in the PT/EN manuals describes this distinction and the focused test limits.

Before a new controlled receipt, an ordinary company-authorized Accounting Manager must
declare **Retrieve Incoming Documents** in the binding's controlled contract, enter a
review note and select **Review Contract Capabilities**. A receive-only contract is
allowed. Unreviewed contracts or contracts missing that operation refuse before any
original or delivery event is retained, including elevated calls. Editing the declaration
invalidates current approval; already retained originals remain available for inspection
and local manager processing. The technical receiving role does not grant contract
review or accounting processing. Scenario74 in the PT/EN manuals covers this boundary;
pre-HTTP refusal and scheduled retrieval still require separate requalification.

This inbox currently receives from the separately configured KMEE loopback mock worker.
It does not connect to an accredited provider sandbox or constitute FTA acceptance.

For periodic controlled receipt, enable **Automatic Controlled Receiving** on a sandbox
binding and assign the native **UAE: Controlled Document Receiving** scheduled action
to a dedicated **Controlled Document Receiver** account. Support configures the
loopback service outside ordinary binding fields. Each invocation preserves one bounded
queue page without importing or posting it. The binding displays a protected receiving
cursor/route and any configuration or retrieval review code. Failed pages remain
retrievable; the cursor cannot be reset through ordinary editing. Accounting managers
review each original separately in **Received UAE Documents**. See scenario 22 in the
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

The repository [scenario based user manual](https://github.com/kmee/l10n-uae/blob/20.0-add-uae-edi/docs/user/scenarios-pt_BR.md)
distinguishes tested user journeys from workflows still to implement.

For foreign-currency emission, the default is Odoo's dated native currency conversion
for the invoice conversion date. The frozen issue records its AED-per-document-unit rate,
conversion/source dates and the actual dated Odoo currency-rate records used; the source
is identified as Odoo currency rates and its CBUAE origin is explicitly unverified. Use
the three **Reviewed AED Exchange Rate**, **AED Exchange Rate Date** and **AED Exchange
Rate Source** fields together to supply a reviewed override. A partial override requires
review; clear all three to return to the native conversion. The effective conversion and
frozen evidence preserve the quote and converted AED line/header values. A mismatch
against the AED ledger, a missing/undated source, or a different VAT-point date blocks
preparation for accounting review. The source field is a human reference and does not
verify CBUAE publication.

For a received foreign-currency bill, review **Amounts Declared by the Sender** in the
UAE tab. Document currency, declared exchange rate, fiscal AED VAT and gross amounts
remain protected. They do not set the local reviewed quote or create a payment. Currency
must already be active; malformed rates/amounts and inconsistent fiscal lines stay in
review. Unequal converted-versus-declared AED headers create a draft with **Received AED
Header Review**: check the original XML and accounting before posting. The immutable
comparison preserves both values and does not replace declarations or certify tax
compliance. Matching headers have no discrepancy notice. Worked USD invoice/partial-credit
vectors and a native multiline receipt browser journey are tested; full combinations
remain pending.

The received-document form retains **Processing History**. Failed processing keeps
its operator, time, diagnostic and original XML hash even after reviewed master-data
correction allows a draft bill. Use **Process Received Document** again after the
correction; do not replace XML. A successful replay does not create another draft or
history entry. Fiscal auditors see read-only history without accounting access.

With native **Export: Allowed**, select **Download Evidence Package** in the
received original's form or UAE Audit inbox. Opening these forms does not build the ZIP.
An integrity failure refuses the explicit download while keeping the form available. It preserves exact XML, protected receipt
details, every processing attempt and retained report, credit references and a
SHA256/size manifest. Hash mismatch or a missing recorded report blocks export. Attempts
without reports retain that absence. Repeated downloads are deterministic until new
processing adds evidence. Download never revalidates or creates accounting/attachments.
This package is not a restorable database backup; retention and restore remain required.

For a received combined credit, process or review its original invoices first, then
inspect **Received Invoice References** in the inbox and accounting UAE tab. Authentic
numbers and optional XML dates are preserved. The draft keeps the credit's own lines
and totals; receipt creates no allocation, reconciliation or payment. Multiple
originals leave the singular native reversal relation empty. Missing or ambiguous
originals keep the whole credit in review. A reviewed legacy bill can be entered through
normal supplier accounting before retrying the same original XML. Never fabricate an
original or rewrite receipt evidence to bypass review.

For internal incoming attention, configure **UAE Incoming Document Reviewer** in the
company's **UAE E-Invoicing** tab using native company editing permission. Choose an
active internal Accounting Manager with access to that company and original. In the
received original, **Request Internal Review** reuses one protected task and native
activity, including after the activity is completed. Receipt remains independent of
reviewer availability. The hourly **UAE: Received Document Internal Review** cron checks
at most 100 eligible originals and records a configuration-review outcome when access
or reviewer configuration requires attention, while other rows continue.

After reviewed master-data recovery and successful processing of the same original,
use **Resolve Internal Review** in **UAE Incoming Reviews**. It preserves request,
assignment and processing history and completes only its own outstanding activity.
Completing an activity alone does not process XML, resolve conflicts or acknowledge a
fiscal document. Changing future company routing does not rewrite earlier assignments.
See scenario 29 in the PT/EN manuals for the selected native journey and proof limits.


When **Remote ID Conflict** is present, an Accounting Manager who can read every
retained original may use **Review Identity Variant**. Compare the exact original/hash
with all displayed members and business evidence, then enter **Justification** and
**Review Reference**. **Select This Original** records internal eligibility without
processing or posting. A new distinct XML received while the assistant is open requires
cancelling and reopening it. Historical conflict remains; **Identity Review Pending**
clears only for the current eligible original. Existing accounting links remain pinned
to their original. Multiple linked originals require escalation.

Process the selected original separately with **Process Received Document**. Internal
identity selections are append-only and separate from processing history. **Received
Evidence Package** includes the complete selection history and other retained originals
with their own processing/retention evidence. Missing access to any required original,
decision or artifact refuses the package; incomplete history is not silently omitted.
Packages refresh after processing or reviewed retention/hold changes. Selection/export
does not establish supplier authenticity or ASP/FTA acceptance. Full provider protocols,
adverse recovery, notification policy and acceptance matrices remain required.

For confirmed disclosed-agent transactions, select **Disclosed Agent Billing** and enter
**Principal ID**, the principal's 15-digit TRN. Keep supplier VAT and Free Zone beneficiary
separate. **Check UAE Data**, preparation and automatic preparation on posting diagnose
missing Principal ID (IBR-137) or equality with the economic supplier VAT (IBR-176).
The ASCII-digit format check is local semantic validation. Preparation freezes principal
and flags. Billing input accepts `l10n_ae_principal_id`; replay preserves the issue.
Supported received agent/Free Zone combinations conserve principal and original bytes
in draft accounting. Duplicate principal structure or an existing self-billed draft with
a different principal requires review. These flows do not establish ASP/FTA acceptance.


For a reviewed payment instrument, open the native Bank journal's **Incoming Payments**
or **Outgoing Payments** method lines and use **UAE Payment Instrument**. This optional
country metadata changes only document classification. A blank value retains supported
native cash/manual or bank/manual derivation; no preferred method means instrument `1`,
even when Recipient Bank is present. Cheque `20` uses the selected method's reviewed
classification and remittance, without inferring bank/card/mandate details. Frozen
originals remain unchanged after journal edits. Code `30` still requires a recipient
account. Scenario 57 in the PT/EN manuals records selected evidence and limitations; the
nine offered choices are not qualification of every instrument/document matrix.

A selected native method that does not identify a supported instrument requires review
of **UAE Payment Instrument** on its journal method line. Native Post keeps the invoice
posted and records UAE review without an issue/UUID; explicit Check/Prepare refuse the
missing classification. Review the same line, then prepare the existing invoice. The
online-service 68 tracer retains its reference without inventing card/mandate data;
subsequent master changes preserve its original and ledger. Scenario 58 in the PT/EN
manuals records the rendered journey and its limits. All nine reviewed codes have basic
ordinary 380/AED XML representation coverage, with a selected account required only for
transfer 30; this does not qualify every instrument/document/direction matrix or execute
payments. Structured optional groups remain pending.

The native payment-term form exposes reviewed **UAE Billing Frequency**, separate from
installments and due dates. Selecting a term in the invoice Form supplies an editable
cadence default. Explicit reviewed supply dates remain independent. **Other** requires
an explanation in **Other UAE Billing Frequency**, appended to the existing invoice
note. Check/Prepare refuse a missing explanation; Post retains native accounting.
Prepared cadence, explanation and original XML are protected against later edits.
Scenarios 59–60 in the PT/EN manuals document Monthly and Other UI journeys. API defaults,
eight other code qualifications, deferred-line mapping and full document/FX/received
matrices remain pending. The pinned executable rule uses DescriptionCode despite the
upstream semantic page's Description binding; research records that discrepancy.

For API billing cadence, `submit_document` uses the selected payment term's reviewed
frequency when `l10n_ae_billing_frequency_code` is omitted. An explicit value overrides
it; `false` or `null` clears it. Its response puts the accounting ID at
`accounting.move_id`; `get_document` returns the issue in `document`. Received
self-billing keeps the original cadence and full OTH Note on its protected draft; an
absent cadence is not inferred from local payment terms. See scenarios 61–62.

Computed AED line amounts and allowance bases preserve precision without reproduced
binary arithmetic tails. Quantity 3 ×USD 0.10 at 3.672500 retains raw line AED
VAT 0.0550875/gross 1.1568375 and prepares with native AED header 0.07/1.17. A full
discount retains allowance base/amount USD 0.3 and zero totals. Source inputs and ledger
remain unchanged.

For foreign documents on an AED ledger, retain the core AED TaxTotal and full accounting
gross normalized for document direction. Independently compare the reviewed quote with
the inverse persisted invoice_currency_rate at six decimals; mismatched quotes require
review. This avoids false cent-rounding refusals on matching multi-line invoices.
Non-AED ledgers retain their separate reviewed fiscal conversion. This is a documented
engineering policy, not a statutory FX aggregation equation or ASP acceptance. Check
and preview remain distinct from formal Validate:100 lines atUSD 1.23/5% with per-line
rounding still fail aligned-ibrp-s-09 for source VAT 6.00 versus expected6.15. No ledger
patch bypasses that rule. Scenario14 describes the actions, repair boundaries and
pending complete rounding/browser/ASP qualification.


For an unprepared UAE invoice, native **Send → Format for this Send** offers
**Customer default**, **PINT-AE** and **PDF only**. This choice belongs to the current
wizard and does not change the customer preference. Changing the sending method keeps
an explicit choice. PINT generation retains its mandatory checks; PDF-only generation
does not fulfill a required electronic invoice obligation. Already prepared originals
retain their PINT format. Native batch Send still uses customer defaults per invoice.
