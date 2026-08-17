// Copyright (c) 2026, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("PayTM POS Settings", {
    refresh: function (frm) {
        frm.dashboard.set_headline(
      __("For more information, {0}.", [
        `<a href='https://www.paytmpayments.com/docs/pos-wireless-connection-prerequisite'>${__("Click here")}</a>`,
      ])
    );
    },
});
