import json

import frappe
from frappe import _
from frappe.integrations.utils import get_json

@frappe.whitelist(allow_guest=True)
def webhook_handler():
	"""Handler for all Razorpay webhook events"""
	original_user = frappe.session.user

	try:
		data = frappe.local.form_dict

		# Verify webhook signature
		razorpay_settings = frappe.get_doc("Razorpay Settings")
		settings = razorpay_settings.get_settings({})

		signature = frappe.get_request_header("X-Razorpay-Signature")
		razorpay_settings.verify_signature(
			body=frappe.request.get_data().decode(), signature=signature, key=settings.webhook_secret
		)

		# Handle different webhook events
		event = data.get("event")
		frappe.set_user("Administrator")
		if event.startswith("payment."):
			handle_payment_webhook(data)
		elif event.startswith("refund."):
			handle_refund_webhook(data)

		return {"status": "Success"}

	except Exception as e:
		frappe.logger().error(f"Razorpay Webhook Error: {frappe.get_traceback()}")
		return {"status": "Failed", "error": str(e)}
	finally:
		frappe.set_user(original_user)


def handle_payment_webhook(data):
	"""Handle payment related webhook events"""
	payment = data.get("payload", {}).get("payment", {}).get("entity", {})
	order_id = payment.get("order_id")
	status = payment.get("status")

	if not order_id:
		return

	# Find linked integration request
	ref_integration = frappe.db.get_value(
		"Integration Request",
		filters={
			"integration_request_service": "Razorpay",
			"output": ("like", f"%{order_id}%"),
		},
	)

	if ref_integration:
		ref_doc = frappe.get_doc("Integration Request", ref_integration)
		ref_data = json.loads(ref_doc.data)
		update_events(ref_doc, data)
		if ref_doc.status == "Queued":
			if status == "captured":
				ref_doc.update_status(ref_data, "Completed")
				update_payment_status(ref_doc, "Completed")
			elif status in ["failed", "expired"]:
				ref_doc.update_status(ref_data, "Failed")
				update_payment_status(ref_doc, "Failed")


def handle_refund_webhook(data):
	"""Handle refund related webhook events"""
	refund = data.get("payload", {}).get("refund", {}).get("entity", {})
	order_id = refund.get("order_id")
	status = refund.get("status")

	if order_id and status == "processed":
		# Find payment integration request
		ref_integration = frappe.db.get_value(
			"Integration Request",
			filters={
				"integration_request_service": "Razorpay",
				"output": ("like", f"%{order_id}%"),
				"status": "Completed",
			},
		)

		if ref_integration:
			ref_doc = frappe.get_doc("Integration Request", ref_integration)
			update_events(ref_doc, data)
			ref_doc.update_status({"refund": refund}, "Refunded")
			update_payment_status(ref_doc, "Refunded")


def update_events(integration_request, data):
	"""Append webhook event data to the integration request output"""
	if integration_request.output:
		output = json.loads(integration_request.output)
	else:
		output = {}

	events = output.get("events", [])
	events.append(data)
	output["events"] = events
	integration_request.db_set("output", get_json(output))


def update_payment_status(integration_request, status):
	"""Update payment status in the reference document"""
	if integration_request.reference_doctype and integration_request.reference_docname:
		ref_doc = frappe.get_doc(integration_request.reference_doctype, integration_request.reference_docname)

		if hasattr(ref_doc, "on_payment_authorized"):
			ref_doc.run_method("on_payment_authorized", status)
