from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
import logging
from celery.schedules import crontab

from celery.task import task, periodic_task
from django.db import transaction
from psycopg2._psycopg import DatabaseError

from casexml.apps.stock.models import StockReport, StockTransaction
from corehq.apps.commtrack.models import StockState
from corehq.apps.locations.models import SQLLocation
from corehq.apps.products.models import Product
from custom.ilsgateway.api import ILSGatewayEndpoint, ILSGatewayAPI
from custom.ilsgateway.balance import BalanceMigration
from custom.ilsgateway.tanzania.reminders import REMINDER_MONTHLY_SOH_SUMMARY, REMINDER_MONTHLY_DELIVERY_SUMMARY, \
    REMINDER_MONTHLY_RANDR_SUMMARY
from custom.ilsgateway.tanzania.reminders.delivery import DeliveryReminder
from custom.ilsgateway.tanzania.reminders.randr import RandrReminder
from custom.ilsgateway.tanzania.reminders.reports import get_district_people, construct_soh_summary, \
    construct_delivery_summary, construct_randr_summary
from custom.ilsgateway.tanzania.reminders.soh_thank_you import SOHThankYouReminder
from custom.ilsgateway.tanzania.reminders.stockonhand import SOHReminder
from custom.ilsgateway.tanzania.reminders.supervision import SupervisionReminder
from custom.ilsgateway.tanzania.warehouse.updater import populate_report_data, default_start_date, \
    process_facility_warehouse_data, process_non_facility_warehouse_data
from custom.ilsgateway.temporary import fix_stock_data
from custom.ilsgateway.utils import send_for_day, send_for_all_domains, send_translated_message
from custom.logistics.commtrack import bootstrap_domain as ils_bootstrap_domain, save_stock_data_checkpoint
from custom.ilsgateway.models import ILSGatewayConfig, SupplyPointStatus, DeliveryGroupReport, ReportRun, \
    GroupSummary, OrganizationSummary, ProductAvailabilityData, Alert, PendingReportingDataRecalculation
from custom.logistics.models import StockDataCheckpoint
from custom.logistics.tasks import stock_data_task
from dimagi.utils.dates import get_business_day_of_month, get_business_day_of_month_before


@periodic_task(run_every=crontab(hour="4", minute="00", day_of_week="*"),
               queue='logistics_background_queue')
def migration_task():
    from custom.ilsgateway.stock_data import ILSStockDataSynchronization
    for config in ILSGatewayConfig.get_all_steady_sync_configs():
        if config.enabled:
            endpoint = ILSGatewayEndpoint.from_config(config)
            ils_bootstrap_domain(ILSGatewayAPI(config.domain, endpoint))
            stock_data_task(ILSStockDataSynchronization(config.domain, endpoint))
            report_run.delay(config.domain)


@task(queue='logistics_background_queue')
def ils_bootstrap_domain_task(domain):
    ils_config = ILSGatewayConfig.for_domain(domain)
    return ils_bootstrap_domain(ILSGatewayAPI(domain, ILSGatewayEndpoint.from_config(ils_config)))


@task(queue='logistics_background_queue', ignore_result=True, acks_late=True)
def balance_migration_task(domain, endpoint):
    BalanceMigration(domain, endpoint).balance_migration()


