# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class WebhookResponseLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		decrypted_response: DF.Code | None
		error: DF.SmallText | None
		gateway: DF.Data | None
		http_status_code: DF.Int
		integration_request: DF.Link | None
		order_id: DF.Data | None
		raw_payload: DF.Code | None
		status: DF.Literal["Success", "Failed", "Error"]
	# end: auto-generated types

	pass
