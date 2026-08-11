frappe.listview_settings["APS Change Request"] = {
	onload(listview) {
		listview.page.add_inner_button(
			__("Change Impact Center", null, "Injection APS"),
			() => frappe.set_route("aps-change-impact-center")
		);
	},
};
