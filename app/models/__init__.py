"""All ORM models.

Importing this package registers every table on the SQLAlchemy metadata, which is
what Alembic autogeneration and ``db.create_all()`` rely on.
"""
from app.models.base import (  # noqa: F401
    FACTOR,
    MONEY,
    PERCENT,
    QUANTITY,
    ActorMixin,
    CustomerType,
    DocumentStatus,
    ExpenseType,
    MeasurementSource,
    MovementDirection,
    MovementReason,
    OwnershipType,
    PaymentDirection,
    PaymentMethod,
    PriceList,
    SoftDeleteMixin,
    TimestampMixin,
    TripStatus,
    UnitKind,
    ValuationMethod,
    WeighingDirection,
)
from app.models.catalog import (  # noqa: F401
    Brand,
    Category,
    Product,
    ProductConversion,
    ProductUnit,
    UnitOfMeasure,
)
from app.models.fleet import Carrier, Driver, Trip, TripExpense, Vehicle  # noqa: F401
from app.models.inventory import (  # noqa: F401
    StockAdjustment,
    StockAdjustmentLine,
    StockCount,
    StockCountLine,
    StockLevel,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    Warehouse,
)
from app.models.parties import Customer, PartyMixin, Supplier  # noqa: F401
from app.models.payments import Payment, PaymentAllocation  # noqa: F401
from app.models.platform import AuditEvent, BusinessSettings, NumberSequence  # noqa: F401
from app.models.purchasing import (  # noqa: F401
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReturn,
    PurchaseReturnItem,
    SupplierInvoice,
    SupplierInvoiceItem,
)
from app.models.sales import (  # noqa: F401
    CreditNote,
    CreditNoteItem,
    DeliveryNote,
    DeliveryNoteItem,
    Quotation,
    QuotationItem,
    SalesInvoice,
    SalesInvoiceItem,
    SalesOrder,
    SalesOrderItem,
)
from app.models.security import ALL_ROLES, ROLE_PERMISSIONS, Role, User, permissions_for  # noqa: F401
from app.models.weighbridge import (  # noqa: F401
    WeighbridgeDevice,
    WeighingCorrection,
    WeighingTicket,
)

__all__ = [
    "User", "Role", "ALL_ROLES", "ROLE_PERMISSIONS", "permissions_for",
    "Customer", "Supplier",
    "Category", "Brand", "Product", "ProductUnit", "ProductConversion", "UnitOfMeasure",
    "Warehouse", "StockLevel", "StockMovement", "StockAdjustment", "StockAdjustmentLine",
    "StockTransfer", "StockTransferLine", "StockCount", "StockCountLine",
    "Quotation", "QuotationItem", "SalesOrder", "SalesOrderItem",
    "DeliveryNote", "DeliveryNoteItem", "SalesInvoice", "SalesInvoiceItem",
    "CreditNote", "CreditNoteItem",
    "PurchaseOrder", "PurchaseOrderItem", "GoodsReceipt", "GoodsReceiptItem",
    "SupplierInvoice", "SupplierInvoiceItem", "PurchaseReturn", "PurchaseReturnItem",
    "Payment", "PaymentAllocation",
    "WeighbridgeDevice", "WeighingTicket", "WeighingCorrection",
    "Vehicle", "Driver", "Carrier", "Trip", "TripExpense",
    "BusinessSettings", "NumberSequence", "AuditEvent",
]
