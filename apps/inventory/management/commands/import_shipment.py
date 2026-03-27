import openpyxl
import time
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from apps.products.models import Product, Category
from apps.inventory.models import Shipment, ShipmentItem


# ==============================================================================
# WEB DASHBOARD FUNCTIONS (Used by apps/inventory/views.py)
# ==============================================================================

def parse_excel_to_preview(uploaded_file):
    """
    Reads an uploaded Excel file (.xlsx),
    and returns a JSON-friendly list of dictionaries for the Web Preview.
    """
    try:
        # data_only=True reads the calculated values of formulas instead of the formula string itself
        wb = openpyxl.load_workbook(uploaded_file, data_only=True)
        sheet = wb.active
    except Exception as e:
        raise ValueError(f"Failed to read Excel file. Please ensure it is a valid .xlsx format. Error: {str(e)}")

    items_to_import = []
    header_found = False
    invoice_number = ""

    for row in sheet.iter_rows(values_only=True):
        # If the row is completely empty, skip it
        if not row or not any(row):
            continue

        # Safely convert all cells to strings (and handle None/empty cells)
        safe_row = [str(cell).strip() if cell is not None else "" for cell in row]

        # Look for Invoice Number before header is found
        if not header_found and not invoice_number:
            for i, cell_val in enumerate(safe_row):
                # E.g. "NO." in one cell, "LSA20251018" in the next
                if cell_val.upper() == 'NO.' and i + 1 < len(safe_row) and safe_row[i + 1]:
                    invoice_number = safe_row[i + 1]
                    break
                # E.g. "NO. LSA20251018" all in one cell
                elif cell_val.upper().startswith('NO.') and len(cell_val) > 3:
                    invoice_number = cell_val[3:].strip()
                    break

        # Need at least 5 columns to be a valid data row for our logic
        if len(safe_row) < 5:
            continue

        # Look for the Header Row to start processing
        if "PART NO" in safe_row[1].upper():
            header_found = True
            continue

        if not header_found:
            continue

        sku = safe_row[1]
        description = safe_row[2]

        # Skip category dividers
        if not sku:
            continue

        try:
            # Using float() first prevents errors if Excel stores the number as a decimal like "10.0"
            pieces = int(float(safe_row[4])) if len(safe_row) > 4 and safe_row[4] else 0
        except ValueError:
            pieces = 0

        if pieces <= 0:
            continue

        # --- Intelligent Category Assignment ---
        desc_lower = description.lower()
        if 'head' in desc_lower:
            category_name = "Head Lamp"
        elif 'tail' in desc_lower:
            category_name = "Tail Lamp"
        elif 'fog' in desc_lower:
            category_name = "Fog Lamp"
        else:
            category_name = "Light"

        # --- EVEN / ODD LOGIC FOR PAIRS ---
        # As requested: Ignores the empty 'set' column and looks strictly at the Pieces.
        # If Pieces is an even number (like 10 or 20), it is a Pair.
        # If it is odd (like 5), it is NOT a Pair.
        is_pair = (pieces % 2 == 0)

        # --- Parse extended columns from Price List 3 ---
        try:
            line_cbm = Decimal(safe_row[8]) if len(safe_row) > 8 and safe_row[8] else Decimal('0.00')
        except Exception:
            line_cbm = Decimal('0.00')

        try:
            unit_cost_usd = Decimal(safe_row[9]) if len(safe_row) > 9 and safe_row[9] else Decimal('0.00')
        except Exception:
            unit_cost_usd = Decimal('0.00')

        try:
            outside_sale_str = safe_row[12] if len(safe_row) > 12 else ''
            outside_sale = Decimal(outside_sale_str) if outside_sale_str else Decimal('0.00')
        except Exception:
            outside_sale = Decimal('0.00')

        # Check Database to see if this is a NEW or EXISTING product
        exists = Product.objects.filter(sku=sku).exists()
        status = 'EXISTS' if exists else 'NEW'

        items_to_import.append({
            'invoice_number': invoice_number,
            'sku': sku,
            'description': description,
            'category': category_name,
            'pieces': pieces,
            'is_pair': is_pair,
            'unit_cost_usd': str(unit_cost_usd),
            'line_cbm': str(line_cbm),
            'outside_sale': str(outside_sale),
            'status': status
        })

    return items_to_import


