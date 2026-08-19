"""
Money tests.

These exist to make the invariants in services.py fail loudly if anyone
loosens them: pair rounding, oversell, credit limits, change out of cash,
refund reversal, and drawer reconciliation.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.money import allocate, q2
from apps.customers.models import Customer
from apps.finance.models import Expense, ExpenseCategory
from apps.inventory.models import StockBatch, Supplier
from apps.location.models import Location
from apps.products.models import Category, Product, Promotion
from apps.sales import services
from apps.sales.models import RegisterSession, Sale, SaleItem, SalePayment
from apps.sales.services import SaleError

User = get_user_model()


class MoneyBase(TestCase):
    """A shop, a cashier, a manager, and a paired part with two batches."""

    def setUp(self):
        self.shop = Location.objects.create(
            name='Abossey Okai Shop', location_type=Location.LocationType.SHOP,
            address='Kaneshie Road', min_margin_percentage=Decimal('10.00'),
        )
        self.cashier = User.objects.create_user(
            username='ama', password='x', role=User.Role.CASHIER, assigned_location=self.shop,
        )
        self.manager = User.objects.create_user(
            username='kojo', password='x', role=User.Role.MANAGER, assigned_location=self.shop,
        )
        self.category = Category.objects.create(name='Head Lamp', slug='head-lamp')
        self.supplier = Supplier.objects.create(name='Lian Sheng (Xiamen)')

        # A paired part: pair sells for 1200, so a single is 60% = 720.
        self.lamp = Product.objects.create(
            name='Corolla 2015 Head Lamp', slug='corolla-2015-head-lamp', sku='HL-COR-15',
            category=self.category, cost_price=Decimal('250.00'), selling_price=Decimal('1200.00'),
            wholesale_price=Decimal('1050.00'), is_sold_in_pairs=True,
            split_price_percentage=Decimal('60.00'), low_stock_threshold=4,
        )
        # A normal part.
        self.filter = Product.objects.create(
            name='Oil Filter', slug='oil-filter', sku='OF-001', category=self.category,
            cost_price=Decimal('20.00'), selling_price=Decimal('45.00'),
            low_stock_threshold=5,
        )

        self.batch_a = StockBatch.objects.create(
            product=self.lamp, location=self.shop, quantity=4, initial_quantity=4,
            cost_price=Decimal('250.00'), supplier=self.supplier, batch_number='LSA-1018',
        )
        self.batch_b = StockBatch.objects.create(
            product=self.lamp, location=self.shop, quantity=2, initial_quantity=2,
            cost_price=Decimal('310.00'), supplier=self.supplier, batch_number='LSA-1122',
        )
        StockBatch.objects.create(
            product=self.filter, location=self.shop, quantity=10, initial_quantity=10,
            cost_price=Decimal('20.00'), batch_number='LSA-1018',
        )

        self.session = RegisterSession.objects.create(
            user=self.cashier, location=self.shop, opening_balance=Decimal('100.00'),
        )
        self.manager_session = RegisterSession.objects.create(
            user=self.manager, location=self.shop, opening_balance=Decimal('0.00'),
        )

    def sell(self, cart, payments, **kwargs):
        kwargs.setdefault('user', self.cashier)
        return services.create_sale(cart=cart, payments=payments, **kwargs)


class SplitPricingTests(MoneyBase):

    def test_single_price_is_a_live_percentage_of_the_pair_price(self):
        self.assertEqual(self.lamp.effective_single_price, Decimal('720.00'))

        # Raise the pair price -- the split price must follow with no re-typing.
        self.lamp.selling_price = Decimal('1400.00')
        self.assertEqual(self.lamp.effective_single_price, Decimal('840.00'))

    def test_split_percentage_applies_to_wholesale_too(self):
        self.assertEqual(self.lamp.effective_single_wholesale_price, Decimal('630.00'))
        self.assertEqual(
            self.lamp.base_price(Product.SellMode.SINGLE, wholesale=True), Decimal('630.00')
        )

    def test_fixed_mode_pins_the_price(self):
        self.lamp.split_price_mode = Product.SplitPriceMode.FIXED
        self.lamp.single_piece_price = Decimal('700.00')
        self.assertEqual(self.lamp.effective_single_price, Decimal('700.00'))

        self.lamp.selling_price = Decimal('1400.00')
        self.assertEqual(self.lamp.effective_single_price, Decimal('700.00'))

    def test_pair_cost_is_doubled_for_margin_comparison(self):
        self.assertEqual(self.lamp.unit_cost(Product.SellMode.PAIR), Decimal('500.00'))
        self.assertEqual(self.lamp.unit_cost(Product.SellMode.SINGLE), Decimal('250.00'))

    def test_price_floor_is_cost_plus_shop_margin(self):
        # Pair: 250 x 2 = 500 landed, +10% = 550
        self.assertEqual(self.lamp.price_floor(Product.SellMode.PAIR, self.shop), Decimal('550.00'))
        self.assertEqual(self.lamp.price_floor(Product.SellMode.SINGLE, self.shop), Decimal('275.00'))

    def test_product_level_margin_overrides_the_shop(self):
        self.lamp.min_margin_percentage = Decimal('50.00')
        self.assertEqual(self.lamp.price_floor(Product.SellMode.PAIR, self.shop), Decimal('750.00'))

    def test_odd_percentage_rounds_to_the_pesewa_and_never_drifts(self):
        self.lamp.split_price_percentage = Decimal('33.33')
        self.lamp.selling_price = Decimal('100.01')
        # 100.01 * 33.33% = 33.333333 -> 33.33
        self.assertEqual(self.lamp.effective_single_price, Decimal('33.33'))


class AllocationTests(TestCase):

    def test_allocate_never_loses_a_pesewa(self):
        parts = allocate(Decimal('100.01'), [1, 1])
        self.assertEqual(sum(parts), Decimal('100.01'))
        self.assertEqual(parts, [Decimal('50.01'), Decimal('50.00')])

    def test_allocate_across_three_units(self):
        parts = allocate(Decimal('10.00'), [1, 1, 1])
        self.assertEqual(sum(parts), Decimal('10.00'))


class SaleTests(MoneyBase):

    def test_pair_sale_deducts_two_pieces_and_books_exact_batch_cost(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 2, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '2400.00'}],
        )

        self.assertEqual(sale.total_amount, Decimal('2400.00'))
        item = sale.items.get()
        self.assertEqual(item.quantity, 2)          # 2 pairs billed
        self.assertEqual(item.pieces_per_unit, 2)
        self.assertEqual(item.pieces, 4)            # 4 pieces off the shelf

        # All 4 pieces came from the cheaper batch (FIFO), so COGS is 4 x 250.
        self.batch_a.refresh_from_db()
        self.batch_b.refresh_from_db()
        self.assertEqual(self.batch_a.quantity, 0)
        self.assertEqual(self.batch_b.quantity, 2)
        self.assertEqual(item.total_cost, Decimal('1000.00'))
        self.assertEqual(sale.total_cost, Decimal('1000.00'))
        self.assertEqual(sale.gross_profit, Decimal('1400.00'))

    def test_pair_straddling_two_batches_costs_both_batches_correctly(self):
        # 3 pairs = 6 pieces: 4 from batch A @250, 2 from batch B @310.
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 3, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '3600.00'}],
        )
        item = sale.items.get()
        self.assertEqual(item.total_cost, Decimal('1620.00'))  # 4*250 + 2*310
        self.assertEqual(item.batch_lines.count(), 2)
        self.assertEqual(sum(bl.pieces for bl in item.batch_lines.all()), 6)

    def test_single_sale_takes_one_piece_at_the_split_price(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'SINGLE'}],
            payments=[{'method': 'CASH', 'amount': '720.00'}],
        )
        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal('720.00'))
        self.assertEqual(item.pieces, 1)
        self.assertEqual(item.total_cost, Decimal('250.00'))
        self.batch_a.refresh_from_db()
        self.assertEqual(self.batch_a.quantity, 3)

    def test_client_cannot_dictate_the_total(self):
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],
                payments=[{'method': 'CASH', 'amount': '1.00'}],
                expected_total='1.00',      # a tampered payload
            )
        self.assertEqual(ctx.exception.code, 'TOTAL_MISMATCH')
        self.assertEqual(Sale.objects.count(), 0)

    def test_correct_expected_total_passes(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '1200.00'}],
            expected_total='1200.00',
        )
        self.assertEqual(sale.total_amount, Decimal('1200.00'))

    def test_cannot_sell_stock_that_does_not_exist(self):
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.lamp.id, 'qty': 5, 'sellMode': 'PAIR'}],   # needs 10 pieces, 6 on hand
                payments=[{'method': 'CASH', 'amount': '6000.00'}],
            )
        self.assertEqual(ctx.exception.code, 'INSUFFICIENT_STOCK')
        self.assertEqual(Sale.objects.count(), 0)
        self.batch_a.refresh_from_db()
        self.assertEqual(self.batch_a.quantity, 4)   # nothing was touched

    def test_two_lines_of_the_same_part_share_one_stock_pool(self):
        # 2 pairs (4 pieces) + 3 singles (3 pieces) = 7 pieces, only 6 exist.
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[
                    {'id': self.lamp.id, 'qty': 2, 'sellMode': 'PAIR'},
                    {'id': self.lamp.id, 'qty': 3, 'sellMode': 'SINGLE'},
                ],
                payments=[{'method': 'CASH', 'amount': '4560.00'}],
            )
        self.assertEqual(ctx.exception.code, 'INSUFFICIENT_STOCK')

    def test_manager_can_record_found_stock_but_it_is_logged(self):
        from apps.inventory.models import StockAdjustment

        sale = services.create_sale(
            user=self.manager,
            cart=[{'id': self.lamp.id, 'qty': 4, 'sellMode': 'PAIR'}],   # 8 pieces vs 6 on hand
            payments=[{'method': 'CASH', 'amount': '4800.00'}],
            allow_stock_correction=True,
        )
        self.assertEqual(sale.items.get().pieces, 8)
        correction = StockAdjustment.objects.get(reason=StockAdjustment.Reason.COUNT_CORRECTION)
        self.assertEqual(correction.adjusted_quantity, 2)
        self.assertEqual(correction.performed_by, self.manager)

    def test_cashier_cannot_sell_below_the_margin_floor(self):
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '520.00'}],
                payments=[{'method': 'CASH', 'amount': '520.00'}],
            )
        self.assertEqual(ctx.exception.code, 'BELOW_FLOOR')

    def test_nobody_can_sell_below_the_floor_at_the_till(self):
        """The floor is absolute -- manager and owner included. Only a promotion lowers it."""
        for user in (self.cashier, self.manager):
            with self.subTest(role=user.role):
                with self.assertRaises(SaleError) as ctx:
                    services.create_sale(
                        user=user,
                        cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '520.00'}],
                        payments=[{'method': 'CASH', 'amount': '520.00'}],
                    )
                self.assertEqual(ctx.exception.code, 'BELOW_FLOOR')
                self.assertIn('promotion', ctx.exception.message.lower())
        self.assertEqual(Sale.objects.count(), 0)

    def test_a_reason_string_cannot_buy_a_way_past_the_floor(self):
        with self.assertRaises(SaleError) as ctx:
            services.create_sale(
                user=self.manager,
                cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '520.00',
                       'overrideReason': 'Cracked lens, clearing old stock'}],
                payments=[{'method': 'CASH', 'amount': '520.00'}],
            )
        self.assertEqual(ctx.exception.code, 'BELOW_FLOOR')
        self.assertEqual(Sale.objects.count(), 0)

    def test_selling_exactly_at_the_floor_is_allowed(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '550.00'}],
            payments=[{'method': 'CASH', 'amount': '550.00'}],
        )
        self.assertEqual(sale.total_amount, Decimal('550.00'))
        self.assertFalse(sale.items.get().below_floor)

    def test_haggled_price_records_the_discount_against_list(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '1100.00'}],
            payments=[{'method': 'CASH', 'amount': '1100.00'}],
        )
        item = sale.items.get()
        self.assertEqual(item.list_price, Decimal('1200.00'))
        self.assertEqual(item.unit_price, Decimal('1100.00'))
        self.assertEqual(item.discount_amount, Decimal('100.00'))
        self.assertEqual(sale.discount_amount, Decimal('100.00'))

    def test_change_cannot_exceed_the_cash_that_was_tendered(self):
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1}],                       # 45.00
                payments=[{'method': 'MOMO', 'amount': '100.00'}],             # overpaid by MoMo
            )
        self.assertEqual(ctx.exception.code, 'CHANGE_EXCEEDS_CASH')

    def test_change_is_recorded_as_a_negative_cash_row(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 1}],
            payments=[{'method': 'CASH', 'amount': '50.00'}],
        )
        self.assertEqual(sale.change_due, Decimal('5.00'))
        self.assertEqual(sale.amount_paid, Decimal('45.00'))
        self.assertTrue(sale.is_fully_paid)

        change_row = sale.payments.get(entry_type=SalePayment.EntryType.CHANGE)
        self.assertEqual(change_row.amount, Decimal('-5.00'))
        self.assertEqual(change_row.register_session, self.session)

    def test_credit_sale_without_a_customer_is_blocked(self):
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1}],
                payments=[{'method': 'CASH', 'amount': '20.00'}],
            )
        self.assertEqual(ctx.exception.code, 'CREDIT_NEEDS_CUSTOMER')

    def test_credit_limit_is_enforced(self):
        customer = Customer.objects.create(
            phone_number='0244000001', first_name='Kwame', credit_limit=Decimal('50.00'),
        )
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 2}],                      # 90.00
                payments=[{'method': 'CASH', 'amount': '20.00'}],             # 70.00 on credit
                customer=customer,
            )
        self.assertEqual(ctx.exception.code, 'CREDIT_LIMIT')

    def test_credit_within_limit_creates_debt_and_consumes_the_limit(self):
        customer = Customer.objects.create(
            phone_number='0244000002', first_name='Yaw', credit_limit=Decimal('500.00'),
        )
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[{'method': 'CASH', 'amount': '40.00'}],
            customer=customer,
        )
        self.assertEqual(sale.balance_remaining, Decimal('50.00'))
        self.assertEqual(customer.current_debt, Decimal('50.00'))
        self.assertEqual(customer.available_credit, Decimal('450.00'))

        # This is the regression that used to crash: current_debt via `purchases`.
        self.assertEqual(customer.unpaid_invoices().count(), 1)

    def test_zero_and_negative_prices_are_refused(self):
        with self.assertRaises(SaleError):
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1, 'price': '0'}],
                payments=[{'method': 'CASH', 'amount': '0'}],
            )

    def test_credit_cannot_be_tendered_as_a_payment_method(self):
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1}],
                payments=[{'method': 'CREDIT', 'amount': '45.00'}],
            )
        self.assertEqual(ctx.exception.code, 'CREDIT_AS_PAYMENT')

    def test_cost_price_resyncs_to_the_weighted_average_after_a_sale(self):
        # Sell all 4 cheap pieces; only the 310 batch remains.
        self.sell(
            cart=[{'id': self.lamp.id, 'qty': 2, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '2400.00'}],
        )
        self.lamp.refresh_from_db()
        self.assertEqual(self.lamp.cost_price, Decimal('310.00'))

    def test_inactive_product_cannot_be_sold(self):
        self.filter.is_active = False
        self.filter.save()
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1}],
                payments=[{'method': 'CASH', 'amount': '45.00'}],
            )
        self.assertEqual(ctx.exception.code, 'INACTIVE_PRODUCT')


class PromotionTests(MoneyBase):
    """
    A promotion is the ONLY thing that may take a price under the margin floor,
    and only down to the promotional price itself.
    """

    def _promo(self, **kwargs):
        from django.utils import timezone
        defaults = {
            'name': 'Easter Clearance',
            'discount_type': Promotion.DiscountType.PERCENT,
            'value': Decimal('20.00'),
            'starts_at': timezone.now() - timedelta(hours=1),
            'created_by': self.manager,
        }
        defaults.update(kwargs)
        products = defaults.pop('products', None)
        categories = defaults.pop('categories', None)
        locations = defaults.pop('locations', None)
        promo = Promotion.objects.create(**defaults)
        if products:
            promo.products.set(products)
        if categories:
            promo.categories.set(categories)
        if locations:
            promo.locations.set(locations)
        return promo

    def test_percentage_promotion_becomes_the_charged_price(self):
        self._promo(value=Decimal('20.00'), products=[self.lamp])

        # 1200 pair price less 20% = 960
        self.assertEqual(self.lamp.base_price(Product.SellMode.PAIR), Decimal('1200.00'))
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        line = quote.lines[0]
        self.assertEqual(line.unit_price, Decimal('960.00'))
        self.assertEqual(line.normal_price, Decimal('1200.00'))
        self.assertIsNotNone(line.promotion)
        # This promotion is still above cost + margin, so the floor is unchanged
        # and the cashier keeps their normal negotiating room.
        self.assertEqual(line.floor, Decimal('550.00'))

    def test_a_promotion_above_cost_does_not_remove_normal_haggling_room(self):
        self._promo(value=Decimal('20.00'), products=[self.lamp])

        # 900 is under the 960 promo price but still above the 550 cost floor.
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '900.00'}],
            payments=[{'method': 'CASH', 'amount': '900.00'}],
        )
        self.assertEqual(sale.total_amount, Decimal('900.00'))
        # ...but 500 is under the cost floor and is still refused outright.
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '500.00'}],
                payments=[{'method': 'CASH', 'amount': '500.00'}],
            )
        self.assertEqual(ctx.exception.code, 'BELOW_FLOOR')

    def test_a_below_cost_promotion_lowers_the_floor_to_the_promo_price(self):
        self._promo(value=Decimal('60.00'), products=[self.lamp], allow_below_cost=True)

        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        # 1200 less 60% = 480, below the 500 pair cost. The floor follows it down.
        self.assertEqual(quote.lines[0].unit_price, Decimal('480.00'))
        self.assertEqual(quote.lines[0].floor, Decimal('480.00'))

        # And nothing can go below even that.
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR', 'price': '400.00'}],
                payments=[{'method': 'CASH', 'amount': '400.00'}],
            )
        self.assertEqual(ctx.exception.code, 'BELOW_FLOOR')
        self.assertIn('on promotion', ctx.exception.message)

    def test_promotion_flows_through_to_a_broken_pair(self):
        self._promo(value=Decimal('20.00'), products=[self.lamp])
        # Promo pair price 960, single = 60% of 960 = 576 (not 60% of 1200).
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'SINGLE'}], self.shop, self.cashier
        )
        self.assertEqual(quote.lines[0].unit_price, Decimal('576.00'))

    def test_promotion_applies_to_wholesale_prices_too(self):
        self._promo(value=Decimal('20.00'), products=[self.lamp])
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier,
            wholesale=True,
        )
        # 1050 wholesale pair less 20% = 840
        self.assertEqual(quote.lines[0].unit_price, Decimal('840.00'))
        self.assertEqual(quote.lines[0].normal_price, Decimal('1050.00'))

    def test_promotional_sale_records_which_promotion_authorised_it(self):
        promo = self._promo(value=Decimal('60.00'), products=[self.lamp],
                            allow_below_cost=True)
        # 1200 less 60% = 480, which is below the 500 pair cost -- allowed here.
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '480.00'}],
        )
        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal('480.00'))
        self.assertEqual(item.promotion, promo)
        self.assertTrue(item.below_floor)
        self.assertTrue(sale.has_price_override)
        self.assertEqual(item.list_price, Decimal('1200.00'))       # normal price kept
        self.assertEqual(item.discount_amount, Decimal('720.00'))   # customer's saving
        self.assertIn(promo.name, item.override_reason)

    def test_promotion_suspends_itself_rather_than_selling_below_cost(self):
        # 60% off puts the pair at 480 against a 500 cost, and this promotion is
        # NOT authorised to lose money -- so it must not apply at all.
        self._promo(value=Decimal('60.00'), products=[self.lamp], allow_below_cost=False)

        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        line = quote.lines[0]
        self.assertIsNone(line.promotion)
        self.assertEqual(line.unit_price, Decimal('1200.00'))   # normal price
        self.assertEqual(line.floor, Decimal('550.00'))         # normal floor

    def test_fixed_price_promotion(self):
        self._promo(discount_type=Promotion.DiscountType.FIXED_PRICE,
                    value=Decimal('999.00'), products=[self.lamp])
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertEqual(quote.lines[0].unit_price, Decimal('999.00'))

    def test_amount_off_promotion(self):
        self._promo(discount_type=Promotion.DiscountType.AMOUNT_OFF,
                    value=Decimal('150.00'), products=[self.lamp])
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertEqual(quote.lines[0].unit_price, Decimal('1050.00'))

    def test_category_promotion_covers_child_categories(self):
        parent = Category.objects.create(name='Lighting', slug='lighting')
        self.category.parent = parent
        self.category.save()
        self._promo(value=Decimal('10.00'), categories=[parent])

        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertEqual(quote.lines[0].unit_price, Decimal('1080.00'))

    def test_catalogue_wide_promotion_hits_everything(self):
        self._promo(value=Decimal('10.00'), applies_to_all=True)
        quote = services.quote_cart(
            [{'id': self.filter.id, 'qty': 1}], self.shop, self.cashier
        )
        self.assertEqual(quote.lines[0].unit_price, Decimal('40.50'))

    def test_a_promotion_that_has_not_started_does_nothing(self):
        from django.utils import timezone
        self._promo(value=Decimal('50.00'), products=[self.lamp],
                    starts_at=timezone.now() + timedelta(days=2))
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertIsNone(quote.lines[0].promotion)
        self.assertEqual(quote.lines[0].unit_price, Decimal('1200.00'))

    def test_an_expired_promotion_does_nothing(self):
        from django.utils import timezone
        self._promo(value=Decimal('50.00'), products=[self.lamp],
                    starts_at=timezone.now() - timedelta(days=10),
                    ends_at=timezone.now() - timedelta(days=1))
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertIsNone(quote.lines[0].promotion)

    def test_a_switched_off_promotion_does_nothing(self):
        promo = self._promo(value=Decimal('50.00'), products=[self.lamp])
        promo.is_active = False
        promo.save()
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertIsNone(quote.lines[0].promotion)

    def test_a_promotion_scoped_to_another_shop_does_not_apply_here(self):
        other_shop = Location.objects.create(name='Kaneshie Branch', address='Kaneshie')
        self._promo(value=Decimal('50.00'), products=[self.lamp], locations=[other_shop])

        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertIsNone(quote.lines[0].promotion)

    def test_best_promotion_wins_when_two_overlap(self):
        self._promo(name='Small', value=Decimal('10.00'), products=[self.lamp])
        self._promo(name='Big', value=Decimal('25.00'), products=[self.lamp])
        quote = services.quote_cart(
            [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}], self.shop, self.cashier
        )
        self.assertEqual(quote.lines[0].promotion.name, 'Big')
        self.assertEqual(quote.lines[0].unit_price, Decimal('900.00'))

    def test_promo_price_shows_up_in_the_pos_catalogue(self):
        self._promo(value=Decimal('20.00'), products=[self.lamp])
        self.client.force_login(self.cashier)
        row = self.client.get(reverse('sales:api_products'), {'q': 'corolla'}).json()['results'][0]
        self.assertEqual(row['retail'], 960.0)
        self.assertEqual(row['normal_retail'], 1200.0)
        self.assertEqual(row['single_retail'], 576.0)      # 60% of the promo pair price
        self.assertEqual(row['promo']['label'], '20% off')
        self.assertEqual(row['promo']['normal_price'], 1200.0)


class DebtSettlementTests(MoneyBase):

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            phone_number='0244000003', first_name='Kofi', credit_limit=Decimal('1000.00'),
        )
        self.sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],                 # 90.00
            payments=[{'method': 'CASH', 'amount': '40.00'}],
            customer=self.customer,
        )

    def test_settlement_goes_into_the_current_drawer_not_the_original(self):
        # A different cashier's shift collects the debt later.
        other = User.objects.create_user(
            username='esi', password='x', role=User.Role.CASHIER, assigned_location=self.shop,
        )
        other_session = RegisterSession.objects.create(
            user=other, location=self.shop, opening_balance=Decimal('0.00'),
        )

        services.settle_debt(sale=self.sale, user=other, amount='50.00', method='CASH')

        payment = self.sale.payments.get(entry_type=SalePayment.EntryType.DEBT)
        self.assertEqual(payment.register_session, other_session)

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.balance_remaining, Decimal('0.00'))
        self.assertTrue(self.sale.is_fully_paid)

        # The debt lands in the collecting drawer, not the one that made the sale.
        other_session.recalculate()
        self.session.recalculate()
        self.assertEqual(other_session.total_cash_sales, Decimal('50.00'))
        self.assertEqual(self.session.total_cash_sales, Decimal('40.00'))

    def test_overpaying_a_debt_is_refused(self):
        with self.assertRaises(SaleError) as ctx:
            services.settle_debt(sale=self.sale, user=self.cashier, amount='500.00', method='CASH')
        self.assertEqual(ctx.exception.code, 'OVERPAYMENT')

    def test_settling_twice_cannot_overpay(self):
        services.settle_debt(sale=self.sale, user=self.cashier, amount='50.00', method='CASH')
        with self.assertRaises(SaleError) as ctx:
            services.settle_debt(sale=self.sale, user=self.cashier, amount='1.00', method='CASH')
        self.assertEqual(ctx.exception.code, 'ALREADY_PAID')


class PartPaymentTests(MoneyBase):
    """
    "Pay some now, pay the rest later" -- the flow this shop actually runs on.
    """

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            phone_number='0244777888', first_name='Musah', workshop_name='Musah Auto',
            credit_limit=Decimal('3000.00'),
        )

    def test_deposit_now_then_two_instalments_later(self):
        # Pair of lamps: 2,400. Mechanic puts down 400 today.
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 2, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '400.00'}],
            customer=self.customer,
        )
        self.assertEqual(sale.total_amount, Decimal('2400.00'))
        self.assertEqual(sale.amount_paid, Decimal('400.00'))
        self.assertEqual(sale.balance_remaining, Decimal('2000.00'))
        self.assertFalse(sale.is_fully_paid)
        self.assertTrue(sale.is_credit_sale)
        self.assertEqual(self.customer.current_debt, Decimal('2000.00'))
        self.assertEqual(self.customer.available_credit, Decimal('1000.00'))

        # Instalment one: 1,200 by MoMo.
        services.settle_debt(sale=sale, user=self.cashier, amount='1200.00',
                             method='MOMO', reference='MM-77812')
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, Decimal('1600.00'))
        self.assertEqual(sale.balance_remaining, Decimal('800.00'))
        self.assertEqual(self.customer.current_debt, Decimal('800.00'))

        # Instalment two clears it.
        services.settle_debt(sale=sale, user=self.cashier, amount='800.00', method='CASH')
        sale.refresh_from_db()
        self.assertEqual(sale.balance_remaining, Decimal('0.00'))
        self.assertTrue(sale.is_fully_paid)
        self.assertEqual(self.customer.current_debt, Decimal('0.00'))
        self.assertEqual(self.customer.available_credit, Decimal('3000.00'))

        # Three money rows, and the reference on the MoMo one was kept.
        self.assertEqual(sale.payments.count(), 3)
        momo = sale.payments.get(payment_method='MOMO')
        self.assertEqual(momo.reference_id, 'MM-77812')
        self.assertEqual(momo.entry_type, SalePayment.EntryType.DEBT)

    def test_split_deposit_across_cash_and_momo_leaves_the_right_balance(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],   # 1200
            payments=[
                {'method': 'CASH', 'amount': '300.00'},
                {'method': 'MOMO', 'amount': '200.00'},
            ],
            customer=self.customer,
        )
        self.assertEqual(sale.amount_paid, Decimal('500.00'))
        self.assertEqual(sale.balance_remaining, Decimal('700.00'))
        self.assertEqual(sale.change_due, Decimal('0.00'))

        self.session.recalculate()
        self.assertEqual(self.session.total_cash_sales, Decimal('300.00'))
        self.assertEqual(self.session.total_momo_sales, Decimal('200.00'))
        self.assertEqual(self.session.total_credit_extended, Decimal('700.00'))

    def test_debt_accumulates_across_several_invoices_and_blocks_at_the_limit(self):
        # Three visits, part-paid each time: 135 billed, 15 down, 120 on credit.
        for _ in range(3):
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 3}],
                payments=[{'method': 'CASH', 'amount': '15.00'}],
                customer=self.customer,
            )
        self.assertEqual(self.customer.current_debt, Decimal('360.00'))
        self.assertEqual(self.customer.unpaid_invoices().count(), 3)

        # Drop the limit to exactly what is owed: no more credit at all.
        self.customer.credit_limit = Decimal('360.00')
        self.customer.save()
        self.assertEqual(self.customer.available_credit, Decimal('0.00'))

        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1}],
                payments=[],
                customer=self.customer,
            )
        self.assertEqual(ctx.exception.code, 'CREDIT_LIMIT')

    def test_paying_off_one_invoice_frees_credit_for_the_next_sale(self):
        self.customer.credit_limit = Decimal('300.00')
        self.customer.save()

        first = self.sell(
            cart=[{'id': self.filter.id, 'qty': 5}],           # 225
            payments=[],                                        # all on credit
            customer=self.customer,
        )
        self.assertEqual(self.customer.current_debt, Decimal('225.00'))

        # No room for another 225.
        with self.assertRaises(SaleError):
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 5}],
                payments=[],
                customer=self.customer,
            )

        # Settle part of it -- 150 -- and the room reappears.
        services.settle_debt(sale=first, user=self.cashier, amount='150.00', method='CASH')
        self.assertEqual(self.customer.current_debt, Decimal('75.00'))

        second = self.sell(
            cart=[{'id': self.filter.id, 'qty': 4}],           # 180
            payments=[],
            customer=self.customer,
        )
        self.assertEqual(self.customer.current_debt, Decimal('255.00'))
        self.assertEqual(self.customer.available_credit, Decimal('45.00'))

    def test_a_zero_deposit_credit_sale_is_allowed_within_the_limit(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[],
            customer=self.customer,
        )
        self.assertEqual(sale.amount_paid, Decimal('0.00'))
        self.assertEqual(sale.balance_remaining, Decimal('90.00'))
        self.assertEqual(sale.status, Sale.Status.COMPLETED)   # goods handed over

    def test_lifetime_stats_track_the_invoice_not_the_cash(self):
        self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],           # 90
            payments=[{'method': 'CASH', 'amount': '10.00'}],
            customer=self.customer,
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.total_spent, Decimal('90.00'))
        self.assertEqual(self.customer.total_visits, 1)
        self.assertIsNotNone(self.customer.last_visit_date)

    def test_receivables_screen_lists_the_debt(self):
        self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '200.00'}],
            customer=self.customer,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse('customers:receivables'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['grand_total'], Decimal('1000.00'))
        self.assertEqual(response.context['invoice_count'], 1)
        self.assertContains(response, 'Musah Auto')

    def test_settling_from_the_web_form_updates_everything(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[{'method': 'CASH', 'amount': '40.00'}],
            customer=self.customer,
        )
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('sales:add_payment', args=[sale.pk]), {
            'amount': '30.00', 'payment_method': 'CASH', 'reference': '',
        })
        self.assertEqual(response.status_code, 302)

        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, Decimal('70.00'))
        self.assertEqual(sale.balance_remaining, Decimal('20.00'))

    def test_web_form_rejects_an_overpayment_without_touching_the_sale(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[{'method': 'CASH', 'amount': '40.00'}],
            customer=self.customer,
        )
        self.client.force_login(self.cashier)
        self.client.post(reverse('sales:add_payment', args=[sale.pk]), {
            'amount': '500.00', 'payment_method': 'CASH',
        })
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, Decimal('40.00'))

    def test_statement_shows_the_outstanding_amount_per_invoice(self):
        self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '500.00'}],
            customer=self.customer,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse('customers:statement', args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200)
        invoice = response.context['unpaid_invoices'][0]
        self.assertEqual(q2(invoice.outstanding), Decimal('700.00'))
        self.assertEqual(response.context['total_outstanding'], Decimal('700.00'))


class RefundTests(MoneyBase):

    def test_refund_restocks_the_original_batches_and_reverses_the_cash(self):
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 2, 'sellMode': 'PAIR'}],   # 4 pieces from batch A
            payments=[{'method': 'CASH', 'amount': '2400.00'}],
        )
        item = sale.items.get()

        result = services.refund_sale(
            sale=sale, user=self.manager,
            lines=[{'item_id': item.id, 'quantity': 1}],                 # one pair back
            reason='Wrong model', refund_method='CASH',
        )

        self.assertEqual(result['refund_value'], Decimal('1200.00'))
        self.assertEqual(result['cash_back'], Decimal('1200.00'))
        self.assertEqual(result['offset_against_debt'], Decimal('0.00'))

        self.batch_a.refresh_from_db()
        self.assertEqual(self.batch_a.quantity, 2)      # 2 pieces went back

        item.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(item.quantity_refunded, 1)
        self.assertEqual(item.total_cost, Decimal('500.00'))   # COGS halved
        self.assertEqual(sale.refunded_amount, Decimal('1200.00'))
        self.assertEqual(sale.net_amount, Decimal('1200.00'))
        self.assertEqual(sale.amount_paid, Decimal('1200.00'))
        self.assertEqual(sale.status, Sale.Status.PARTIAL_REFUND)
        self.assertEqual(sale.gross_profit, Decimal('700.00'))

    def test_refund_on_a_credit_sale_clears_the_debt_before_paying_cash(self):
        customer = Customer.objects.create(
            phone_number='0244000004', first_name='Abena', credit_limit=Decimal('5000.00'),
        )
        sale = self.sell(
            cart=[{'id': self.lamp.id, 'qty': 1, 'sellMode': 'PAIR'}],   # 1200
            payments=[{'method': 'CASH', 'amount': '200.00'}],           # 1000 on credit
            customer=customer,
        )
        item = sale.items.get()

        result = services.refund_sale(
            sale=sale, user=self.manager,
            lines=[{'item_id': item.id, 'quantity': 1}],
            reason='Returned unused',
        )

        # The 1200 refund wipes the 1000 debt first, then 200 leaves the drawer.
        self.assertEqual(result['offset_against_debt'], Decimal('1000.00'))
        self.assertEqual(result['cash_back'], Decimal('200.00'))

        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.REFUNDED)
        self.assertEqual(sale.net_amount, Decimal('0.00'))
        self.assertEqual(sale.amount_paid, Decimal('0.00'))
        self.assertEqual(customer.current_debt, Decimal('0.00'))

    def test_cashier_cannot_refund(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 1}],
            payments=[{'method': 'CASH', 'amount': '45.00'}],
        )
        with self.assertRaises(SaleError) as ctx:
            services.refund_sale(
                sale=sale, user=self.cashier,
                lines=[{'item_id': sale.items.get().id, 'quantity': 1}],
            )
        self.assertEqual(ctx.exception.code, 'FORBIDDEN')

    def test_cannot_refund_more_than_was_sold(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 1}],
            payments=[{'method': 'CASH', 'amount': '45.00'}],
        )
        with self.assertRaises(SaleError) as ctx:
            services.refund_sale(
                sale=sale, user=self.manager,
                lines=[{'item_id': sale.items.get().id, 'quantity': 5}],
            )
        self.assertEqual(ctx.exception.code, 'REFUND_TOO_MANY')

    def test_refund_without_restock_does_not_return_pieces_to_the_shelf(self):
        sale = self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[{'method': 'CASH', 'amount': '90.00'}],
        )
        batch = StockBatch.objects.get(product=self.filter)
        before = batch.quantity

        services.refund_sale(
            sale=sale, user=self.manager,
            lines=[{'item_id': sale.items.get().id, 'quantity': 1}],
            reason='Smashed on arrival', restock=False,
        )

        batch.refresh_from_db()
        self.assertEqual(batch.quantity, before)


class RegisterSessionTests(MoneyBase):

    def test_expected_cash_accounts_for_sales_change_refunds_and_expenses(self):
        # Cash sale of 90 with 100 tendered -> 10 change.
        self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[{'method': 'CASH', 'amount': '100.00'}],
        )
        # A MoMo sale must NOT change the drawer.
        self.sell(
            cart=[{'id': self.filter.id, 'qty': 1}],
            payments=[{'method': 'MOMO', 'amount': '45.00'}],
        )
        # Petty cash out of the till.
        category = ExpenseCategory.objects.create(name='Transport')
        Expense.objects.create(
            location=self.shop, category=category, amount=Decimal('30.00'),
            description='Taxi for parts run', requested_by=self.cashier,
            date_incurred='2026-08-17', is_paid_from_till=True,
            register_session=self.session, status=Expense.Status.APPROVED,
        )

        self.session.recalculate()
        # 100 opening float + 90 cash sale - 30 expense = 160
        self.assertEqual(self.session.total_cash_sales, Decimal('90.00'))
        self.assertEqual(self.session.total_momo_sales, Decimal('45.00'))
        self.assertEqual(self.session.total_till_expenses, Decimal('30.00'))
        self.assertEqual(self.session.expected_cash, Decimal('160.00'))

    def test_credit_extended_is_tracked_on_the_shift(self):
        customer = Customer.objects.create(
            phone_number='0244000005', credit_limit=Decimal('1000.00'),
        )
        self.sell(
            cart=[{'id': self.filter.id, 'qty': 2}],
            payments=[{'method': 'CASH', 'amount': '40.00'}],
            customer=customer,
        )
        self.session.recalculate()
        self.assertEqual(self.session.total_credit_extended, Decimal('50.00'))

    def test_closing_a_balanced_drawer_is_clean(self):
        self.sell(
            cart=[{'id': self.filter.id, 'qty': 1}],
            payments=[{'method': 'CASH', 'amount': '45.00'}],
        )
        session = services.close_session(
            session=self.session, user=self.cashier, actual_cash='145.00',
        )
        self.assertEqual(session.status, RegisterSession.Status.CLOSED)
        self.assertEqual(session.discrepancy, Decimal('0.00'))

    def test_short_drawer_is_flagged_as_a_discrepancy(self):
        self.sell(
            cart=[{'id': self.filter.id, 'qty': 1}],
            payments=[{'method': 'CASH', 'amount': '45.00'}],
        )
        session = services.close_session(
            session=self.session, user=self.cashier, actual_cash='120.00',
            notes='Cannot account for it',
        )
        self.assertEqual(session.status, RegisterSession.Status.DISCREPANCY)
        self.assertEqual(session.discrepancy, Decimal('-25.00'))

    def test_a_closed_session_cannot_be_closed_again(self):
        services.close_session(session=self.session, user=self.cashier, actual_cash='100.00')
        with self.assertRaises(SaleError) as ctx:
            services.close_session(session=self.session, user=self.cashier, actual_cash='100.00')
        self.assertEqual(ctx.exception.code, 'ALREADY_CLOSED')

    def test_selling_without_an_open_register_is_refused(self):
        services.close_session(session=self.session, user=self.cashier, actual_cash='100.00')
        with self.assertRaises(SaleError) as ctx:
            self.sell(
                cart=[{'id': self.filter.id, 'qty': 1}],
                payments=[{'method': 'CASH', 'amount': '45.00'}],
            )
        self.assertEqual(ctx.exception.code, 'NO_SESSION')


class PosEndpointTests(MoneyBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)

    def test_product_search_is_server_side_and_scoped_to_the_shop(self):
        response = self.client.get(reverse('sales:api_products'), {'q': 'corolla'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total'], 1)
        row = data['results'][0]
        self.assertEqual(row['sku'], 'HL-COR-15')
        self.assertEqual(row['pieces'], 6)
        self.assertEqual(row['sellable_pairs'], 3)
        self.assertEqual(row['single_retail'], 720.0)

    def test_search_matches_sku_and_brand_not_just_name(self):
        response = self.client.get(reverse('sales:api_products'), {'q': 'HL-COR'})
        self.assertEqual(response.json()['total'], 1)

    def test_cashier_does_not_receive_cost_prices(self):
        row = self.client.get(reverse('sales:api_products'), {'q': 'corolla'}).json()['results'][0]
        self.assertNotIn('cost', row)

        self.client.force_login(self.manager)
        row = self.client.get(reverse('sales:api_products'), {'q': 'corolla'}).json()['results'][0]
        self.assertEqual(row['cost'], 250.0)

    def test_low_stock_filter(self):
        response = self.client.get(reverse('sales:api_products'), {'stock': 'low'})
        # Lamp threshold is 4 and 6 pieces are on hand, so it is not low.
        self.assertEqual(response.json()['total'], 0)

        self.batch_a.quantity = 1
        self.batch_a.save()
        self.batch_b.quantity = 1
        self.batch_b.save()
        response = self.client.get(reverse('sales:api_products'), {'stock': 'low'})
        self.assertEqual(response.json()['total'], 1)

    def test_quote_endpoint_prices_the_cart_without_saving_anything(self):
        response = self.client.post(
            reverse('sales:api_quote'),
            data={'cart': [{'id': self.lamp.id, 'qty': 1, 'sellMode': 'SINGLE'}]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total'], 720.0)
        self.assertEqual(Sale.objects.count(), 0)

    def test_process_sale_endpoint_returns_a_printable_receipt(self):
        response = self.client.post(
            reverse('sales:process_sale'),
            data={
                'cart': [{'id': self.filter.id, 'qty': 1}],
                'payments': [{'method': 'CASH', 'amount': '45.00'}],
                'total_amount': '45.00',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertIn('Oil Filter', payload['receipt_html'])
        self.assertIn(payload['invoice_number'], payload['receipt_html'])

    def test_rejected_sale_returns_the_reason_and_saves_nothing(self):
        response = self.client.post(
            reverse('sales:process_sale'),
            data={
                'cart': [{'id': self.lamp.id, 'qty': 99, 'sellMode': 'PAIR'}],
                'payments': [{'method': 'CASH', 'amount': '1.00'}],
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'INSUFFICIENT_STOCK')
        self.assertEqual(Sale.objects.count(), 0)

    def test_batch_detail_shows_every_batch_but_hides_cost_from_cashiers(self):
        response = self.client.get(
            reverse('sales:api_product_batches', args=[self.lamp.id])
        )
        data = response.json()
        self.assertEqual(len(data['batches']), 2)
        self.assertEqual(data['product']['pieces_here'], 6)
        self.assertNotIn('cost_price', data['batches'][0])
