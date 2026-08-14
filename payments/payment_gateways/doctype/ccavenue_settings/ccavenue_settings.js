// Copyright (c) 2026, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("CCAvenue Settings", {
  refresh: function (frm) {
    frm.dashboard.set_headline(
      __("For more information, {0}.", [
        `<a href='https://www.ccavenue.com/'>${__("Click here")}</a>`,
      ])
    );

    const webhook_url = `${window.location.origin}/api/method/payments.payment_gateways.doctype.ccavenue_settings.ccavenue_settings.ccavenue_webhook`;
    frm.dashboard.add_comment(
      __(
        "Configure this URL as the Server-to-Server / Dynamic Event Notification URL in your CCAvenue merchant dashboard: {0}",
        [`<code>${webhook_url}</code>`]
      ),
      "blue",
      true
    );
  },
});
