"""WTForms definitions.

Only the fields an operator edits are declared here; derived values (totals,
stock quantities, converted units) are computed by the service layer and never
submitted.  Every form is CSRF-protected by Flask-WTF, and numeric fields are
validated with :class:`DecimalField`/``InputRequired`` so that malformed money or
quantity input is rejected rather than silently coerced.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, Optional, ValidationError

from config import WILAYAS
from app.models.security import ALL_ROLES


def _wilaya_choices():
    return [(0, "—")] + [(code, name) for code, name in sorted(WILAYAS.items())]


def percentage_not_a_fraction(form, field):
    """Refuse ``0.19`` in a field whose label asks for a percentage.

    Rates are stored as percentages throughout (``19`` means 19 %): the column
    type, the ``| percent`` filter, the totals arithmetic and the 0–100 range
    all agree on that.  Typing the fraction form is the easiest way to apply a
    rate one hundred times too small, so it is rejected with a message instead
    of being silently "corrected".
    """
    if field.data is None:
        return
    value = Decimal(str(field.data))
    if Decimal("0") < value < Decimal("1"):
        raise ValidationError("settings.vat_is_percentage")


def _unit_choices():
    from sqlalchemy import select

    from app.extensions import db
    from app.models import UnitOfMeasure
    from app.i18n import localized_name

    units = db.session.execute(select(UnitOfMeasure)).scalars().all()
    return [(0, "—")] + [(unit.id, f"{localized_name(unit)} ({unit.code})") for unit in units]


class BaseForm(FlaskForm):
    """FlaskForm whose labels and select choice labels are translated.

    Field and choice labels are written as catalogue keys in the source (which
    keeps them greppable and lets the i18n tests verify they exist).  They are
    resolved to the active language when the form is built, so the rendered
    widget contains no raw key and no English fallback.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._translate_labels()

    def _translate_labels(self) -> None:
        from app.i18n import translate

        try:
            for field in self._fields.values():
                label = getattr(field, "label", None)
                if label is not None and label.text:
                    label.text = translate(label.text)
                if not isinstance(field, SelectField):
                    continue
                choices = field.choices
                if choices is None:
                    continue
                # ``choices`` may be a callable that WTForms resolves lazily.
                resolved = choices() if callable(choices) else choices
                field.choices = [
                    (value, translate(text) if isinstance(text, str) else text)
                    for value, text in resolved
                ]
        except RuntimeError:
            # No application/request context (for example a form built in a
            # unit test): leave the keys in place for the caller to translate.
            return
        except (TypeError, IndexError, KeyError):
            # A malformed or empty choice list must not break form construction.
            return


class LoginForm(BaseForm):
    username = StringField("auth.username", validators=[DataRequired(), Length(max=50)])
    password = PasswordField("auth.password", validators=[DataRequired()])
    remember = BooleanField("auth.remember")
    submit = SubmitField("auth.sign_in")


class ChangePasswordForm(BaseForm):
    current_password = PasswordField("auth.current_password", validators=[DataRequired()])
    new_password = PasswordField("auth.new_password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "auth.confirm_password", validators=[DataRequired(), EqualTo("new_password")]
    )
    submit = SubmitField("common.save")


class PartyForm(BaseForm):
    code = StringField("common.code", validators=[Optional(), Length(max=20)])
    name = StringField("common.name", validators=[DataRequired(), Length(max=160)])
    contact_person = StringField("party.contact_person", validators=[Optional(), Length(max=120)])
    phone = StringField("common.phone", validators=[Optional(), Length(max=40)])
    phone_secondary = StringField("party.phone_secondary", validators=[Optional(), Length(max=40)])
    email = StringField("common.email", validators=[Optional(), Email(), Length(max=120)])
    wilaya_code = SelectField("address.wilaya", coerce=int, choices=_wilaya_choices, default=0)
    commune = StringField("address.commune", validators=[Optional(), Length(max=80)])
    locality = StringField("address.locality", validators=[Optional(), Length(max=80)])
    street = StringField("address.street", validators=[Optional(), Length(max=160)])
    address_details = TextAreaField("address.details", validators=[Optional()])
    tax_id = StringField("party.tax_id", validators=[Optional(), Length(max=40)])
    registry_no = StringField("party.registry_no", validators=[Optional(), Length(max=40)])
    payment_terms_days = IntegerField("party.payment_terms", validators=[Optional(), NumberRange(min=0, max=365)], default=0)
    opening_balance = DecimalField("party.opening_balance", validators=[Optional()], places=2, default=Decimal("0"))
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class CustomerForm(PartyForm):
    customer_type = SelectField(
        "customer.type", choices=[("retail", "customer.retail"), ("contractor", "customer.contractor"),
                                  ("wholesale", "customer.wholesale"), ("public", "customer.public")],
        default="retail",
    )
    credit_limit = DecimalField("customer.credit_limit", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))
    is_blocked = BooleanField("customer.blocked")


