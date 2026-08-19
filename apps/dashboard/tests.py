"""
Render smoke tests.

Every page touched by the pricing/money rework is loaded as each role that is
supposed to reach it. Template errors and broken url names fail here rather
than in front of a cashier.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.customers.models import Customer
from apps.finance.models import ExpenseCategory
from apps.inventory.models import Shipment, ShipmentItem, StockBatch, Supplier
from apps.location.models import Location
from apps.products.models import Category, Product, Promotion
from apps.sales import services
from apps.sales.models import RegisterSession

User = get_user_model()


class PageRenderTests(TestCase):

    def setUp(self):
        self.shop = Location.objects.create(
            name='Main Shop', location_type=Location.LocationType.SHOP, address='Abossey Okai',
        )
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER, assigned_location=self.shop,
        )
        self.cashier = User.objects.create_user(
            username='cash', password='pw', role=User.Role.CASHIER, assigned_location=self.shop,
        )
        self.category = Category.objects.create(name='Lamps', slug='lamps')
        self.product = Product.objects.create(
            name='Hilux Head Lamp', slug='hilux-lamp', sku='HL-1', category=self.category,
            cost_price=Decimal('250.00'), selling_price=Decimal('1200.00'),
            is_sold_in_pairs=True, low_stock_threshold=4,
        )
        StockBatch.objects.create(
            product=self.product, location=self.shop, quantity=6,
            cost_price=Decimal('250.00'), batch_number='LSA-1',
        )
        self.customer = Customer.objects.create(
            phone_number='0244111222', first_name='Kwesi', workshop_name='Kwesi Motors',
            credit_limit=Decimal('2000.00'),
        )
        ExpenseCategory.objects.create(name='Transport')

        self.session = RegisterSession.objects.create(
            user=self.cashier, location=self.shop, opening_balance=Decimal('50.00'),
        )
        self.owner_session = RegisterSession.objects.create(
            user=self.owner, location=self.shop, opening_balance=Decimal('0.00'),
        )

        # One credit sale so debt/refund/receipt pages have something to show.
        self.sale = services.create_sale(
            user=self.cashier,
            cart=[{'id': self.product.id, 'qty': 1, 'sellMode': 'PAIR'}],
            payments=[{'method': 'CASH', 'amount': '200.00'}],
            customer=self.customer,
        )

    def assertRenders(self, url, user, name=''):
        self.client.force_login(user)
        response = self.client.get(url)
        self.assertIn(
            response.status_code, (200, 302),
            f"{name or url} returned {response.status_code}",
        )
        return response

    # --- Dashboard ---
    def test_owner_dashboard_renders(self):
        response = self.assertRenders(reverse('dashboard:analytics'), self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Gross Profit')

    def test_cashier_dashboard_renders_without_profit(self):
        response = self.assertRenders(reverse('dashboard:analytics'), self.cashier)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Gross Profit')
        # The cashier must still see their own drawer.
        self.assertContains(response, 'My open shift')

    def test_dashboard_revenue_is_not_multiplied_by_line_count(self):
        # Two lines on one invoice used to inflate revenue via the items join.
        services.create_sale(
            user=self.cashier,
            cart=[
                {'id': self.product.id, 'qty': 1, 'sellMode': 'SINGLE'},
                {'id': self.product.id, 'qty': 1, 'sellMode': 'PAIR'},
            ],
            payments=[{'method': 'CASH', 'amount': '1920.00'}],
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse('dashboard:analytics'))
        # 1200 (first sale) + 1920 = 3120.00 exactly
        self.assertEqual(response.context['revenue'], Decimal('3120.00'))
        self.assertEqual(response.context['transactions'], 2)

    # --- POS ---
    def test_pos_renders_for_cashier_with_open_session(self):
        response = self.assertRenders(reverse('sales:pos'), self.cashier)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Search the whole catalogue')
        # The catalogue must NOT be dumped into the HTML any more.
        self.assertNotContains(response, 'product-card-')

    def test_pos_asks_to_open_register_when_none_is_open(self):
        services.close_session(session=self.session, user=self.cashier, actual_cash='250.00')
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('sales:pos'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/open_register.html')

    # --- Sales ---
    def test_sale_pages_render(self):
        self.assertRenders(reverse('sales:list'), self.owner, 'sale list')
        self.assertRenders(reverse('sales:detail', args=[self.sale.pk]), self.owner, 'sale detail')
        self.assertRenders(reverse('sales:sessions'), self.cashier, 'sessions')
        self.assertRenders(reverse('sales:session_detail', args=[self.session.pk]), self.cashier)
        self.assertRenders(reverse('sales:close_register'), self.cashier, 'close register')
        self.assertRenders(reverse('sales:returns'), self.owner, 'returns')
        self.assertRenders(reverse('sales:deliveries'), self.owner, 'deliveries')

    def test_refund_page_renders_for_manager_only(self):
        response = self.assertRenders(reverse('sales:refund', args=[self.sale.pk]), self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'refund_qty_')

        self.client.force_login(self.cashier)
        response = self.client.get(reverse('sales:refund', args=[self.sale.pk]))
        self.assertEqual(response.status_code, 302)   # bounced

    def test_sale_detail_shows_the_outstanding_balance(self):
        response = self.assertRenders(reverse('sales:detail', args=[self.sale.pk]), self.owner)
        self.assertContains(response, 'Balance owing')

    def test_receipt_endpoint_reprints(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('sales:api_receipt', args=[self.sale.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Hilux Head Lamp', response.json()['receipt_html'])

    # --- Products ---
    def test_product_pages_render(self):
        self.assertRenders(reverse('products:product_list'), self.owner, 'product list')
        self.assertRenders(reverse('products:product_detail', args=[self.product.pk]), self.owner)
        self.assertRenders(reverse('products:product_create'), self.owner, 'product create')
        self.assertRenders(reverse('products:product_edit', args=[self.product.pk]), self.owner)
        self.assertRenders(reverse('products:price_suggestions'), self.owner, 'price suggestions')

    def test_product_form_exposes_the_split_rule(self):
        response = self.assertRenders(reverse('products:product_edit', args=[self.product.pk]), self.owner)
        self.assertContains(response, 'split_price_percentage')
        self.assertContains(response, 'Breaking the pair')

    def test_editing_a_price_marks_it_manual_and_logs_it(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('products:product_edit', args=[self.product.pk]), {
            'name': self.product.name,
            'category': self.category.pk,
            'sku': self.product.sku,
            'cost_price': '250.00',
            'selling_price': '1350.00',
            'is_sold_in_pairs': 'on',
            'split_price_mode': 'PERCENT',
            'split_price_percentage': '60.00',
            'tax_rate': '0.00',
            'low_stock_threshold': '4',
            'is_returnable': 'on',
            'description': '',
        })
        self.assertEqual(response.status_code, 302)

        self.product.refresh_from_db()
        self.assertEqual(self.product.selling_price, Decimal('1350.00'))
        self.assertTrue(self.product.price_is_manual)
        # And the split price followed the pair price automatically: 60% of 1350.
        self.assertEqual(self.product.effective_single_price, Decimal('810.00'))

        log = self.product.price_history.filter(field='selling_price').first()
        self.assertEqual(log.new_value, Decimal('1350.00'))
        self.assertEqual(log.changed_by, self.owner)

    def test_form_rejects_a_price_below_landed_cost(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('products:product_edit', args=[self.product.pk]), {
            'name': self.product.name,
            'category': self.category.pk,
            'sku': self.product.sku,
            'cost_price': '250.00',
            'selling_price': '400.00',       # below 250 x 2 for a pair
            'is_sold_in_pairs': 'on',
            'split_price_mode': 'PERCENT',
            'split_price_percentage': '60.00',
            'tax_rate': '0.00',
            'low_stock_threshold': '4',
            'description': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'below the landed cost')
        self.product.refresh_from_db()
        self.assertEqual(self.product.selling_price, Decimal('1200.00'))   # unchanged

    # --- Promotions ---
    def test_promotion_pages_render_for_managers_only(self):
        self.assertRenders(reverse('products:promotion_list'), self.owner, 'promotion list')
        self.assertRenders(reverse('products:promotion_create'), self.owner, 'promotion form')

        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse('products:promotion_list')).status_code, 302)
        self.assertEqual(self.client.get(reverse('products:promotion_create')).status_code, 302)

    def test_creating_a_promotion_through_the_form(self):
        from django.utils import timezone
        self.client.force_login(self.owner)
        response = self.client.post(reverse('products:promotion_create'), {
            'name': 'Easter Clearance',
            'description': 'Moving old Hilux stock',
            'discount_type': 'PERCENT',
            'value': '15',
            'products': [self.product.pk],
            'starts_at': timezone.now().strftime('%Y-%m-%dT%H:%M'),
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)

        promo = Promotion.objects.get(name='Easter Clearance')
        self.assertEqual(promo.created_by, self.owner)
        self.assertTrue(promo.is_running())
        # 1200 less 15% = 1020, and the split price follows: 60% of 1020.
        self.assertEqual(promo.price_for(self.product.selling_price), Decimal('1020.00'))
        self.assertEqual(
            self.product.base_price(Product.SellMode.SINGLE, promotion=promo), Decimal('612.00')
        )

    def test_form_refuses_a_below_cost_promotion_unless_authorised(self):
        from django.utils import timezone
        self.client.force_login(self.owner)
        payload = {
            'name': 'Silly discount',
            'discount_type': 'PERCENT',
            'value': '80',                     # 1200 -> 240, under the 500 pair cost
            'products': [self.product.pk],
            'starts_at': timezone.now().strftime('%Y-%m-%dT%H:%M'),
            'is_active': 'on',
        }
        response = self.client.post(reverse('products:promotion_create'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'below landed cost')
        self.assertFalse(Promotion.objects.filter(name='Silly discount').exists())

        # Ticking the loss-leader box makes it an explicit, recorded decision.
        payload['allow_below_cost'] = 'on'
        response = self.client.post(reverse('products:promotion_create'), payload)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Promotion.objects.get(name='Silly discount').allow_below_cost)

    def test_form_requires_a_scope(self):
        from django.utils import timezone
        self.client.force_login(self.owner)
        response = self.client.post(reverse('products:promotion_create'), {
            'name': 'Nothing selected',
            'discount_type': 'PERCENT',
            'value': '10',
            'starts_at': timezone.now().strftime('%Y-%m-%dT%H:%M'),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose what this promotion covers')

    def test_toggling_a_promotion_off_restores_normal_prices(self):
        from django.utils import timezone
        promo = Promotion.objects.create(
            name='Weekend deal', discount_type='PERCENT', value=Decimal('10.00'),
            starts_at=timezone.now(), created_by=self.owner,
        )
        promo.products.add(self.product)

        self.client.force_login(self.cashier)
        row = self.client.get(reverse('sales:api_products'), {'q': 'Hilux'}).json()['results'][0]
        self.assertEqual(row['retail'], 1080.0)

        self.client.force_login(self.owner)
        self.client.post(reverse('products:promotion_toggle', args=[promo.pk]))
        promo.refresh_from_db()
        self.assertFalse(promo.is_active)

        self.client.force_login(self.cashier)
        row = self.client.get(reverse('sales:api_products'), {'q': 'Hilux'}).json()['results'][0]
        self.assertEqual(row['retail'], 1200.0)
        self.assertIsNone(row['promo'])

    # --- Receivables ---
    def test_receivables_page_renders(self):
        response = self.assertRenders(reverse('customers:receivables'), self.owner, 'receivables')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Kwesi Motors')
        self.assertEqual(response.context['grand_total'], Decimal('1000.00'))

    # --- Inventory ---
    def test_inventory_pages_render(self):
        supplier = Supplier.objects.create(name='Lian Sheng')
        shipment = Shipment.objects.create(
            reference_number='LSA-TEST', supplier_name=supplier.name,
            exchange_rate=Decimal('13.0000'), total_freight_usd=Decimal('0.00'),
            total_cbm=Decimal('1.0000'),
        )
        ShipmentItem.objects.create(
            shipment=shipment, product=self.product, quantity=10,
            unit_cost_usd=Decimal('20.00'), total_line_cbm=Decimal('1.0000'),
            outside_sale_price_ghs=Decimal('1200.00'),
        )
        self.assertRenders(reverse('inventory:dashboard'), self.owner, 'inventory dashboard')
        self.assertRenders(reverse('inventory:shipment_list'), self.owner, 'shipment list')
        self.assertRenders(reverse('inventory:shipment_detail', args=[shipment.pk]), self.owner)
        batch = StockBatch.objects.filter(product=self.product).first()
        self.assertRenders(reverse('inventory:batch_detail', args=[batch.pk]), self.owner)

    # --- Customers & Finance ---
    def test_customer_pages_render(self):
        self.assertRenders(reverse('customers:list'), self.owner, 'customer list')
        self.assertRenders(reverse('customers:detail', args=[self.customer.pk]), self.owner)
        response = self.assertRenders(reverse('customers:statement', args=[self.customer.pk]), self.owner)
        self.assertEqual(response.status_code, 200)

    def test_customer_search_api_returns_credit_position(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('customers:api_search'), {'q': 'Kwesi'})
        result = response.json()['results'][0]
        self.assertEqual(result['name'], 'Kwesi Motors')
        self.assertEqual(result['current_debt'], 1000.0)      # 1200 sale less 200 paid
        self.assertEqual(result['available_credit'], 1000.0)

    def test_finance_pages_render(self):
        self.assertRenders(reverse('finance:profit_loss'), self.owner, 'profit & loss')
        self.assertRenders(reverse('finance:tax_report'), self.owner, 'tax report')
        self.assertRenders(reverse('finance:expenses_list'), self.owner, 'expenses')

    def test_profit_and_loss_uses_true_batch_cost(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('finance:profit_loss'))
        self.assertEqual(response.context['total_revenue'], Decimal('1200.00'))
        self.assertEqual(response.context['total_cogs'], Decimal('500.00'))
        self.assertEqual(response.context['gross_profit'], Decimal('700.00'))