# Region KILIMANJARO
ILS_FACILITIES = [948, 998, 974, 1116, 971, 1122, 921, 658, 995, 1057,
                  652, 765, 1010, 657, 1173, 1037, 965, 749, 1171, 980,
                  1180, 1033, 975, 1056, 970, 742, 985, 2194, 935, 1128,
                  1172, 773, 916, 1194, 4862, 1003, 994, 1034, 1113, 1167,
                  949, 987, 986, 960, 1046, 942, 972, 21, 952, 930,
                  1170, 1067, 1006, 752, 747, 1176, 746, 755, 1102, 924,
                  744, 1109, 760, 922, 945, 988, 927, 1045, 1060, 938,
                  1041, 1101, 1107, 939, 910, 934, 929, 1111, 1174, 1044,
                  1008, 914, 1040, 1035, 1126, 1203, 912, 990, 908, 654,
                  1051, 1110, 983, 771, 1068, 756, 4807, 973, 1013, 911,
                  1048, 1196, 917, 1127, 963, 1032, 1164, 951, 918, 999,
                  923, 1049, 1000, 1165, 915, 1036, 1121, 758, 1054, 1042,
                  4861, 1007, 1053, 954, 761, 1002, 748, 919, 976, 1177,
                  1179, 1001, 743, 762, 741, 959, 1119, 772, 941, 956, 964,
                  1014, 953, 754, 1202, 1166, 977, 757, 961, 759, 997, 947, 1112, 978, 1124,
                  768, 937, 1195, 913, 906, 1043, 1178, 992, 1038, 957, 1106, 767, 979, 1012,
                  926, 1120, 933, 1066, 1105, 943, 1047, 1063, 1004, 958, 751, 763, 1011, 936,
                  1114, 932, 984, 656, 653, 946, 1058, 931, 770, 1108, 909, 1118, 1062, 745, 1065,
                  955, 1052, 753, 944, 1061, 1069, 1104, 996, 4860, 950, 993, 1064, 1175, 1059, 1050,
                  968, 928, 989, 967, 966, 750, 981, 1055, 766, 1123, 1039, 1103, 655, 1125, 774, 991,
                  1117, 920, 769, 1005, 1009, 925, 1115, 907, 4996]


def get_locations(api_object, facilities):
    for facility in facilities:
        location = api_object.endpoint.get_location(facility, params=dict(with_historical_groups=1))
        api_object.location_sync(api_object.endpoint.models_map['location'](location))


def process_supply_point_status(supply_point_status, domain, location_id=None):
    location_id = location_id or supply_point_status.location_id
    try:
        SupplyPointStatus.objects.get(
            external_id=int(supply_point_status.external_id),
            location_id=location_id
        )
    except SupplyPointStatus.DoesNotExist:
        supply_point_status.save()


def sync_supply_point_status(domain, endpoint, facility, checkpoint, date, limit=100, offset=0):
    has_next = True
    next_url = ""

    while has_next:
        meta, supply_point_statuses = endpoint.get_supplypointstatuses(
            domain,
            limit=limit,
            offset=offset,
            next_url_params=next_url,
            filters=dict(supply_point=facility, status_date__gte=date),
            facility=facility
        )
        # set the checkpoint right before the data we are about to process
        if not supply_point_statuses:
            return None
        location_id = SQLLocation.objects.get(domain=domain, external_id=facility).location_id
        save_stock_data_checkpoint(checkpoint,
                                   'supply_point_status',
                                   meta.get('limit') or limit,
                                   meta.get('offset') or offset, date, location_id, True)
        for supply_point_status in supply_point_statuses:
            process_supply_point_status(supply_point_status, domain, location_id)

        if not meta.get('next', False):
            has_next = False
        else:
            next_url = meta['next'].split('?')[1]


def process_delivery_group_report(dgr, domain, location_id=None):
    location_id = location_id or dgr.location_id
    try:
        DeliveryGroupReport.objects.get(external_id=dgr.external_id, location_id=location_id)
    except DeliveryGroupReport.DoesNotExist:
        dgr.save()


def sync_delivery_group_report(domain, endpoint, facility, checkpoint, date, limit=100, offset=0):
    has_next = True
    next_url = ""
    while has_next:
        meta, delivery_group_reports = endpoint.get_deliverygroupreports(
            domain,
            limit=limit,
            offset=offset,
            next_url_params=next_url,
            filters=dict(supply_point=facility, report_date__gte=date),
            facility=facility
        )
        location_id = SQLLocation.objects.get(domain=domain, external_id=facility).location_id
        # set the checkpoint right before the data we are about to process
        save_stock_data_checkpoint(checkpoint,
                                   'delivery_group',
                                   meta.get('limit') or limit,
                                   meta.get('offset') or offset,
                                   date, location_id, True)
        for dgr in delivery_group_reports:
            try:
                DeliveryGroupReport.objects.get(external_id=dgr.external_id, location_id=location_id)
            except DeliveryGroupReport.DoesNotExist:
                dgr.save()

        if not meta.get('next', False):
            has_next = False
        else:
            next_url = meta['next'].split('?')[1]


