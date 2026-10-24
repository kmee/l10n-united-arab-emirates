Optional Sales integration for the UAE PINT-AE localization. It carries references to
taxed down payments from native Sales invoices into the final invoice's frozen fiscal
evidence and PINT XML, while preserving Odoo's native down-payment accounting. The
generic accounting localization remains installable without Sales.

During fiscal preparation, the integration checks that each linked advance is posted and
belongs to the same legal company, commercial customer, document currency, and document
category. When automatic preparation is enabled, posting validates eligible moves as
well. It prevents deductions from exceeding the advance's remaining net and VAT amounts,
accounting for previously frozen UAE deductions and qualifying posted native deductions.

The final invoice shows the original advance number and date together with the consumed
net and VAT. Its PINT XML references the original advance. A payment settles the advance
invoice; the final invoice keeps the balance calculated by the native Sales down-payment
flow, without subtracting that payment again or creating another payment.
