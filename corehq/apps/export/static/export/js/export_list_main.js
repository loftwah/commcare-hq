hqDefine("export/js/export_list_main", function () {
    'use strict';

    /* Angular; to be deprecated */
    var initial_page_data = hqImport("hqwebapp/js/initial_page_data").get,
        listExportsApp = window.angular.module('listExportsApp', ['hq.list_exports']);
    listExportsApp.config(["$httpProvider", function ($httpProvider) {
        $httpProvider.defaults.xsrfCookieName = 'csrftoken';
        $httpProvider.defaults.xsrfHeaderName = 'X-CSRFToken';
        $httpProvider.defaults.headers.common["X-CSRFToken"] = $("#csrfTokenContainer").val();
    }]);
    listExportsApp.config(["djangoRMIProvider", function (djangoRMIProvider) {
        djangoRMIProvider.configure(initial_page_data("djng_current_rmi"));
    }]);
    listExportsApp.constant('bulk_download_url', initial_page_data("bulk_download_url"));
    listExportsApp.constant('modelType', initial_page_data("model_type"));
    listExportsApp.constant('staticModelType', initial_page_data("static_model_type"));
    listExportsApp.constant('filterFormElements', {
        emwf_form_filter: function () {
            return $('#id_emwf_form_filter');
        },
        emwf_case_filter: function () {
            return $('#id_emwf_case_filter');
        },
    });
    listExportsApp.constant('filterFormModalElement', function () {
        return $('#setFeedFiltersModal');
    });

    /* Knockout */
    var exportModel = function(options) {
        options.isAutoRebuildEnabled = options.isAutoRebuildEnabled || false;
        options.isDailySaved = options.isDailySaved || false;
        options.isFeed = options.isFeed || false;
        options.showLink = options.showLink || false;
        options.emailedExport = options.emailedExport || {};

        var mapping = {
            'copy': ["emailedExport"]
        };
        var self = ko.mapping.fromJS(options, mapping);

        self.isLocationSafeForUser = function () {
            return _.isEmpty(self.emailedExport) || self.emailedExport.isLocationSafeForUser;
        };

        self.downloadRequested = function (model, e) {
            var $btn = $(e.target);
            $btn.addClass('disabled');
            $btn.text(gettext('Download Requested'));
            return true;    // allow default click action to process so file is downloaded
        };
        self.copyLinkRequested = function (model, e) {
            model.showLink(true);
            var clipboard = new Clipboard(e.target, {
                target: function (trigger) {
                    return trigger.nextElementSibling;
                },
            });
            clipboard.onClick(e);
            clipboard.destroy();
        };

        self.updateEmailedExportData = function (model) {
            var component = model.emailedExport;
            $('#modalRefreshExportConfirm-' + model.id() + '-' + component.groupId).modal('hide');
            component.updatingData = true;
            // TODO: test
            $.ajax({
                method: 'POST',
                url: hqImport("hqwebapp/js/initial_page_data").reverse('update_emailed_export_data'),
                data: {
                    export_id: model.id(),
                },
                success: function (data) {
                    if (data.success) {
                        var exportType = hqImport('export/js/utils').capitalize(model.exportType());
                        hqImport('analytix/js/google').track.event(exportType + " Exports", "Update Saved Export", "Saved");
                        self.pollProgressBar(model);
                    }
                },
            });
        };

        self.updateDisabledState = function (model, e) {
            var component = model.emailedExport,
                $button = $(e.currentTarget);

            $button.disableButton();
            $.ajax({
                method: 'POST',
                url: hqImport("hqwebapp/js/initial_page_data").reverse('toggle_saved_export_enabled'),
                data: {
                    export_id: model.id(),
                    is_auto_rebuild_enabled: model.isAutoRebuildEnabled(),
                },
                success: function (data) {
                    if (data.success) {
                        var exportType = hqImport('export/js/utils').capitalize(model.exportType());
                        var event = (model.isAutoRebuildEnabled() ? "Disable" : "Enable") + " Saved Export";
                        hqImport('analytix/js/google').track.event(exportType + " Exports", event, "Saved");
                        model.isAutoRebuildEnabled(data.isAutoRebuildEnabled);
                    }
                    $button.enableButton();
                    $('#modalEnableDisableAutoRefresh-' + model.id() + '-' + component.groupId).modal('hide');
                },
            });
        };

        return self;
    };

    var exportListModel = function(options) {
        hqImport("hqwebapp/js/assert_properties").assert(options, ['exports']);

        var self = {};

        self.exports = _.map(options.exports, function (e) { return exportModel(e); });
        self.myExports = _.filter(self.exports, function (e) { return !!e.my_export; });
        self.notMyExports = _.filter(self.exports, function (e) { return !e.my_export; });

        self.sendExportAnalytics = function () {
            hqImport('analytix/js/kissmetrix').track.event("Clicked Export button");
            return true;
        };

        self.setFilterModalExport = function (e) {
            // TODO: test, since this comment isn't going to be true anymore
            // The filterModalExport is used as context for the FeedFilterFormController
            self.filterModalExport = e;
        };

        // TODO: test
        // Polling
        self.pollProgressBar = function (exp) {
            exp.emailedExport.updatingData = false;
            exp.emailedExport.taskStatus = {
                'percentComplete': 0,
                'inProgress': true,
                'success': false,
            };
            var tick = function () {
                $.ajax({
                    method: 'GET',
                    url: hqImport("hqwebapp/js/initial_page_data").reverse("get_saved_export_progress"),
                    data: {
                        export_instance_id: exp.id(),
                    },
                    success: function (data) {
                        exp.emailedExport.taskStatus = data.taskStatus;
                        if (!data.taskStatus.success) {
                            // The first few ticks don't yet register the task
                            exp.emailedExport.taskStatus.inProgress = true;
                            setTimeout(tick, 1500);
                        } else {
                            exp.emailedExport.taskStatus.justFinished = true;
                        }
                    },
                });
            };
            tick();
        };
        _.each(self.exports, function (exp) {
            if (exp.emailedExport && exp.emailedExport.taskStatus && exp.emailedExport.taskStatus.inProgress) {
                self.pollProgressBar(exp);
            }
        });

        // Bulk export handling
        self.selectAll = function() {
            _.each(self.exports, function (e) { e.addedToBulk(true); });
        };
        self.selectNone = function() {
            _.each(self.exports, function (e) { e.addedToBulk(false); });
        };
        self.showBulkExportDownload = ko.observable(false);
        self.bulkExportList = ko.observable('');
        _.each(self.exports, function (e) {
            e.addedToBulk.subscribe(function (newValue) {
                // Determine whether or not to show bulk export download button & message
                if (newValue !== self.showBulkExportDownload()) {
                    self.showBulkExportDownload(!!_.find(self.exports, function (e) {
                        return e.addedToBulk();
                    }));
                }

                // Update hidden value of exports to download
                if (self.showBulkExportDownload()) {
                    self.bulkExportList(JSON.stringify(_.map(_.filter(self.exports, function (e) {
                        return e.addedToBulk();
                    }), function (e) {
                        return ko.mapping.toJS(e);
                    })));
                }
            });
        });

        return self;
    };

    $(function () {
        var initialPageData = hqImport("hqwebapp/js/initial_page_data");

        $("#create-export").koApplyBindings(hqImport("export/js/create_export").createExportModel({
            model_type: initialPageData.get("model_type", true),
            drilldown_fetch_url: initialPageData.reverse('get_app_data_drilldown_values'),
            drilldown_submit_url: initialPageData.reverse('submit_app_data_drilldown_form'),
            page: {
                is_daily_saved_export: initialPageData.get('is_daily_saved_export', true),
                is_feed: initialPageData.get('is_feed', true),
                is_deid: initialPageData.get('is_deid', true),
                model_type: initialPageData.get('model_type', true),
            },
        }));
        $('#createExportOptionsModal').on('show.bs.modal', function () {
            hqImport('analytix/js/kissmetrix').track.event("Clicked New Export");
        });

        $("#export-list").koApplyBindings(exportListModel({
            exports: initialPageData.get("exports"),
        }));

        var modelType = initial_page_data("model_type");
        if (modelType === 'form') {
            hqImport('analytix/js/kissmetrix').track.event('Visited Export Forms Page');
        } else if (modelType === 'case') {
            hqImport('analytix/js/kissmetrix').track.event('Visited Export Cases Page');
        }
    });
});