@task(queue='background_queue', ignore_result=True)
def ils_clear_stock_data_task(domain):
    assert ILSGatewayConfig.for_domain(domain)
    locations = SQLLocation.objects.filter(domain=domain)
    SupplyPointStatus.objects.filter(location_id__in=locations.values_list('location_id', flat=True)).delete()
    DeliveryGroupReport.objects.filter(location_id__in=locations.values_list('location_id', flat=True)).delete()
    products = Product.ids_by_domain(domain)
    StockState.objects.filter(product_id__in=products).delete()
    StockTransaction.objects.filter(
        case_id__in=locations.exclude(supply_point_id__isnull=True).values_list('supply_point_id', flat=True)
    ).delete()
    StockReport.objects.filter(domain=domain).delete()
    StockDataCheckpoint.objects.filter(domain=domain).delete()


@task(queue='background_queue', ignore_result=True)
def clear_report_data(domain):
    locations_ids = SQLLocation.objects.filter(domain=domain).values_list('location_id', flat=True)
    GroupSummary.objects.filter(org_summary__location_id__in=locations_ids).delete()
    OrganizationSummary.objects.filter(location_id__in=locations_ids).delete()
    ProductAvailabilityData.objects.filter(location_id__in=locations_ids).delete()
    Alert.objects.filter(location_id__in=locations_ids).delete()
    ReportRun.objects.filter(domain=domain).delete()


@task(queue='background_queue', ignore_result=True)
def fix_stock_data_task(domain):
    fix_stock_data(domain)


@periodic_task(run_every=crontab(hour="4", minute="00", day_of_week="*"),
               queue='logistics_background_queue')
def report_run(domain, locations=None, strict=True):
    last_successful_run = ReportRun.last_success(domain)
    recalculation_on_location_change(domain, last_successful_run)

    last_run = ReportRun.last_run(domain)
    start_date = (datetime.min if not last_successful_run else last_successful_run.end)
    end_date = datetime.utcnow()

    running = ReportRun.objects.filter(complete=False, domain=domain)
    if running.count() > 0:
        raise Exception("Warehouse already running, will do nothing...")

    if last_run and last_run.has_error:
        run = last_run
        run.complete = False
        run.save()
    else:
        if start_date == end_date:
            return
        # start new run
        run = ReportRun.objects.create(start=start_date, end=end_date,
                                       start_run=datetime.utcnow(), domain=domain)
    has_error = True
    try:
        populate_report_data(run.start, run.end, domain, run, locations, strict=strict)
        has_error = False
    except Exception, e:
        # just in case something funky happened in the DB
        if isinstance(e, DatabaseError):
            try:
                transaction.rollback()
            except:
                pass
        has_error = True
        raise
    finally:
        # complete run
        run = ReportRun.objects.get(pk=run.id)
        run.has_error = has_error
        run.end_run = datetime.utcnow()
        run.complete = True
        run.save()
        logging.info("ILSGateway report runner end time: %s" % datetime.utcnow())

facility_delivery_partial = partial(send_for_day, cutoff=15, reminder_class=DeliveryReminder)
district_delivery_partial = partial(send_for_day, cutoff=13, reminder_class=DeliveryReminder,
                                    location_type='DISTRICT')


@periodic_task(run_every=crontab(day_of_month="13-15", hour=11, minute=0),
               queue="logistics_reminder_queue")
def first_facility_delivery_task():
    facility_delivery_partial(15)


@periodic_task(run_every=crontab(day_of_month="20-22", hour=11, minute=0),
               queue="logistics_reminder_queue")
def second_facility_delivery_task():
    facility_delivery_partial(22)


@periodic_task(run_every=crontab(day_of_month="26-30", hour=11, minute=0),
               queue="logistics_reminder_queue")
def third_facility_delivery_task():
    facility_delivery_partial(30)


@periodic_task(run_every=crontab(day_of_month="11-13", hour=5, minute=0),
               queue="logistics_reminder_queue")
def first_district_delivery_task():
    district_delivery_partial(13)


@periodic_task(run_every=crontab(day_of_month="18-20", hour=11, minute=0),
               queue="logistics_reminder_queue")
def second_district_delivery_task():
    district_delivery_partial(20)


@periodic_task(run_every=crontab(day_of_month="26-28", hour=11, minute=0),
               queue="logistics_reminder_queue")
def third_district_delivery_task():
    district_delivery_partial(28)


