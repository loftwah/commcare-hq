import json
from corehq.apps.users.models import CouchUser
from casexml.apps.case.models import CommCareCase
from corehq.apps.app_manager.models import ApplicationBase, Application
from dimagi.utils.couch.safe_index import safe_index
from dimagi.utils.decorators import inline
from casexml.apps.phone.caselogic import get_footprint
from datetime import datetime
from corehq.elastic import get_es
import urllib

CASE_STATUS_OPEN = 'open'
CASE_STATUS_CLOSED = 'closed'
CASE_STATUS_ALL = 'all'

def closed_to_status(closed_bool):
    return {None: CASE_STATUS_ALL,
            True: CASE_STATUS_CLOSED,
            False: CASE_STATUS_OPEN}[closed_bool]

def status_to_closed_flags(status):
    return {CASE_STATUS_ALL: [True, False],
            CASE_STATUS_CLOSED: [True],
            CASE_STATUS_OPEN: [False]}[status]

class CaseAPIResult(object):
    """
    The result of a case API query. Useful for abstracting out the difference
    between an id-only representation and a full_blown one.
    """
    def __init__(self, id=None, couch_doc=None, id_only=False):
        self._id = id
        self._couch_doc = couch_doc
        self.id_only = id_only

    def __getitem__(self, key):
        if key == 'case_id':
            return self.id
        else:
            return self.case_json.__getitem__(key)

    @property
    def id(self):
        if self._id is None:
            self._id = self._couch_doc._id
        return self._id

    @property
    def case_json(self):
        if self._couch_doc is None:
            self._couch_doc = CommCareCase.get(self._id)
        return self._couch_doc.get_json()

    def to_json(self):
        return self.id if self.id_only else self.case_json

class CaseAPIHelper(object):
    """
    Simple config object for querying the APIs
    """
    def __init__(self, status='open', case_type=None, ids_only=False,
                 footprint=False):
        if status not in [CASE_STATUS_ALL, CASE_STATUS_CLOSED, CASE_STATUS_OPEN]:
            raise ValueError("invalid case status %s" % status)
        self.status = status
        self.case_type = case_type
        self.ids_only = ids_only
        self.footprint = footprint

    def get_all(self, domain):
        key = [domain, self.case_type or {}, {}]
        view_name = 'hqcase/open_cases' if self.status==CASE_STATUS_OPEN else 'hqcase/all_cases'
        if not self.ids_only:
            cases = CommCareCase.view(view_name,
                startkey=key,
                endkey=key + [{}],
                include_docs=True,
                reduce=False,
            )
            return [CaseAPIResult(couch_doc=case) for case in cases]
        else:
            return [CaseAPIResult(id=res["id"], id_only=True) \
                    for res in CommCareCase.get_db().view(
                        view_name,
                        startkey=key,
                        endkey=key + [{}],
                        include_docs=False,
                        reduce=False,
                    )]

    def get_owned(self, domain, user_id):
        try:
            user = CouchUser.get_by_user_id(user_id, domain)
        except KeyError:
            user = None
        try:
            owner_ids = user.get_owner_ids()
        except AttributeError:
            owner_ids = [user_id]

        @list
        @inline
        def keys():
            for owner_id in owner_ids:
                for bool in status_to_closed_flags(self.status):
                    yield [domain, owner_id, bool]

        # TODO: honor ids_only,
        # use more efficient view
        cases = CommCareCase.view('hqcase/by_owner', keys=keys, include_docs=True, reduce=False)
        if self.footprint:
            cases = get_footprint(cases).values()
        # demo_user cases!
        return [case.get_json() for case in cases]

# todo: Make these api functions use generators for streaming
# so that a limit call won't fetch more docs than it needs to
# This could be achieved with something like CommCareCase.paging_view that
# returns a generator but internally batches couch requests
# potentially doubling the batch-size each time in case it really is a lot of data

def get_all_cases(domain, include_closed=False, case_type=None, ids_only=False):
    """
    Get all cases in a domain.
    """
    status = CASE_STATUS_ALL if include_closed else CASE_STATUS_OPEN
    helper = CaseAPIHelper(status, case_type, ids_only)
    return helper.get_all(domain)

def get_owned_cases(domain, user_id, closed=False, footprint=False, ids_only=False):
    """
    Get all cases in a domain owned by a particular user.
    """
    helper = CaseAPIHelper(closed_to_status(closed), ids_only=ids_only,
                           footprint=footprint)
    return helper.get_owned(domain, user_id)

