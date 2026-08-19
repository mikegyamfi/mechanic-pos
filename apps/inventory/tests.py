"""
Shipment / batch reconciliation tests.

The rule under test: a batch keeps its own true cost, the product's cost_price
is the weighted average of the batches on hand, and a manually-set selling
price is never silently overwritten by a container.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.location.models import Location
from apps.products.models import Category, PriceChangeLog, Product
from apps.inventory.models import Shipment, ShipmentItem, StockBatch

User = get_user_model()


class ShipmentReceiptTests(TestCase):

    def setUp(self):
        self.shop = Location.objects.create(
            name='Warehouse', location_type=Location.LocationType.WAREHOUSE, address='Spintex',
        )
        self.manager = User.objects.create_user(
            username='boss', password='x', role=User.Role.OWNER, assigned_location=self.shop,
        )
        self.category = Category.objects.create(name='Head Lamp', slug='head-lamp')

        self.lamp = Product.objects.create(
            name='Hilux Head Lamp', slug='hilux-head-lamp', sku='HL-HLX',
            category=self.category, cost_price=Decimal('0.00'), selling_price=Decimal('0.00'),
            is_sold_in_pairs=True, split_price_percentage=Decimal('60.00'),
        )

        self.shipment = Shipment.objects.create(
            reference_number='LSA20251018', supplier_name='Lian Sheng (Xiamen)',
            exchange_rate=Decimal('13.0000'), total_freight_usd=Decimal('1000.00'),
            total_cbm=Decimal('20.0000'),
        )

    def _add_item(self, quantity=10, unit_cost_usd='20.00', sale_price='800.00', **kwargs):
        return ShipmentItem.objects.create(
            shipment=self.shipment, product=self.lamp, quantity=quantity,
            unit_cost_usd=Decimal(unit_cost_usd), total_line_cbm=Decimal('2.0000'),
            outside_sale_price_ghs=Decimal(sale_price), **kwargs
        )

    def test_receipt_creates_a_batch_at_the_true_material_cost(self):
        self._add_item()
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        batch = StockBatch.objects.get(product=self.lamp)
        self.assertEqual(batch.quantity, 10)
        self.assertEqual(batch.initial_quantity, 10)
        self.assertEqual(batch.cost_price, Decimal('260.00'))    # 20 USD x 13
        self.assertEqual(batch.batch_number, 'LSA20251018')

    def test_receipt_sets_the_price_and_the_split_follows_it(self):
        self._add_item(sale_price='800.00')
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.selling_price, Decimal('800.00'))
        # 60% of the pair price, computed live -- not a stored half-price.
        self.assertEqual(self.lamp.effective_single_price, Decimal('480.00'))

    def test_product_cost_is_the_weighted_average_of_batches_on_hand(self):
        # First container: 40 pieces at 250 (raise the rate to hit the number).
        self._add_item(quantity=40, unit_cost_usd='25.00')
        self.shipment.exchange_rate = Decimal('10.0000')
        self.shipment.save()
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.cost_price, Decimal('250.00'))

        # Second container: 10 pieces at 310.
        second = Shipment.objects.create(
            reference_number='LSA20251122', supplier_name='Lian Sheng (Xiamen)',
            exchange_rate=Decimal('10.0000'), total_freight_usd=Decimal('0.00'),
            total_cbm=Decimal('1.0000'),
        )
        ShipmentItem.objects.create(
            shipment=second, product=self.lamp, quantity=10, unit_cost_usd=Decimal('31.00'),
            total_line_cbm=Decimal('1.0000'), outside_sale_price_ghs=Decimal('800.00'),
        )
        second.receive_into_stock(location=self.shop, received_by=self.manager)

        # (40 x 250 + 10 x 310) / 50 = 262.00
        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.cost_price, Decimal('262.00'))
        # ...while each batch keeps its own real cost.
        costs = sorted(StockBatch.objects.filter(product=self.lamp).values_list('cost_price', flat=True))
        self.assertEqual(costs, [Decimal('250.00'), Decimal('310.00')])

    def test_a_manual_price_is_not_overwritten_but_suggested(self):
        # A manager prices it by hand first.
        self.lamp.apply_price(Decimal('950.00'), source=PriceChangeLog.Source.MANUAL,
                              changed_by=self.manager)
        self.lamp.refresh_from_db()
        self.assertTrue(self.lamp.price_is_manual)

        self._add_item(sale_price='800.00')
        summary = self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.selling_price, Decimal('950.00'))          # untouched
        self.assertEqual(self.lamp.suggested_selling_price, Decimal('800.00'))  # parked
        self.assertEqual(summary['prices_suggested'], 1)
        self.assertEqual(summary['prices_applied'], 0)

        # The suggestion is in the audit log, marked as not applied.
        log = PriceChangeLog.objects.filter(was_applied=False).get()
        self.assertEqual(log.new_value, Decimal('800.00'))
        self.assertEqual(log.source, PriceChangeLog.Source.SHIPMENT)

    def test_accepting_a_suggestion_applies_it_and_clears_it(self):
        self.lamp.apply_price(Decimal('950.00'), source=PriceChangeLog.Source.MANUAL)
        self._add_item(sale_price='800.00')
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)
        self.lamp.refresh_from_db()

        self.lamp.apply_price(self.lamp.suggested_selling_price,
                              source=PriceChangeLog.Source.SUGGESTION_ACCEPTED,
                              changed_by=self.manager)
        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.selling_price, Decimal('800.00'))
        self.assertIsNone(self.lamp.suggested_selling_price)

    def test_a_line_can_be_received_as_stock_only(self):
        self._add_item(sale_price='800.00', update_selling_price=False)
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.selling_price, Decimal('0.00'))   # price untouched
        self.assertEqual(StockBatch.objects.filter(product=self.lamp).count(), 1)

    def test_a_shipment_cannot_be_received_twice(self):
        self._add_item()
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        with self.assertRaises(ValueError) as ctx:
            self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)
        self.assertIn('already been received', str(ctx.exception))
        self.assertEqual(StockBatch.objects.filter(product=self.lamp).count(), 1)

    def test_receipt_records_who_where_and_when(self):
        self._add_item()
        self.shipment.receive_into_stock(location=self.shop, received_by=self.manager)

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'RECEIVED')
        self.assertEqual(self.shipment.received_by, self.manager)
        self.assertEqual(self.shipment.received_location, self.shop)
        self.assertIsNotNone(self.shipment.received_at)

    def test_container_level_economics(self):
        self._add_item(quantity=10, unit_cost_usd='20.00', sale_price='800.00')

        # 10 pieces of a paired part = 5 sellable pairs at 800 = 4,000 revenue.
        item = self.shipment.items.get()
        self.assertEqual(item.billable_units, 5)
        self.assertEqual(item.projected_line_revenue_ghs, Decimal('4000.00'))
        self.assertEqual(item.total_material_cost_ghs, Decimal('2600.00'))
        self.assertEqual(item.projected_line_profit_ghs, Decimal('1400.00'))

        # Freight is tracked at container level but deliberately excluded from
        # the per-unit landed cost (see the comment in total_landed_cost_ghs).
        self.assertEqual(self.shipment.total_material_cost_ghs, Decimal('2600.00'))
        self.assertEqual(self.shipment.total_freight_cost_ghs, Decimal('13000.00'))
        self.assertEqual(item.unit_landed_cost_ghs, Decimal('260.00'))

    def test_odd_piece_count_still_leaves_a_single_to_sell(self):
        self._add_item(quantity=7)
        self.assertEqual(self.shipment.items.get().billable_units, 4)   # 3 pairs + 1 single


class BatchTests(TestCase):

    def setUp(self):
        self.shop = Location.objects.create(name='Shop', address='x')
        self.category = Category.objects.create(name='Filters', slug='filters')
        self.product = Product.objects.create(
            name='Air Filter', slug='air-filter', sku='AF-1', category=self.category,
            cost_price=Decimal('30.00'), selling_price=Decimal('60.00'),
        )

    def test_initial_quantity_is_captured_once_and_tracks_what_sold(self):
        batch = StockBatch.objects.create(
            product=self.product, location=self.shop, quantity=20, cost_price=Decimal('30.00'),
        )
        self.assertEqual(batch.initial_quantity, 20)

        batch.quantity = 8
        batch.save()
        batch.refresh_from_db()
        self.assertEqual(batch.initial_quantity, 20)
        self.assertEqual(batch.quantity_sold, 12)
        self.assertEqual(batch.stock_value, Decimal('240.00'))

    def test_cost_resync_ignores_empty_batches(self):
        StockBatch.objects.create(
            product=self.product, location=self.shop, quantity=0, cost_price=Decimal('1000.00'),
        )
        StockBatch.objects.create(
            product=self.product, location=self.shop, quantity=5, cost_price=Decimal('40.00'),
        )
        self.product.resync_cost_price()
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, Decimal('40.00'))

    def test_resync_keeps_the_last_known_cost_when_stock_runs_out(self):
        StockBatch.objects.create(
            product=self.product, location=self.shop, quantity=0, cost_price=Decimal('45.00'),
        )
        self.product.resync_cost_price()
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, Decimal('30.00'))   # unchanged, not zeroed
