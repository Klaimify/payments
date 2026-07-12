// Copyright (c) 2016, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Razorpay Settings", {
  refresh: function (frm) {
    frm.add_custom_button(__("Clear"), function () {
      frm.call({
        doc: frm.doc,
        method: "clear",
        callback: function (r) {
          frm.refresh();
        },
      });
    });
    frm.add_custom_button(__('Poll On-Demand Settlements'), function () {
      frappe.call({
        method:
          'payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.poll_ondemand_settlement_statuses',
        freeze: true,
        freeze_message: __('Polling on-demand settlements...'),
        callback: function (r) {
          frappe.msgprint({
            title: __('On-Demand Settlement Poll'),
            indicator: 'green',
            message: __('Poll completed. Check error log for details if any settlements were processed.'),
          });
        },
      });
    });
    frm.add_custom_button(__("Check Settlements"), function () {
      let d = new frappe.ui.Dialog({
        title: __("Check Settlements for Date"),
        fields: [
          {
            label: __("Settlement Date"),
            fieldname: "settlement_date",
            fieldtype: "Date",
            reqd: 1,
            default: frappe.datetime.get_today(),
          },
        ],
        primary_action_label: __("Process Settlements"),
        primary_action(values) {
          frappe.call({
            method:
              "payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.process_settlements_for_date",
            args: {
              date: values.settlement_date,
            },
            freeze: true,
            freeze_message: __("Processing settlements..."),
            callback: function (r) {
              if (r.message) {
                frappe.msgprint({
                  title: __("Settlement Processing"),
                  indicator: "green",
                  message: r.message,
                });
              }
            },
          });
          d.hide();
        },
      });
      d.show();
    });
  },
});