class SupplierForm(PartyForm):
    supplied_materials = StringField("supplier.materials", validators=[Optional(), Length(max=200)])


class CategoryForm(BaseForm):
    code = StringField("common.code", validators=[DataRequired(), Length(max=40)])
    name_ar = StringField("common.name_ar", validators=[DataRequired(), Length(max=120)])
    name_fr = StringField("common.name_fr", validators=[DataRequired(), Length(max=120)])
    name_en = StringField("common.name_en", validators=[DataRequired(), Length(max=120)])
    sort_order = IntegerField("common.sort_order", validators=[Optional()], default=0)
    submit = SubmitField("common.save")


class UnitForm(BaseForm):
    code = StringField("common.code", validators=[DataRequired(), Length(max=20)])
    kind = SelectField("unit.kind", choices=[("mass", "unit.mass"), ("volume", "unit.volume"),
                                             ("count", "unit.count"), ("length", "unit.length")])
    factor = DecimalField("unit.factor", validators=[DataRequired(), NumberRange(min=Decimal("0.000001"))], places=6)
    name_ar = StringField("common.name_ar", validators=[DataRequired(), Length(max=60)])
    name_fr = StringField("common.name_fr", validators=[DataRequired(), Length(max=60)])
    name_en = StringField("common.name_en", validators=[DataRequired(), Length(max=60)])
    allows_fraction = BooleanField("unit.allows_fraction", default=True)
    submit = SubmitField("common.save")


class WarehouseForm(BaseForm):
    code = StringField("common.code", validators=[DataRequired(), Length(max=20)])
    name_ar = StringField("common.name_ar", validators=[DataRequired(), Length(max=120)])
    name_fr = StringField("common.name_fr", validators=[DataRequired(), Length(max=120)])
    name_en = StringField("common.name_en", validators=[DataRequired(), Length(max=120)])
    location = StringField("address.location", validators=[Optional(), Length(max=200)])
    wilaya_code = SelectField("address.wilaya", coerce=int, choices=_wilaya_choices, default=0)
    is_default = BooleanField("warehouse.is_default")
    submit = SubmitField("common.save")


class ProductForm(BaseForm):
    sku = StringField("product.sku", validators=[Optional(), Length(max=40)])
    barcode = StringField("product.barcode", validators=[Optional(), Length(max=60)])
    name = StringField("common.name", validators=[DataRequired(), Length(max=160)])
    description = TextAreaField("common.description", validators=[Optional()])
    category_id = SelectField("product.category", coerce=int, choices=[], validate_choice=False)
    brand_id = SelectField("product.brand", coerce=int, choices=[], validate_choice=False)
    default_supplier_id = SelectField("product.default_supplier", coerce=int, choices=[], validate_choice=False)
    stock_unit_id = SelectField("product.stock_unit", coerce=int, choices=_unit_choices, validate_choice=False)
    cost_price = DecimalField("product.cost_price", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))
    sale_price = DecimalField("product.sale_price", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))
    wholesale_price = DecimalField("product.wholesale_price", validators=[Optional(), NumberRange(min=0)], places=2)
    contractor_price = DecimalField("product.contractor_price", validators=[Optional(), NumberRange(min=0)], places=2)
    reorder_level = DecimalField("product.reorder_level", validators=[Optional(), NumberRange(min=0)], places=4, default=Decimal("0"))
    bag_weight_kg = DecimalField("product.bag_weight", validators=[Optional(), NumberRange(min=0)], places=3)
    steel_diameter_mm = DecimalField("product.steel_diameter", validators=[Optional(), NumberRange(min=0)], places=2)
    steel_length_m = DecimalField("product.steel_length", validators=[Optional(), NumberRange(min=0)], places=3)
    steel_grade = StringField("product.steel_grade", validators=[Optional(), Length(max=60)])
    aggregate_grading = StringField("product.grading", validators=[Optional(), Length(max=60)])
    spec_notes = TextAreaField("product.spec_notes", validators=[Optional()])
    is_active = BooleanField("common.active", default=True)
    submit = SubmitField("common.save")


