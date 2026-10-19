"""Two-process concurrency check for payment allocation (MySQL).

Run only against a disposable database, never production:

    DATABASE_URL=mysql+pymysql://... python scripts/concurrency-check.py

The script seeds one customer, one invoice (1 000,00 DZD) and two payments of
1 000,00 DZD, then launches two worker processes.  Both try to settle the same
invoice.  Exactly one allocation must succeed; the other must be refused with
``payment.exceeds_balance``.  The invoice must end with exactly one allocation
and a zero balance.

The worker that wins sleeps for two seconds before committing, which gives the
other worker time to reach the row lock.  This exercises the real
``SELECT ... FOR UPDATE`` path under MySQL, not a simulated lock.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import date
from decimal import Decimal

from app import create_app
from app.extensions import db
from app.models import (
    Customer,
    DocumentStatus,
    Payment,
    PaymentDirection,
    PaymentMethod,
    SalesInvoice,
)
from app.services import payments
from app.services.errors import BusinessError

TOKEN = uuid.uuid4().hex[:10]


def worker(payment_id: int, invoice_id: int, *, delay: float, hold: float) -> int:
    app = create_app()
    with app.app_context():
        payment = db.session.get(Payment, payment_id)
        assert payment is not None
        time.sleep(delay)
        try:
            payments.allocate(
                payment,
                "sales_invoice",
                invoice_id,
                Decimal("1000.00"),
            )
            if hold:
                time.sleep(hold)
            db.session.commit()
            print("ALLOCATED", flush=True)
            return 0
        except BusinessError as error:
            db.session.rollback()
            print(f"REFUSED {error.message_key}", flush=True)
            return 2


def main() -> int:
    if "--worker" in sys.argv:
        index = sys.argv.index("--worker")
        payment_id = int(sys.argv[index + 1])
        invoice_id = int(sys.argv[index + 2])
        delay = float(sys.argv[index + 3])
        hold = float(sys.argv[index + 4])
        return worker(payment_id, invoice_id, delay=delay, hold=hold)

    app = create_app()
    with app.app_context():
        customer = Customer(code=f"CONC-{TOKEN}", name=f"Concurrency test {TOKEN}")
        db.session.add(customer)
        db.session.flush()

        invoice = SalesInvoice(
            number=f"CONC-INV-{TOKEN}",
            customer_id=customer.id,
            invoice_date=date.today(),
            status=DocumentStatus.POSTED.value,
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
        )
        payments_rows = [
            Payment(
                number=f"CONC-PAY-{TOKEN}-{index}",
                direction=PaymentDirection.INBOUND.value,
                payment_date=date.today(),
                amount=Decimal("1000.00"),
                method=PaymentMethod.CASH.value,
                customer_id=customer.id,
                status=DocumentStatus.POSTED.value,
            )
            for index in (1, 2)
        ]
        db.session.add(invoice)
        db.session.add_all(payments_rows)
        db.session.commit()
        invoice_id = invoice.id
        payment_ids = [row.id for row in payments_rows]

    script = os.path.abspath(__file__)
    common = [sys.executable, script, "--worker"]
    first = subprocess.Popen(
        [*common, str(payment_ids[0]), str(invoice_id), "0", "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(0.5)
    second = subprocess.Popen(
        [*common, str(payment_ids[1]), str(invoice_id), "0", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    first_output, _ = first.communicate(timeout=30)
    second_output, _ = second.communicate(timeout=30)

    print("worker 1:", first_output.strip(), f"(exit {first.returncode})")
    print("worker 2:", second_output.strip(), f"(exit {second.returncode})")

    with app.app_context():
        invoice = db.session.get(SalesInvoice, invoice_id)
        paid = invoice.paid_amount
        balance = invoice.balance_due

    outcomes = [first.returncode, second.returncode]
    ok = (
        sorted(outcomes) == [0, 2]
        and paid == Decimal("1000.00")
        and balance == Decimal("0.00")
    )
    print(f"invoice paid={paid} balance={balance}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
