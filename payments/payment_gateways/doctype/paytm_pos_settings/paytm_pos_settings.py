import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

STAGING_HOST = "https://securegw-stage.paytm.in"
PRODUCTION_HOST = "https://securegw-edc.paytm.in"

STAGING_REFUND_HOST = "https://securestage.paytmpayments.com"
PRODUCTION_REFUND_HOST = "https://secure.paytmpayments.com"

SALE_PATH = "/ecr/payment/request"
STATUS_PATH = "/ecr/V2/payment/status"
VOID_PATH = "/ecr/void"
REFUND_PATH = "/refund/apply"
REFUND_STATUS_PATH = "/v2/refund/status"


class PayTMPOSSettings(Document):
	def validate(self):
		if not (self.merchant_id or "").strip():
			frappe.throw(_("Merchant ID is required"))

		if not (self.channel_id or "").strip():
			frappe.throw(_("Channel ID is required"))

		if not (self.merchant_key or "").strip():
			frappe.throw(_("Merchant Key is required"))

		if self.timeout_configuration:
			if not (1 <= cint(self.timeout_configuration) <= 60):
				frappe.throw(_("Timeout Configuration must be between 1 and 60 seconds"))

		seen_terminal_ids = set()
		for row in self.table_qsom:
			if row.enabled and row.terminal_id in seen_terminal_ids:
				frappe.throw(_("Terminal ID {0} is duplicated in POS Devices").format(row.terminal_id))
			seen_terminal_ids.add(row.terminal_id)


def get_paytm_pos_config() -> dict:
	"""Return the PayTM POS Settings as a dict with the merchant key decrypted and
	the host/endpoints resolved from the staging flag."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	config.update(
		{"merchant_key": get_decrypted_password("PayTM POS Settings", "PayTM POS Settings", "merchant_key")}
	)

	host = STAGING_HOST if cint(config.get("staging")) else PRODUCTION_HOST
	refund_host = STAGING_REFUND_HOST if cint(config.get("staging")) else PRODUCTION_REFUND_HOST
	config.update(
		{
			"host": host,
			"sale_endpoint": host + SALE_PATH,
			"status_endpoint": host + STATUS_PATH,
			"void_endpoint": host + VOID_PATH,
			"refund_endpoint": refund_host + REFUND_PATH,
			"refund_status_endpoint": refund_host + REFUND_STATUS_PATH,
		}
	)
	return config


def get_enabled_terminals() -> list[dict]:
	"""Return the enabled POS Devices rows as a list of dicts."""
	settings = frappe.get_single("PayTM POS Settings")
	return [row.as_dict() for row in settings.table_qsom if row.enabled]


def get_terminal(terminal_name: str | None = None) -> dict:
	"""Resolve the terminal to use for a Sale request. If terminal_name is given it
	must match an enabled row's name or terminal_id; otherwise the first enabled terminal is used."""
	terminals = get_enabled_terminals()
	if not terminals:
		frappe.throw(_("No POS terminal configured"))

	if terminal_name:
		for terminal in terminals:
			if terminal["terminal_name"] == terminal_name or terminal["terminal_id"] == terminal_name:
				return terminal
		frappe.throw(_("POS terminal {0} is not enabled or not found").format(terminal_name))

	return terminals[0]


def is_api_enabled(api_name: str) -> bool:
	"""Check if a specific API is enabled in PayTM POS Settings."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	return bool(cint(config.get(api_name)))


@frappe.whitelist()
def get_pos_terminals() -> list[dict]:
	"""Return all POS Devices as a list of dicts for use in linked fields."""
	terminals = get_enabled_terminals()
	return [
		{
			"terminal_id": t.get("terminal_id"),
			"terminal_name": t.get("terminal_name"),
			"enabled": t.get("enabled"),
		}
		for t in terminals
	]


@frappe.whitelist()
def get_pos_config() -> dict:
	"""Return POS configuration (without sensitive key) for frontend use."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	return {
		"merchant_id": config.get("merchant_id"),
		"channel_id": config.get("channel_id"),
		"staging": cint(config.get("staging")),
		"sale_api": cint(config.get("sale_api")),
		"status_enquiry_api": cint(config.get("status_enquiry_api")),
		"void_api": cint(config.get("void_api")),
		"refund_api": cint(config.get("refund_api")),
		"refund_status_api": cint(config.get("refund_status_api")),
		"terminals": get_enabled_terminals(),
	}
