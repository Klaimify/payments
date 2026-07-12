import json

import frappe
from frappe import _
from frappe.integrations.utils import get_json

@frappe.whitelist(allow_guest=True)
def webhook_handler():
	"""Handler for all Razorpay webhook events"""
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
		if event.startswith("payment."):
			handle_payment_webhook(data)
		elif event.startswith("refund."):
			handle_refund_webhook(data)
		elif event.startswith("settlement."):
			# Enqueue settlement processing to avoid timeout
			frappe.enqueue(
				method="payments.webhook.razorpay.handle_settlement_webhook",
				queue="short",
				timeout=300,
				data=data,
			)
			frappe.logger().info(f"Settlement webhook enqueued for processing: {data.get('payload', {}).get('settlement', {}).get('entity', {}).get('id')}")

		return {"status": "Success"}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Razorpay Webhook Error")
		return {"status": "Failed", "error": str(e)}


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
		
		# Store payment_id in output for settlement tracking
		if payment.get("id"):
			output = json.loads(ref_doc.output) if ref_doc.output else {}
			output["payment_id"] = payment.get("id")
			ref_doc.db_set("output", get_json(output))
		
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


def finalize_integration_request_settlement(integration_request, settlement_data, source="webhook"):
	"""Finalize settlement for an Integration Request in an idempotent way."""
	latest_output = frappe.db.get_value("Integration Request", integration_request.name, "output")
	output = json.loads(latest_output) if latest_output else {}
	processed = output.get("settlement_processed") or {}
	settlement_id = settlement_data.get("settlement_id")

	if settlement_id and processed.get("settlement_id") == settlement_id:
		return False

	output["settlement_processed"] = {
		"settlement_id": settlement_id,
		"status": "processed",
		"amount": settlement_data.get("amount"),
		"currency": settlement_data.get("currency"),
		"utr": settlement_data.get("utr"),
		"settled_at": settlement_data.get("settled_at"),
		"source": source,
	}
	integration_request.db_set("output", get_json(output))

	if settlement_id:
		cache_key = f"razorpay_settlement_processed_{settlement_id}"
		frappe.cache().setex(cache_key, 30 * 24 * 60 * 60, "1")

	if integration_request.reference_doctype and integration_request.reference_docname:
		ref_doctype_doc = frappe.get_doc(integration_request.reference_doctype, integration_request.reference_docname)
		if hasattr(ref_doctype_doc, "on_payment_settlement"):
			ref_doctype_doc.run_method("on_payment_settlement", settlement_data)
			frappe.logger().info(
				f"Successfully processed settlement for {integration_request.reference_doctype} "
				f"{integration_request.reference_docname}"
			)
			return True

		frappe.logger().warning(
			f"{integration_request.reference_doctype} does not have on_payment_settlement method"
		)
		return False

	frappe.logger().warning(f"Integration Request {integration_request.name} has no reference document")
	return False


def handle_settlement_webhook(data):
	"""Handle settlement related webhook events"""
	settlement = data.get("payload", {}).get("settlement", {}).get("entity", {})
	settlement_id = settlement.get("id")
	status = settlement.get("status")

	if not settlement_id or status != "processed":
		return

	# Get settlement date - Razorpay provides created_at timestamp
	# Convert Unix timestamp to datetime
	import datetime
	try:
		settlement_timestamp = settlement.get("created_at")
		if not settlement_timestamp:
			frappe.logger().warning(f"No created_at timestamp in settlement {settlement_id}")
			return
		
		# Convert Unix timestamp to datetime
		settlement_date = datetime.datetime.fromtimestamp(settlement_timestamp)
		year = settlement_date.year
		month = settlement_date.month
		day = settlement_date.day
		
		frappe.logger().info(
			f"Processing settlement {settlement_id} for date {year}-{month:02d}-{day:02d}"
		)
	except Exception as e:
		frappe.log_error(f"Error parsing settlement date: {str(e)}", "Settlement Webhook")
		return

	# Get Razorpay settings to fetch settlement transactions
	razorpay_settings = frappe.get_doc("Razorpay Settings")
	settlement_items = razorpay_settings.fetch_settlement_transactions(year, month, day)

	if not settlement_items:
		return

	# Filter items for this specific settlement_id
	settlement_items = [item for item in settlement_items if item.get("settlement_id") == settlement_id]
	
	if not settlement_items:
		frappe.logger().warning(
			f"No settlement items found for settlement_id {settlement_id} on date {year}-{month:02d}-{day:02d}"
		)
		return
	
	frappe.logger().info(
		f"Found {len(settlement_items)} items for settlement {settlement_id}"
	)

	# Process each payment in the settlement
	processed_count = 0
	failed_count = 0
	
	for item in settlement_items:
		if item.get("type") != "payment":
			frappe.logger().debug(f"Skipping non-payment item: {item.get('type')}")
			continue

		entity_id = item.get("entity_id")
		if not entity_id:
			frappe.logger().warning("Settlement item missing entity_id")
			continue

		frappe.logger().info(
			f"Processing settlement item - Payment ID: {entity_id}, "
			f"Amount: {item.get('amount')}, "
			f"Settled: {item.get('settled')}, "
			f"UTR: {item.get('settlement_utr')}"
		)

		# Find integration request by payment_id stored in output
		integration_requests = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Razorpay",
				"status": "Completed",
				"output": ("like", f"%{entity_id}%"),
			},
			fields=["name"],
		)
		
		if not integration_requests:
			frappe.logger().warning(f"No Integration Request found for payment_id: {entity_id}")
			failed_count += 1
			continue

		for integration_request in integration_requests:
			try:
				ref_doc = frappe.get_doc("Integration Request", integration_request.name)
				
				# Update events with settlement data
				update_events(ref_doc, data)

				settlement_data = {
					"settlement_id": settlement_id,
					"payment_id": entity_id,
					"utr": item.get("settlement_utr") or settlement.get("utr"),
					"settled": item.get("settled"),
					"amount": item.get("amount"),
					"currency": item.get("currency"),
					"settled_at": item.get("settled_at"),
					"settlement_data": item,
				}

				if finalize_integration_request_settlement(ref_doc, settlement_data, source="webhook"):
					processed_count += 1
				else:
					failed_count += 1
			except Exception as e:
				frappe.log_error(
					f"Error processing settlement for payment {entity_id}: {str(e)}", 
					"Settlement Webhook"
				)
				failed_count += 1
	
	frappe.logger().info(
		f"Settlement {settlement_id} processing complete: "
		f"{processed_count} succeeded, {failed_count} failed"
	)


def update_payment_status(integration_request, status):
	"""Update payment status in the reference document"""
	if integration_request.reference_doctype and integration_request.reference_docname:
		ref_doc = frappe.get_doc(integration_request.reference_doctype, integration_request.reference_docname)

		if hasattr(ref_doc, "on_payment_authorized"):
			ref_doc.run_method("on_payment_authorized", status)
