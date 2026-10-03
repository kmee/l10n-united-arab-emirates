Install only in an isolated Odoo 20.0 development database. Read the repository architecture and QA instructions before preparing synthetic documents. These Alpha modules are not approved for production fiscal transmission.

Accounting managers can open **Received UAE Documents**, inspect the preserved XML and
select **Process Received Document**. A supported ordinary AED tax invoice creates a
supplier bill in draft; **Open Supplier Bill** opens it for accounting review. Unknown
suppliers/taxes, invalid XML and unsupported scenarios remain in review with diagnostics.
The original evidence cannot be edited, and a received document cannot be emitted again.

This inbox currently receives from the separately configured KMEE loopback mock worker.
It does not connect to an accredited provider sandbox or constitute FTA acceptance.

Received tax credit notes in AED can become draft supplier refunds when their single
preceding invoice reference matches an existing supplier bill. The declared credit
reason, prepaid amount and payable amount remain available for review. Processing does
not post, reconcile or import payments. Missing or ambiguous references stay in the
received-document review inbox with their original XML.