facility_randr_partial = partial(send_for_day, cutoff=5, reminder_class=RandrReminder, location_type='FACILITY')
district_randr_partial = partial(send_for_day, cutoff=13, reminder_class=RandrReminder, location_type='DISTRICT')


@periodic_task(run_every=crontab(day_of_month="3-5", hour=5, minute=0),
               queue="logistics_reminder_queue")
def first_facility():
    """Last business day before or on 5th day of the Submission month, 8:00am"""
    facility_randr_partial(5)


@periodic_task(run_every=crontab(day_of_month="8-10", hour=5, minute=0),
               queue="logistics_reminder_queue")
def second_facility():
    """Last business day before or on 10th day of the submission month, 8:00am"""
    facility_randr_partial(10)


@periodic_task(run_every=crontab(day_of_month="10-12", hour=5, minute=0),
               queue="logistics_reminder_queue")
def third_facility():
    """Last business day before or on 12th day of the submission month, 8:00am"""
    facility_randr_partial(12)


@periodic_task(run_every=crontab(day_of_month="11-13", hour=5, minute=0),
               queue="logistics_reminder_queue")
def first_district():
    district_randr_partial(13)


@periodic_task(run_every=crontab(day_of_month="13-15", hour=5, minute=0),
               queue="logistics_reminder_queue")
def second_district():
    district_randr_partial(15)


@periodic_task(run_every=crontab(day_of_month="15-17", hour=11, minute=0),
               queue="logistics_reminder_queue")
def third_district():
    district_randr_partial(17)


@periodic_task(run_every=crontab(day_of_month="26-31", hour=11, minute=15),
               queue="logistics_reminder_queue")
def supervision_task():
    now = datetime.utcnow()
    last_business_day = get_business_day_of_month(month=now.month, year=now.year, count=-1)
    if now.day == last_business_day.day:
        send_for_all_domains(last_business_day, SupervisionReminder)


def get_last_and_nth_business_day(date, n):
    last_month = datetime(date.year, date.month, 1) - timedelta(days=1)
    last_month_last_day = get_business_day_of_month(month=last_month.month, year=last_month.year, count=-1)
    nth_business_day = get_business_day_of_month(month=date.month, year=date.year, count=n)
    return last_month_last_day, nth_business_day


@periodic_task(run_every=crontab(day_of_month="26-31", hour=11, minute=0),
               queue="logistics_reminder_queue")
def first_soh_task():
    now = datetime.utcnow()
    last_business_day = get_business_day_of_month(month=now.month, year=now.year, count=-1)
    if now.day == last_business_day.day:
        send_for_all_domains(last_business_day, SOHReminder)


@periodic_task(run_every=crontab(day_of_month="1-3", hour=6, minute=0),
               queue="logistics_reminder_queue")
def second_soh_task():
    now = datetime.utcnow()
    last_month_last_day, first_business_day = get_last_and_nth_business_day(now, 1)
    if now.day == first_business_day.day:
        send_for_all_domains(last_month_last_day, SOHReminder)


@periodic_task(run_every=crontab(day_of_month="5-7", hour=5, minute=15),
               queue="logistics_reminder_queue")
def third_soh_task():
    now = datetime.utcnow()
    last_month_last_day, fifth_business_day = get_last_and_nth_business_day(now, 5)
    if now.day == fifth_business_day.day:
        send_for_all_domains(last_month_last_day, SOHReminder)


@periodic_task(run_every=crontab(day_of_month="6-8", hour=13, minute=0),
               queue="logistics_reminder_queue")
def soh_summary_task():
    """
        6th business day of the month @ 3pm Tanzania time
    """
    now = datetime.utcnow()
    sixth_business_day = get_business_day_of_month(month=now.month, year=now.year, count=6)
    if now.day != sixth_business_day.day:
        return

    for domain in ILSGatewayConfig.get_all_enabled_domains():
        for user in get_district_people(domain):
            send_translated_message(user, REMINDER_MONTHLY_SOH_SUMMARY, **construct_soh_summary(user.location))


@periodic_task(run_every=crontab(day_of_month="26-31", hour=13, minute=0),
               queue="logistics_reminder_queue")
