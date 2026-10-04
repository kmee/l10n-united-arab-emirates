Use only an isolated Odoo20 development database with synthetic data and a controlled
sandbox binding. Configure a reviewed advance account in Sales, create and confirm
an order, and use the native Create Invoice wizard for a percentage down payment.
Post and prepare that invoice, then register its receipt through native Accounting.
Use the regular invoice wizard with down payment deduction for the balance.

The final invoice's UAE E-Invoicing tab displays **Taxed Advance References** for
review. Its PINT XML includes the original number/date. The advance's receipt belongs
to the advance invoice; it is not subtracted again from the final invoice's payable
amount. XML frozen before receipt keeps its original declared payment amounts.

These steps describe the implemented model workflow. Browser execution of this
journey and partial reversal of the final balance remain pending. Production transport
and accredited ASP homologation remain unavailable.