@transaction.atomic
def process_confirmed_import(items, supplier_name, exchange_rate, total_freight_usd, total_cbm):
    """
    Takes the confirmed JSON preview and safely writes everything to the database.
    """
    # Extract the invoice number we parsed earlier, fallback to a timestamp if none was found
    invoice_number = items[0].get('invoice_number') if items else None
    if not invoice_number:
        invoice_number = f"INV-{int(time.time())}"

    # 1. Setup Base Categories
    cat_light, _ = Category.objects.get_or_create(name="Light")
    categories_map = {
        "Head Lamp": Category.objects.get_or_create(name="Head Lamp", parent=cat_light)[0],
        "Tail Lamp": Category.objects.get_or_create(name="Tail Lamp", parent=cat_light)[0],
        "Fog Lamp": Category.objects.get_or_create(name="Fog Lamp", parent=cat_light)[0],
        "Light": cat_light
    }

    # 2. Create the Shipment with the Extracted Invoice Number
    shipment = Shipment.objects.create(
        reference_number=invoice_number,
        supplier_name=supplier_name,
        total_freight_usd=Decimal(str(total_freight_usd)),
        total_cbm=Decimal(str(total_cbm)),
        exchange_rate=Decimal(str(exchange_rate)),
        status='PENDING'
    )

    products_created = 0
    items_added = 0

    # 3. Process Each Item
    for item in items:
        product_category = categories_map.get(item['category'], cat_light)
        outside_sale = Decimal(str(item['outside_sale']))

        product, created = Product.objects.get_or_create(
            sku=item['sku'],
            defaults={
                'name': item['description'],
                'category': product_category,
                'selling_price': outside_sale,
                'cost_price': Decimal('0.00'),
                'is_sold_in_pairs': item['is_pair']
            }
        )

        if created:
            products_created += 1
        else:
            if outside_sale > 0:
                product.selling_price = outside_sale
            product.category = product_category
            product.is_sold_in_pairs = item['is_pair']
            product.save()

        ShipmentItem.objects.create(
            shipment=shipment,
            product=product,
            quantity=item['pieces'],
            unit_cost_usd=Decimal(str(item['unit_cost_usd'])),
            total_line_cbm=Decimal(str(item['line_cbm'])),
            outside_sale_price_ghs=outside_sale
        )
        items_added += 1

    return shipment, products_created, items_added


# ==============================================================================
# COMMAND LINE TOOL (Fallback / Terminal Usage)
# ==============================================================================

class Command(BaseCommand):
    help = 'Imports products and a shipment using the shared preview logic.'

    def add_arguments(self, parser):
        parser.add_argument('excel_file', type=str, help='Path to the XLSX file')
        parser.add_argument('--supplier', type=str, default="Lian Sheng (Xiamen)", help='Supplier Name')
        parser.add_argument('--rate', type=str, default="13.00", help='Exchange Rate')
        parser.add_argument('--freight', type=str, default="0.00", help='Total Freight Cost (USD)')
        parser.add_argument('--cbm', type=str, default="0.00", help='Total CBM')

    def handle(self, *args, **kwargs):
        file_path = kwargs['excel_file']

        self.stdout.write(self.style.SUCCESS(f"Reading file: {file_path}..."))

        # Open the file in binary mode so it mimics a web upload
        try:
            with open(file_path, 'rb') as f:
                items = parse_excel_to_preview(f)
        except Exception as e:
            raise CommandError(str(e))

        self.stdout.write(self.style.SUCCESS(f"File parsed! Found {len(items)} items. Saving to DB..."))

        shipment, p_created, i_added = process_confirmed_import(
            items=items,
            supplier_name=kwargs['supplier'],
            exchange_rate=kwargs['rate'],
            total_freight_usd=kwargs['freight'],
            total_cbm=kwargs['cbm']
        )

        self.stdout.write(self.style.SUCCESS(f"Import Complete!"))
        self.stdout.write(self.style.SUCCESS(f"- Shipment Ref: {shipment.reference_number}"))
        self.stdout.write(self.style.SUCCESS(f"- New Products Created: {p_created}"))
        self.stdout.write(self.style.SUCCESS(f"- Items added to Shipment: {i_added}"))