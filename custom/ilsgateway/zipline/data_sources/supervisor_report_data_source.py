from django.db.models.aggregates import Sum
from django.utils.translation import ugettext_lazy as _

from corehq.apps.reports.datatables import DataTablesColumn
from custom.ilsgateway.zipline.data_sources.zipline_data_source import ZiplineDataSource
from custom.zipline.models import EmergencyOrder, EmergencyOrderPackage, EmergencyOrderStatusUpdate
from custom.ilsgateway.zipline import helpers


class SupervisorReportDataSource(ZiplineDataSource):

    @property
    def orders_id(self):
        return self.config.orders_id

    @property
    def filters(self):
        additional_filters = {}
        if self.sql_location.location_type_object.administrative:
            descendants = self.sql_location.get_descendants() \
                .exclude(is_archived=True).values_list('site_code', flat=True)
            additional_filters['location_code__in'] = descendants
        else:
            additional_filters['location_code'] = self.sql_location.site_code

        if self.statuses:
            additional_filters['status__in'] = self.statuses

        orders_id = filter(lambda x: bool(x), self.orders_id)
        if orders_id:
            additional_filters['pk__in'] = orders_id

        return dict(
            domain=self.domain,
            timestamp__range=[self.start_date, self.end_date],
            **additional_filters
        )

    def get_emergency_orders(self, start, limit):
        offset = start + limit
        return EmergencyOrder.objects.filter(**self.filters).select_related('confirmed_status')[start:offset]

    @property
    def total_count(self):
        return EmergencyOrder.objects.filter(**self.filters).count()

    @property
    def columns(self):
        return [
            DataTablesColumn('date', help_text=_('timestamp for receipt of incoming emg request, automatic')),
            DataTablesColumn('location code', help_text=_('the location that corresponds to the health facility')),
            DataTablesColumn('status', help_text=_('current status of the transaction (rejected, cancelled, '
                                                   'cancelled by user, received, approved, dispatched, delivered, '
                                                   'confirmed)')),
            DataTablesColumn('total delivery time', help_text=_('time between emg status and rec status, '
                                                                'total time to resupply  in minutes')),
            DataTablesColumn('confirmation timestamp', help_text=_('timestamp for receipt of rec confirmation')),
            DataTablesColumn('emergency order request', help_text=_('structured string with product long codes'
                                                                    ' (for example, 10010203MD) and quantities'
                                                                    ' for products requested in emg request ')),
            DataTablesColumn('delivered products cost', help_text=_('value of products dropped to the'
                                                                    ' health facility, tanzanian shillings')),
            DataTablesColumn('products requested and not confirmed',
                             help_text=_('structured string with products '
                                         'that were not confirmed based on the request'))
        ]

    def get_data(self, start, limit):
        emergency_orders = self.get_emergency_orders(start, limit)
        rows = []

        for emergency_order in emergency_orders:
            delivered_products_cost = EmergencyOrderPackage.objects.filter(
                order_id=emergency_order.pk,
                status=EmergencyOrderStatusUpdate.STATUS_DELIVERED
            ).aggregate(sum_cost=Sum('cost'))['sum_cost']
            rows.append([
                helpers.format_date(emergency_order.timestamp),
                emergency_order.location_code,
                emergency_order.status,
                helpers.delivery_lead_time(emergency_order, emergency_order.confirmed_status),
                helpers.status_date_or_empty_string(emergency_order.confirmed_status),
                helpers.convert_products_dict_to_list(emergency_order.products_requested),
                delivered_products_cost,
                helpers.products_requested_not_confirmed(emergency_order)
            ])
        return rows