class ConversionForm(BaseForm):
    from_unit_id = SelectField("unit.from", coerce=int, choices=_unit_choices, validate_choice=False)
    to_unit_id = SelectField("unit.to", coerce=int, choices=_unit_choices, validate_choice=False)
    factor = DecimalField("unit.factor", validators=[DataRequired(), NumberRange(min=Decimal("0.000001"))], places=6)
    conversion_type = SelectField(
        "unit.conversion_type",
        choices=[("packaging", "unit.packaging"), ("density", "unit.density"), ("other", "unit.other")],
    )
    effective_from = DateField("unit.effective_from", validators=[DataRequired()], default=date.today)
    source = StringField("unit.source", validators=[Optional(), Length(max=200)])
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class SalesDocumentForm(BaseForm):
    customer_id = SelectField("sale.customer", coerce=int, choices=[], validate_choice=False)
    document_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    valid_until = DateField("sale.valid_until", validators=[Optional()])
    delivery_date = DateField("sale.delivery_date", validators=[Optional()])
    warehouse_id = SelectField("stock.warehouse", coerce=int, choices=[], validate_choice=False)
    price_list = SelectField(
        "sale.price_list",
        choices=[("retail", "price.retail"), ("wholesale", "price.wholesale"), ("contractor", "price.contractor")],
    )
    delivery_address = TextAreaField("sale.delivery_address", validators=[Optional()])
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class PurchaseDocumentForm(BaseForm):
    supplier_id = SelectField("purchase.supplier", coerce=int, choices=[], validate_choice=False)
    document_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    expected_date = DateField("purchase.expected_date", validators=[Optional()])
    warehouse_id = SelectField("stock.warehouse", coerce=int, choices=[], validate_choice=False)
    supplier_reference = StringField("purchase.supplier_reference", validators=[Optional(), Length(max=60)])
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class ReceiptCostsForm(BaseForm):
    transport_cost = DecimalField("purchase.transport_cost", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))
    handling_cost = DecimalField("purchase.handling_cost", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))
    other_cost = DecimalField("purchase.other_cost", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))


class PaymentForm(BaseForm):
    direction = SelectField("payment.direction", choices=[("inbound", "payment.inbound"), ("outbound", "payment.outbound")])
    customer_id = SelectField("sale.customer", coerce=int, choices=[], validate_choice=False)
    supplier_id = SelectField("purchase.supplier", coerce=int, choices=[], validate_choice=False)
    amount = DecimalField("payment.amount", validators=[DataRequired(), NumberRange(min=Decimal("0.01"))], places=2)
    payment_date = DateField("payment.date", validators=[DataRequired()], default=date.today)
    method = SelectField(
        "payment.method",
        choices=[("cash", "payment.cash"), ("bank_transfer", "payment.bank_transfer"),
                 ("cheque", "payment.cheque"), ("card", "payment.card"), ("other", "payment.other")],
    )
    reference = StringField("payment.reference", validators=[Optional(), Length(max=80)])
    notes = TextAreaField("common.notes", validators=[Optional()])
    auto_allocate = BooleanField("payment.auto_allocate", default=True)
    submit = SubmitField("common.save")


