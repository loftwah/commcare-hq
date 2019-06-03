from __future__ import absolute_import
from __future__ import unicode_literals

from collections import defaultdict

from corehq.apps.app_manager.dbaccessors import get_current_app
from corehq.apps.export.dbaccessors import get_latest_case_export_schema, get_latest_form_export_schema
from corehq.apps.export.models import CaseExportDataSchema, ExportItem, FormExportDataSchema
from corehq.apps.reports.analytics.esaccessors import get_case_types_for_domain_es


def get_case_type_to_properties(domain):
    case_type_to_properties = defaultdict(list)
    case_types = get_case_types_for_domain_es(domain)
    for case_type in case_types:
        if not case_type:
            # TODO - understand why a case can have a blank case type and handle appropriately
            continue
        case_export_schema = (
            get_latest_case_export_schema(domain, case_type)
            or CaseExportDataSchema.generate_schema_from_builds(domain, None, case_type)
        )
        for export_group_schema in case_export_schema.group_schemas[0].items:
            cleaned_case_property = export_group_schema.label.replace('_', '')
            case_type_to_properties[case_type].append(cleaned_case_property)
    return dict(case_type_to_properties)


def get_xmlns_by_app(domain, app_id):
    app = get_current_app(domain, app_id)
    xmlnss = [form.xmlns.split('/')[-1] for form in app.get_forms()]
    print(xmlnss)
    return xmlnss


def get_properties_by_xmlns(domain, app_id, xmlns):
    complete_xmlns = 'http://openrosa.org/formdesigner/' + xmlns
    form_export_schema = get_latest_form_export_schema(
        domain, app_id, complete_xmlns
    ) or FormExportDataSchema.generate_schema_from_builds(domain, app_id, complete_xmlns)
    export_items = [
        item for item in form_export_schema.group_schemas[0].items
        if isinstance(item, ExportItem)
    ]

    def _cleanup(item):
        return item.label.split('.')[-1].replace('#', '').strip()

    return set([_cleanup(item) for item in export_items]) - {''}