def get_filtered_cases(domain, user_id=None, filters=None, footprint=False,
                       ids_only=False):

    @inline
    def cases():
        """pre-filter cases based on user_id and (if possible) closed"""
        closed = json.loads(filters.get('closed') or 'null')
        case_type = filters.get('properties/case_type')

        if user_id:
            return get_owned_cases(domain, user_id, closed=closed, 
                                   footprint=footprint)
        else:
            return get_all_cases(domain, include_closed=closed in (True, None),
                                 case_type=case_type, ids_only=ids_only)

    if filters:
        def _filter(res):
            for path, val in filters.items():
                # optimization hack: we secretly know that 'closed' is taken
                # care of by the above calls, and that a filter value of None
                # means don't filter
                if val is None or path == 'closed':
                    continue

                actual_val = safe_index(res.case_json, path.split("/"))

                if actual_val != val:
                    # closed=false => case.closed == False
                    if val in ('null', 'true', 'false'):
                        if actual_val != json.loads(val):
                            return False
                    else:
                        return False

            return True
        cases = filter(_filter, cases)
    return cases

def es_filter_cases(domain, filters=None):
    """
    Filter cases using elastic search
    """
    
    class ElasticCaseQuery(object):
        # this class is currently pretty customized to serve exactly
        # this API. one day it may be worth reconciling our ES interfaces
        # but today is not that day.
        RESERVED_KEYS = ('date_modified_start', 'date_modified_end', 
                         'server_date_modified_start', 'server_date_modified_end', 
                         'limit')
    
        def __init__(self, domain, filters):
            self.domain = domain
            self.filters = filters
            self.limit = int(filters.get('limit', 50))
            self._date_modified_start = filters.get("date_modified_start", None)
            self._date_modified_end = filters.get("date_modified_end", None)
            self._server_date_modified_start = filters.get("server_date_modified_start", None)
            self._server_date_modified_end = filters.get("server_date_modified_end", None)
            
        
        @property
        def uses_modified(self):
            return bool(self._date_modified_start or self._date_modified_end)
        
        @property
        def uses_server_modified(self):
            return bool(self._server_date_modified_start or self._server_date_modified_end)
        
        @property
        def date_modified_start(self):
            return self._date_modified_start or datetime(1970,1,1).strftime("%Y-%m-%d")
        
        @property
        def date_modified_end(self):
            return self._date_modified_end or datetime.max.strftime("%Y-%m-%d")
        
        @property
        def server_date_modified_start(self):
            return self._server_date_modified_start or datetime(1970,1,1).strftime("%Y-%m-%d")
        
        @property
        def server_date_modified_end(self):
            return self._server_date_modified_end or datetime.max.strftime("%Y-%m-%d")
        
        @property
        def scrubbed_filters(self):
            return dict((k, v) for k, v in self.filters.items() if k not in self.RESERVED_KEYS)
        
        def _modified_params(self, key, start, end):
            return {
                'range': {
                    key: {
                        'from': start,
                        'to': end
                    }
                }
            }
        
        @property
        def modified_params(self, ):
            return self._modified_params('modified_on',
                                         self.date_modified_start,
                                         self.date_modified_end)
        
        @property
        def server_modified_params(self):
            return self._modified_params('server_modified_on',
                                         self.server_date_modified_start,
                                         self.server_date_modified_end)
        
        def get_terms(self):
            yield {'term': {'domain.exact': self.domain}}
            if self.uses_modified:
                yield self.modified_params
            if self.uses_modified:
                yield self.modified_params
            if self.uses_server_modified:
                yield self.server_modified_params
            for k, v in self.scrubbed_filters.items():
                yield {'term': {k: v.lower()}}

        def get_query(self):
            return {
                'query': {
                    'bool': {
                        'must': list(self.get_terms())
                    }
                },
                'sort': {
                    'modified_on': {'order': 'asc'}
                },
                'from': 0,
                'size': self.limit,
            }
    
    q = ElasticCaseQuery(domain, filters)
    res = get_es().get('hqcases/_search', data=q.get_query())
    # this is ugly, but for consistency / ease of deployment just
    # use this to return everything in the expected format for now
    return [CommCareCase.wrap(r["_source"]).get_json() for r in res['hits']['hits'] if r["_source"]]

def get_filters_from_request(request, limit_top_level=None):
    """
    limit_top_level lets you specify a whitelist of top-level properties you can include in the filters,
    properties with a / in them are always included in the filters
    """
    def _decode(thing):
        try:
            return urllib.unquote(thing)
        except Exception:
            return thing
    
    # super weird hack: force decoding keys because sometimes (only seen in 
    # production) django doesn't do this for us.
    filters = dict((_decode(k), v) for k, v in request.REQUEST.items())
    if limit_top_level is not None:
        filters = dict([(key, val) for key, val in filters.items() if '/' in key or key in limit_top_level])

    filters.update({
        'user_id': None,
        'closed': ({
            'any': None,
            'true': 'true',
            'false': 'false',
        }.get(filters.get('closed'), 'false')),
        'format': None,
        'footprint': None,
        'ids_only': None,
    })
    return filters

def get_cloudcare_apps(domain):
    return map(lambda app: app._doc,
               ApplicationBase.view('cloudcare/cloudcare_apps', 
                                    startkey=[domain], endkey=[domain, {}]))

def get_app(domain, app_id):
    app = Application.get(app_id)
    assert(app.domain == domain)
    return app._doc