class WeighingTicketForm(BaseForm):
    direction = SelectField("weigh.direction", choices=[("inbound", "weigh.inbound"), ("outbound", "weigh.outbound")])
    ticket_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    plate = StringField("weigh.plate", validators=[Optional(), Length(max=30)])
    driver_name = StringField("weigh.driver", validators=[Optional(), Length(max=120)])
    product_id = SelectField("product.title", coerce=int, choices=[], validate_choice=False)
    customer_id = SelectField("sale.customer", coerce=int, choices=[], validate_choice=False)
    supplier_id = SelectField("purchase.supplier", coerce=int, choices=[], validate_choice=False)
    warehouse_id = SelectField("stock.warehouse", coerce=int, choices=[], validate_choice=False)
    device_id = SelectField("weigh.device", coerce=int, choices=[], validate_choice=False)
    gross_weight = DecimalField("weigh.gross", validators=[Optional(), NumberRange(min=0)], places=3)
    tare_weight = DecimalField("weigh.tare", validators=[Optional(), NumberRange(min=0)], places=3)
    expected_quantity = DecimalField("weigh.expected", validators=[Optional(), NumberRange(min=0)], places=3)
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class TripForm(BaseForm):
    trip_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    planned_date = DateField("fleet.planned_date", validators=[Optional()])
    vehicle_id = SelectField("fleet.vehicle", coerce=int, choices=[], validate_choice=False)
    driver_id = SelectField("fleet.driver", coerce=int, choices=[], validate_choice=False)
    carrier_id = SelectField("fleet.carrier", coerce=int, choices=[], validate_choice=False)
    customer_id = SelectField("sale.customer", coerce=int, choices=[], validate_choice=False)
    origin = StringField("fleet.origin", validators=[Optional(), Length(max=160)])
    destination = StringField("fleet.destination", validators=[Optional(), Length(max=160)])
    transport_fee = DecimalField("fleet.transport_fee", validators=[Optional(), NumberRange(min=0)], places=2, default=Decimal("0"))
    carrier_cost = DecimalField("fleet.carrier_cost", validators=[Optional(), NumberRange(min=0)], places=2)
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class TripExpenseForm(BaseForm):
    expense_type = SelectField(
        "fleet.expense_type",
        choices=[("fuel", "fleet.fuel"), ("toll", "fleet.toll"), ("repair", "fleet.repair"),
                 ("driver_allowance", "fleet.driver_allowance"), ("carrier_fee", "fleet.carrier_fee"),
                 ("other", "fleet.other")],
    )
    amount = DecimalField("payment.amount", validators=[DataRequired(), NumberRange(min=Decimal("0.01"))], places=2)
    expense_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    paid_by = StringField("fleet.paid_by", validators=[Optional(), Length(max=120)])
    note = StringField("common.notes", validators=[Optional(), Length(max=255)])
    submit = SubmitField("common.save")


class VehicleForm(BaseForm):
    plate = StringField("fleet.plate", validators=[DataRequired(), Length(max=30)])
    label = StringField("common.name", validators=[Optional(), Length(max=80)])
    vehicle_type = StringField("fleet.vehicle_type", validators=[Optional(), Length(max=60)])
    capacity_quantity = DecimalField("fleet.capacity", validators=[Optional(), NumberRange(min=0)], places=4)
    capacity_unit_id = SelectField("product.stock_unit", coerce=int, choices=_unit_choices, validate_choice=False)
    tare_weight_kg = DecimalField("fleet.tare", validators=[Optional(), NumberRange(min=0)], places=3)
    odometer_km = DecimalField("fleet.odometer", validators=[Optional(), NumberRange(min=0)], places=3)
    insurance_expiry = DateField("fleet.insurance_expiry", validators=[Optional()])
    inspection_expiry = DateField("fleet.inspection_expiry", validators=[Optional()])
    default_driver_id = SelectField("fleet.driver", coerce=int, choices=[], validate_choice=False)
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class DriverForm(BaseForm):
    name = StringField("common.name", validators=[DataRequired(), Length(max=120)])
    phone = StringField("common.phone", validators=[Optional(), Length(max=40)])
    licence_no = StringField("fleet.licence_no", validators=[Optional(), Length(max=60)])
    licence_expiry = DateField("fleet.licence_expiry", validators=[Optional()])
    licence_category = StringField("fleet.licence_category", validators=[Optional(), Length(max=20)])
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class CarrierForm(BaseForm):
    name = StringField("common.name", validators=[DataRequired(), Length(max=160)])
    contact_person = StringField("party.contact_person", validators=[Optional(), Length(max=120)])
    phone = StringField("common.phone", validators=[Optional(), Length(max=40)])
    tax_id = StringField("party.tax_id", validators=[Optional(), Length(max=40)])
    default_rate = DecimalField("carrier.default_rate", validators=[Optional(), NumberRange(min=0)], places=2)
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class AdjustmentForm(BaseForm):
    adjustment_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    warehouse_id = SelectField("stock.warehouse", coerce=int, choices=[], validate_choice=False)
    justification = TextAreaField("stock.justification", validators=[DataRequired()])
    submit = SubmitField("common.save")


class TransferForm(BaseForm):
    transfer_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    source_warehouse_id = SelectField("stock.source", coerce=int, choices=[], validate_choice=False)
    destination_warehouse_id = SelectField("stock.destination", coerce=int, choices=[], validate_choice=False)
    note = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class CountForm(BaseForm):
    count_date = DateField("common.date", validators=[DataRequired()], default=date.today)
    warehouse_id = SelectField("stock.warehouse", coerce=int, choices=[], validate_choice=False)
    note = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")