def delivery_summary_task():
    """
        last business day of month 3pm Tanzania time
    """
    now = datetime.utcnow()
    last_business_day = get_business_day_of_month(month=now.month, year=now.year, count=-1)
    if now.day != last_business_day.day:
        return

    for domain in ILSGatewayConfig.get_all_enabled_domains():
        for user in get_district_people(domain):
            send_translated_message(
                user, REMINDER_MONTHLY_DELIVERY_SUMMARY, **construct_delivery_summary(user.location)
            )


@periodic_task(run_every=crontab(day_of_month="15-17", hour=13, minute=0),
               queue="logistics_reminder_queue")
def randr_summary_task():
    """
        on 17th day of month or before if it's not a business day @ 3pm Tanzania time
    """

    now = datetime.utcnow()
    business_day = get_business_day_of_month_before(month=now.month, year=now.year, day=17)
    if now.day != business_day.day:
        return

    for domain in ILSGatewayConfig.get_all_enabled_domains():
        for user in get_district_people(domain):
            send_translated_message(
                user, REMINDER_MONTHLY_RANDR_SUMMARY, **construct_randr_summary(user.location)
            )


@periodic_task(run_every=crontab(day_of_month="18-20", hour=14, minute=0),
               queue="logistics_reminder_queue")
def soh_thank_you_task():
    """
    Last business day before the 20th at 4:00 PM Tanzania time
    """
    now = datetime.utcnow()
    last_month = datetime(now.year, now.month, 1) - timedelta(days=1)
    for domain in ILSGatewayConfig.get_all_enabled_domains():
        SOHThankYouReminder(domain=domain, date=last_month).send()


def recalculate_on_group_change(location, last_run):
    OrganizationSummary.objects.filter(location_id=location.get_id).delete()
    process_facility_warehouse_data(location, default_start_date(), last_run.end)

    for parent in location.sql_location.get_ancestors(ascending=True):
        process_non_facility_warehouse_data(parent.couch_location,
                                            default_start_date(), last_run.end, strict=False)


def recalculate_on_parent_change(location, previous_parent_id, last_run):
    previous_parent = SQLLocation.objects.get(location_id=previous_parent_id)
    type_location_map = defaultdict(set)

    previous_ancestors = list(previous_parent.get_ancestors(include_self=True, ascending=True))
    actual_ancestors = list(location.sql_location.get_ancestors(ascending=True))

    locations_to_recalculate = set()

    i = 0
    while previous_ancestors[i] != actual_ancestors[i] and i < len(previous_ancestors):
        locations_to_recalculate.add(previous_ancestors[i])
        locations_to_recalculate.add(actual_ancestors[i])
        i += 1

    for sql_location in locations_to_recalculate:
        type_location_map[sql_location.location_type.name].add(sql_location)

    for location_type in ["DISTRICT", "REGION", "MSDZONE"]:
        for sql_location in type_location_map[location_type]:
            process_non_facility_warehouse_data(
                sql_location.couch_location, default_start_date(), last_run.end, strict=False
            )


def recalculation_on_location_change(domain, last_run):
    if not last_run:
        PendingReportingDataRecalculation.objects.filter(domain=domain).delete()
        return

    pending_recalculations = PendingReportingDataRecalculation.objects.filter(domain=domain).order_by('pk')
    recalcs_dict = defaultdict(list)

    for pending_recalculation in pending_recalculations:
        key = (pending_recalculation.sql_location, pending_recalculation.type)
        recalcs_dict[key].append(pending_recalculation.data)

    for (sql_location, recalculation_type), data_list in recalcs_dict.iteritems():
        # If there are more changes, consider earliest and latest change.
        # Thanks to this we avoid recalculations when in fact group/parent wasn't changed.
        # E.g Group is changed from A -> B and later from B -> A.
        # In this situation there is no need to recalculate data.
        if recalculation_type == 'group_change'\
                and data_list[0]['previous_group'] != data_list[-1]['current_group']:
            recalculate_on_group_change(sql_location.couch_location, last_run)
        elif recalculation_type == 'parent_change' \
                and data_list[0]['previous_parent'] != data_list[-1]['current_parent']:
            recalculate_on_parent_change(
                sql_location.couch_location, data_list[0]['previous_parent'], last_run
            )
        PendingReportingDataRecalculation.objects.filter(
            sql_location=sql_location, type=recalculation_type, domain=domain
        ).delete()