class UserForm(BaseForm):
    username = StringField("auth.username", validators=[DataRequired(), Length(max=50)])
    full_name = StringField("common.name", validators=[Optional(), Length(max=120)])
    role = SelectField("users.role", choices=[(role, f"role.{role}") for role in sorted(ALL_ROLES)])
    phone = StringField("common.phone", validators=[Optional(), Length(max=30)])
    language = SelectField("common.language", choices=[("", "—"), ("ar", "العربية"), ("fr", "Français"), ("en", "English")])
    is_active = BooleanField("common.active", default=True)
    password = PasswordField("auth.password", validators=[Optional(), Length(min=8)])
    submit = SubmitField("common.save")


class SettingsForm(BaseForm):
    business_name = StringField("settings.business_name", validators=[DataRequired(), Length(max=160)])
    legal_name = StringField("settings.legal_name", validators=[Optional(), Length(max=160)])
    tax_id = StringField("party.tax_id", validators=[Optional(), Length(max=40)])
    registry_no = StringField("party.registry_no", validators=[Optional(), Length(max=40)])
    activity = StringField("settings.activity", validators=[Optional(), Length(max=160)])
    phone = StringField("common.phone", validators=[Optional(), Length(max=40)])
    email = StringField("common.email", validators=[Optional(), Email(), Length(max=120)])
    street = StringField("address.street", validators=[Optional(), Length(max=160)])
    commune = StringField("address.commune", validators=[Optional(), Length(max=80)])
    locality = StringField("address.locality", validators=[Optional(), Length(max=80)])
    wilaya_code = SelectField("address.wilaya", coerce=int, choices=_wilaya_choices, default=0)
    currency = StringField("settings.currency", validators=[DataRequired(), Length(max=8)])
    currency_symbol = StringField("settings.currency_symbol", validators=[DataRequired(), Length(max=8)])
    timezone = StringField("settings.timezone", validators=[DataRequired(), Length(max=64)])
    default_language = SelectField("common.language", choices=[("ar", "العربية"), ("fr", "Français"), ("en", "English")])
    vat_rate = DecimalField("settings.vat_rate", validators=[DataRequired(), NumberRange(min=0, max=100), percentage_not_a_fraction], places=2)
    prices_include_tax = BooleanField("settings.prices_include_tax")
    default_payment_terms_days = IntegerField("settings.payment_terms", validators=[Optional(), NumberRange(min=0, max=365)], default=0)
    weigh_tolerance_percent = DecimalField("settings.weigh_tolerance", validators=[Optional(), NumberRange(min=0, max=100), percentage_not_a_fraction], places=2, default=Decimal("2"))
    allow_negative_stock = BooleanField("settings.allow_negative_stock")
    enable_weighbridge = BooleanField("settings.enable_weighbridge")
    enable_fleet = BooleanField("settings.enable_fleet")
    enable_accounting = BooleanField("settings.enable_accounting")
    default_warehouse_id = SelectField("stock.default_warehouse", coerce=int, choices=[], validate_choice=False)
    bank_details = TextAreaField("settings.bank_details", validators=[Optional()])
    invoice_footer = TextAreaField("settings.invoice_footer", validators=[Optional()])
    submit = SubmitField("common.save")


class WeighbridgeDeviceForm(BaseForm):
    code = StringField("common.code", validators=[DataRequired(), Length(max=20)])
    name = StringField("common.name", validators=[DataRequired(), Length(max=120)])
    protocol = SelectField("weigh.protocol", choices=[("manual", "weigh.manual"), ("serial", "weigh.serial"), ("tcp", "weigh.tcp")])
    serial_number = StringField("weigh.serial_number", validators=[Optional(), Length(max=60)])
    connection_config = StringField("weigh.connection", validators=[Optional(), Length(max=200)])
    capacity_kg = DecimalField("weigh.capacity", validators=[Optional(), NumberRange(min=0)], places=3)
    division_kg = DecimalField("weigh.division", validators=[Optional(), NumberRange(min=0)], places=3)
    calibration_reference = StringField("weigh.calibration_reference", validators=[Optional(), Length(max=80)])
    calibration_valid_until = DateField("weigh.calibration_valid_until", validators=[Optional()])
    notes = TextAreaField("common.notes", validators=[Optional()])
    submit = SubmitField("common.save")
